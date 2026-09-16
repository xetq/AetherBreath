"""
AetherBreath Agent - 带对话连续性的生产级入口
支持多轮工具调用、错误恢复、日志记录、会话持久化
所有配置均从 config.yaml 和 .env 加载
"""

import os
import sys
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any, Tuple, Optional
from pathlib import Path
import yaml
import hashlib

# ===== 把项目根目录加入 sys.path，以便导入根目录下的模块 =====
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ===== 导入依赖 =====
from openai import OpenAI
from dotenv import load_dotenv
from task_orchestrator import ToolCall, TaskOrchestrator

# 日志模块（同目录直接导入）
from logger import SessionLogger

# 技能系统（同目录直接导入：技能库扫描 / 注册表同步 / 上下文注入块）
from skill_system import sync_registry, build_injection_block

# MCP 服务站（同目录直接导入：注册表同步 / 注入块 / 总闸）
# v2：MCP 只注入**注册表**（每 station 一行），工具 schema 由模型的 mcp_search 按需取
# （见 docs/MCP设计.md v2 §四）。与技能注册表同一处、同一套"会话内冻结"语义。
import mcp_station

# 上下文管理器（同目录直接导入：视图生成 / 落盘 / 命中 / 回捞）
# 原文永不动：本模块只生产「发给模型的视图」，working_memory 与日志一律不改
from context_manager import ContextManager, load_params as load_cm_params

# 中期交互（同目录直接导入）：回合运行中主人追加的「用户交代」→ 随下一批工具返回回灌模型。
# 本模块只负责接线：投递入口只在 WebUI 侧（bridge 的 /mid_turn），CLI 下信箱恒空，
# flush 零开销、行为零差异 —— 这与"agent.py 仅路由"一致，逻辑都在 mid_turn.py 里。
import mid_turn

# 工具网关
from agent_tools import (
    AVAILABLE_TOOLS,
    TOOLS_SCHEMA,
)


# ========== 1. 加载配置 ==========

# 1.1 显式加载 .env（不依赖 cwd）：项目根 .env 优先，兼容历史 agent/.env 位置
def _load_env_files() -> None:
    candidates = [
        PROJECT_ROOT / ".env",
        Path(__file__).parent / ".env",
    ]
    loaded = [str(p) for p in candidates if p.exists()]
    for p in candidates:
        if p.exists():
            load_dotenv(p, override=False)
    if loaded:
        print(f"📄 已加载 .env: {', '.join(loaded)}")
    else:
        print("ℹ️ 未找到 .env 文件（项目根或 agent/ 目录），将读取系统环境变量与 config.yaml 默认值")

# 1.2 计算项目根目录（已定义）

# 1.3 加载 config.yaml
def load_config():
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"❌ 配置文件不存在: {config_path}\n"
            "请确保在项目根目录创建 config.yaml（参考文档）"
        )
    with open(config_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# 1.35 加载 .env（须在读取 LLM 环境变量之前执行）
_load_env_files()

# 通用环境变量读取：按顺序取第一个非空值（新键名优先，兼容旧键名）
def _get_env(*names, default=None):
    """按顺序返回第一个非空环境变量；全部缺失则返回 default。"""
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default

# 1.4 解析路径
def resolve_path(relative_path: str) -> Path:
    return PROJECT_ROOT / relative_path

WORKING_MEMORY_DIR = resolve_path(CONFIG["paths"]["working_memory"])
LONG_MEMORY_DIR = resolve_path(CONFIG["paths"]["long_memory"])
SOUL_PATH = resolve_path(CONFIG["paths"]["soul_file"])
AGENTS_PATH = resolve_path(CONFIG["paths"]["agents_file"])
MEMORY_PATH = resolve_path(CONFIG["paths"]["memory_file"])
WORKSPACE_ROOT = resolve_path(CONFIG["paths"]["workspace"])
KNOWLEDGE_BASE_DIR = resolve_path(CONFIG["paths"]["knowledge_base"])
CHROMA_DB_DIR = resolve_path(CONFIG["paths"]["chroma_db"])
# 技能系统：技能库目录 + 注册表文件（缺省 agent_skills/，注册表每会话启动同步一次）
SKILLS_DIR = resolve_path(CONFIG["paths"].get("skills_dir", "agent_skills"))
SKILL_REGISTRY_PATH = resolve_path(
    CONFIG["paths"].get("skill_registry", "agent_skills/SKILL_REGISTRY.md")
)

# 确保必要的目录存在
WORKING_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
LONG_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
KNOWLEDGE_BASE_DIR.mkdir(parents=True, exist_ok=True)

# 1.45 上下文管理器：参数 + 常驻实例（会话日志在 main 里注入）
#      设计见 agent_workspace/上下文管理器/DESIGN.md，开关在 config.yaml 的
#      context_manager.enabled（false = 完全回退到原文直发）
CM_PARAMS = load_cm_params(CONFIG.get("context_manager"))
_CONTEXT_MGR: Optional[ContextManager] = None
_LAST_PROMPT_TOKENS: Dict[str, int] = {}      # sid → 上一轮 API 返回的真实 prompt_tokens


def get_context_manager(log=None) -> ContextManager:
    """懒创建常驻上下文管理器；会话切换时刷新其日志句柄。"""
    global _CONTEXT_MGR
    if _CONTEXT_MGR is None:
        _CONTEXT_MGR = ContextManager(PROJECT_ROOT, CM_PARAMS, log=log)
    elif log is not None:
        _CONTEXT_MGR.log = log
    return _CONTEXT_MGR

# 1.5 读取 LLM 配置（OpenAI 兼容通用方案，支持任意厂商）
#     .env 填三个核心变量即可切换厂商：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
#     旧键名 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / MODEL_NAME 仍兼容（自动回退）
#     REASONING_EFFORT / THINKING_ENABLED 是厂商专属参数，默认关闭（最兼容），
#     目标模型支持时再显式开启（详见 .env.example）。
LLM_API_KEY = _get_env("LLM_API_KEY", "DEEPSEEK_API_KEY")
MODEL_NAME = _get_env("LLM_MODEL", "MODEL_NAME", default=CONFIG["defaults"].get("model", "deepseek-chat"))
MAX_ITERATIONS = int(_get_env("MAX_ITERATIONS", default=CONFIG["defaults"].get("max_iterations", 100)))

# 新配置模式：只要用了 LLM_* 三件套（任意一个），行为参数只认 LLM_ 新键且默认关闭；
# 否则（纯旧 DEEPSEEK_* 配置）才兼容旧的 REASONING_EFFORT / THINKING_ENABLED 键。
_USING_LLM_KEYS = any(os.environ.get(k) for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"))
if _USING_LLM_KEYS:
    REASONING_EFFORT = _get_env("LLM_REASONING_EFFORT", default="off")
    THINKING_ENABLED = _get_env("LLM_THINKING_ENABLED", default="false").lower() == 'true'
    LLM_BASE_URL = _get_env("LLM_BASE_URL")
    if not LLM_BASE_URL:
        raise ValueError(
            "❌ 使用 LLM_* 配置时请同时设置 LLM_BASE_URL\n"
            "（参考 .env.example 中各家厂商的端点示例）。"
        )
else:
    REASONING_EFFORT = _get_env("REASONING_EFFORT", default="off")
    THINKING_ENABLED = _get_env("THINKING_ENABLED", default="false").lower() == 'true'
    LLM_BASE_URL = _get_env("DEEPSEEK_BASE_URL", default="https://api.deepseek.com")

if not LLM_API_KEY:
    raise ValueError(
        "❌ 环境变量 LLM_API_KEY 未设置（或旧名 DEEPSEEK_API_KEY）。\n"
        "请在项目根目录或 agent/ 目录的 .env 中填写（参考 .env.example），\n"
        "或先设置系统环境变量 LLM_API_KEY。"
    )


# ========== 2. 初始化 OpenAI 客户端 ==========
client = OpenAI(
    api_key=LLM_API_KEY,
    base_url=LLM_BASE_URL,
)


# ========== 2.1 API 请求组装（厂商无关） ==========

def compose_chat_kwargs(
    messages: List[Dict[str, Any]],
    model: str = MODEL_NAME,
    tools=None,
    reasoning_effort: str | None = None,
    thinking: bool = False,
) -> Dict[str, Any]:
    """组装 chat.completions.create 的请求参数。

    - reasoning_effort: 仅当显式给出且不是 off/none 时才附带（多数厂商不认，
      带了会 400，默认不传 = 最兼容）。
    - thinking: DeepSeek 系 / GLM 等 thinking 模型的专属字段，默认关闭。
    """
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "tools": tools,
        "tool_choice": "auto",
    }
    if reasoning_effort and str(reasoning_effort).lower() not in ("", "off", "none", "false"):
        kwargs["reasoning_effort"] = reasoning_effort
    if thinking:
        kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
    return kwargs


def _mask_key(key: str) -> str:
    """打码密钥用于启动日志：sk-abc...xyz1（绝不打印完整 key）。"""
    if not key:
        return "(未设置)"
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}...{key[-4:]}"


print(
    f"🤖 LLM: model={MODEL_NAME} | base_url={LLM_BASE_URL} | "
    f"key={_mask_key(LLM_API_KEY)} | thinking={'ON' if THINKING_ENABLED else 'off'} | "
    f"reasoning_effort={REASONING_EFFORT}"
)


# ========== 3. 会话管理函数 ==========

def get_session_id() -> str:
    """获取或创建会话ID"""
    print("\n" + "=" * 60)
    print("🤖 AetherBreath Agent ")
    print("=" * 60)

    existing = list(WORKING_MEMORY_DIR.glob("*.json"))
    if existing:
        print("\n📂 已有会话:")
        for i, f in enumerate(existing, 1):
            try:
                with open(f, 'r', encoding='utf-8') as fp:
                    data = json.load(fp)
                    msg_count = len(data.get("messages", []))
                    created = data.get("created_at", "未知")
                    status = data.get("status", "complete")
                    status_mark = {"active": "🔄 进行中", "interrupted": "⚠️ 中断未完成", "complete": "✅ 正常"}.get(status, status)
                    print(f"  {i}. {f.stem} ({status_mark}, 消息数: {msg_count}, 创建: {created})")
            except:
                print(f"  {i}. {f.stem}")

    print("\n选项:")
    print("  - 输入已有会话ID 继续对话")
    print("  - 输入 'new' 创建新会话")
    print("  - 直接回车 自动创建新会话")

    choice = input("\n💬 请输入: ").strip()

    if choice and choice.lower() != 'new':
        session_id = choice
        session_file = WORKING_MEMORY_DIR / f"{session_id}.json"
        if not session_file.exists():
            print(f"⚠️ 会话 '{session_id}' 不存在，将创建新会话")
            return session_id
        return session_id

    session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"✅ 新会话创建: {session_id}")
    return session_id


def load_session(session_id: str, log: SessionLogger) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """
    加载会话历史。

    Returns:
        (messages, saved_snapshot): messages 为纯对话历史（不含 system）；
        saved_snapshot 为上次保存的 system_prompt（语境快照），
        用于续聊时判断「源文件是否变化、能否沿用旧快照」。
    """
    session_file = WORKING_MEMORY_DIR / f"{session_id}.json"
    if session_file.exists():
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                messages = data.get("messages", [])
                clean = [m for m in messages if m.get("role") != "system"]
                if len(clean) != len(messages):
                    log.warning(f"会话 {session_id} 清理了 {len(messages) - len(clean)} 条重复 system 消息")
                    messages = clean
                snapshot = data.get("system_prompt")
                if snapshot:
                    one_line = snapshot.replace("\n", " ").strip()
                    print(f"📜 上次语境快照: {one_line[:60]}...")
                status = data.get("status", "complete")
                if status == "interrupted":
                    print(f"♻️ 会话 '{session_id}' 上次被中断，已从断点恢复（{len(messages)} 条消息）")
                else:
                    print(f"📂 加载会话 '{session_id}'，共 {len(messages)} 条消息")
                return messages, snapshot
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"加载会话失败: {e}，从空会话开始")
            return [], None
    return [], None


def save_session(session_id: str, messages: List[Dict[str, str]], log: SessionLogger, status: str = "active"):
    """保存会话历史（原子写）"""
    session_file = WORKING_MEMORY_DIR / f"{session_id}.json"
    tmp_file = session_file.with_suffix(".json.tmp")
    try:
        system_prompt = None
        for m in messages:
            if m.get("role") == "system":
                system_prompt = m.get("content", "")
                break
        messages = [m for m in messages if m.get("role") != "system"]
        data = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "message_count": len(messages),
            "status": status,
            "messages": messages
        }
        if system_prompt is not None:
            data["system_prompt"] = system_prompt
        with open(tmp_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_file, session_file)
    except Exception as e:
        log.error(f"保存会话失败: {e}")
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass


# ========== 4. 核心 Agent 循环 ==========

# ===== 4.0 常驻编排器单例：agent.py 仅作路由器，不直接执行任何工具 =====
# 编排器实例化一次（双通道线程池：串行 1 worker + 并行 8 worker），
# 跨 LLM 轮次/会话复用；一切工具执行都交给它，避免反复建销线程池。



def close_dangling_tool_calls(conversation, tool_calls, log,
                              reason="❌ 本回合在该调用生效前被中断，操作未执行。"):
    """给每个已声明却没有响应的 tool_call 补一条 role=tool 消息。

    OpenAI 兼容接口要求 assistant.tool_calls 的每个 id 都有配对 tool 消息，缺一条
    就会让该会话下次加载被判 400（表现为这个对话再也发不出消息）；界面侧则显示成
    一个空的工具卡片。审批把等待拉到分钟级，使这种断头从罕见变成常见，故必须闭合。
    """
    try:
        have = set()
        for m in conversation:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                have.add(m.get("tool_call_id"))
        n = 0
        for tc in (tool_calls or []):
            tcid = getattr(tc, "id", None)
            if tcid and tcid not in have:
                conversation.append({"role": "tool", "tool_call_id": tcid, "content": reason})
                n = n + 1
        if n:
            log.warning("已为 " + str(n) + " 个未响应调用补闭合消息（防会话损坏）")
        return n
    except Exception:
        return 0        # 闭合逻辑自己绝不能再把回合搞崩


_ORCHESTRATOR: TaskOrchestrator | None = None


def _get_orchestrator() -> TaskOrchestrator:
    """懒创建常驻编排器；首次调用后整场会话复用同一实例。"""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        _ORCHESTRATOR = TaskOrchestrator(
            max_workers=8,
            default_timeout=30,
            tools_map=AVAILABLE_TOOLS,
            log_enabled=True,
        )
        print("🧭 常驻编排器已启动（串行管道 ×1 + 并行管道 ×8）")
    return _ORCHESTRATOR


def shutdown_orchestrator() -> None:
    """关闭常驻编排器，回收双通道线程池（会话结束/进程退出时调用）。"""
    global _ORCHESTRATOR
    if _ORCHESTRATOR is not None:
        _ORCHESTRATOR.shutdown()
        _ORCHESTRATOR = None
        print("🧭 常驻编排器已关闭")


# ===== 4.05 请求组装：system 快照原样 + 历史走上下文视图 =====
# 只影响「发给模型的那一份」，conversation 与落盘原文一字不改 —— 原文永远是真相。

def assemble_request(conversation: List[Dict[str, Any]], session_id: str, log) -> List[Dict[str, Any]]:
    """组装请求体：system 消息（快照冻结）不动，历史部分优先用已落盘的上下文视图。"""
    if not CM_PARAMS.get("enabled"):
        return conversation
    try:
        sys_msgs = [m for m in conversation if m.get("role") == "system"]
        history = [m for m in conversation if m.get("role") != "system"]
        body, how = get_context_manager(log).view_for(session_id, history)
        if how == "stale_view":
            # 视图与原文不一致：本轮回退原文，下个轮次边界会重算视图。
            # 用量不必清 —— 本轮 API 返回的真实 prompt_tokens 会覆盖它。
            log.warning("上下文视图与原文不一致：本轮回退原文，下个轮次边界重算")
        return sys_msgs + list(body)
    except Exception as e:
        log.warning(f"上下文视图组装失败，本轮用原文: {e}")
        return conversation


def _install_restore_provider(conversation: List[Dict[str, Any]], session_id: str) -> None:
    """把「当前会话原文」接到 restore_context 工具上。

    视图层下原文始终在内存（conversation）里，取回无需任何归档文件；
    conversation 是同一个 list 对象，本回合后续 append 的内容也能取到。
    """
    try:
        from agent_tools.restore_context import set_provider
        cm = get_context_manager()

        def _fn(round_no=None, tool_call_id=None, max_chars: int = 20000):
            # 轮号口径与视图一致：都不含 system（system 是快照，不属于任何一轮）
            hist = [m for m in conversation if m.get("role") != "system"]
            return cm.restore(session_id, hist, round_no=round_no,
                              tool_call_id=tool_call_id, max_chars=max_chars)

        set_provider(_fn)
    except Exception:
        pass          # 数据源装不上不影响主流程（工具会给明确错误）


def _maybe_compact_at_turn_start(conversation: List[Dict[str, Any]], session_id: str, log) -> None:
    """回合入口：判定并生成上下文视图（同步，实测 603 条约 50ms）。

    ⚠️ 必须放在这里、而不是 main() 的交互循环里：
      - CLI 走 main() → call_agent_with_tools
      - **WebUI 走 bridge.py:1065 → call_agent_with_tools（完全不经过 main()）**
    2026-09-13 实测事故：判定只写在 main() 里，WebUI 会话永远不触发压缩，
    视图目录一直是空的（主人发现"压缩后没看到视图"）。
    两条路径的公共入口只有这一个函数，故判定放这里。
    """
    if not CM_PARAMS.get("enabled"):
        return
    try:
        get_context_manager(log).maybe_compact(
            session_id,
            [m for m in conversation if m.get("role") != "system"],
            prompt_tokens=_LAST_PROMPT_TOKENS.get(session_id),
            model=MODEL_NAME,
        )
    except Exception as e:
        log.warning(f"上下文压缩判定失败（本轮继续用现有上下文）: {e}")


def call_agent_with_tools(
    messages: List[Dict[str, Any]],
    session_id: str,
    log: SessionLogger,
    max_iterations: int = MAX_ITERATIONS
) -> Dict[str, Any]:
    conversation = messages.copy()
    _install_restore_provider(conversation, session_id)   # restore_context 的数据源
    _maybe_compact_at_turn_start(conversation, session_id, log)   # 轮次边界：压缩判定

    def ensure_tool_responses(conv: List[Dict[str, Any]]) -> None:
        i = 0
        while i < len(conv):
            msg = conv[i]
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                needed_ids = {tc.get("id") for tc in msg["tool_calls"] if tc.get("id")}
                existing_ids = set()
                j = i + 1
                while j < len(conv) and conv[j].get("role") == "tool":
                    tid = conv[j].get("tool_call_id")
                    if tid in needed_ids:
                        existing_ids.add(tid)
                    j += 1
                missing_ids = needed_ids - existing_ids
                if missing_ids:
                    log.warning(f"为 {len(missing_ids)} 个 tool_calls 补上 '编排器未响应'")
                    for tid in sorted(missing_ids):
                        conv.insert(i + 1, {
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": "❌ 编排器未响应",
                        })
                        i += 1
            i += 1

    ensure_tool_responses(conversation)
    log.info("Agent 循环开始", max_iterations=max_iterations, message_count=len(conversation))
    iteration = 0

    try:
        while iteration < max_iterations:
            iteration += 1
            log.debug(f"Agent 循环第 {iteration}/{max_iterations} 轮")
            ensure_tool_responses(conversation)

            try:
                response = client.chat.completions.create(**compose_chat_kwargs(
                    assemble_request(conversation, session_id, log),
                    model=MODEL_NAME,
                    tools=TOOLS_SCHEMA,
                    reasoning_effort=REASONING_EFFORT,
                    thinking=THINKING_ENABLED,
                ))
            except Exception as e:
                log.error(f"API 调用失败: {e}")
                return {"error": f"API 请求异常: {str(e)}"}

            log.debug("LLM 原始响应", raw=response.model_dump())

            # 上下文用量真值：本轮的 prompt_tokens 就是「当前上下文实际占用」，
            # 供下一个轮次边界判定是否压缩（本地估算只作兜底，见 DESIGN.md §6）
            try:
                _u = getattr(response, "usage", None)
                if _u is not None:
                    _LAST_PROMPT_TOKENS[session_id] = int(getattr(_u, "prompt_tokens", 0) or 0)
            except (TypeError, ValueError, AttributeError):
                pass

            assistant_msg = response.choices[0].message
            conversation.append(assistant_msg.model_dump())
            save_session(session_id, conversation, log)

            if assistant_msg.tool_calls:
                reasoning_text = getattr(assistant_msg, "reasoning_content", None)
                progress_text = (assistant_msg.content or "").strip() or (reasoning_text or "").strip()
                if progress_text:
                    print(f"[中期进度]: {progress_text}")
                    log.info(f"[中期进度]: {progress_text}")
            if not assistant_msg.tool_calls:
                log.info(f"Agent 完成推理，共 {iteration} 轮")
                save_session(session_id, conversation, log, status="complete")
                return {
                    "content": assistant_msg.content or "",
                    "conversation": conversation,
                    "iterations": iteration,
                    "prompt_tokens": _LAST_PROMPT_TOKENS.get(session_id),
                }

            tool_calls = assistant_msg.tool_calls

            # ---- 统一工具执行：agent.py 仅作路由器，一切工具执行交给常驻编排器 ----
            # 单工具调用 → 编排器串行管道（1 worker）；多工具批 → 并行管道（依赖分层）
            orchestrator = _get_orchestrator()
            orchestrator.set_logger(log)
            log.info(f"工具调用开始，共 {len(tool_calls)} 个（统一编排）")

            parsed_calls = []
            audit_items = []      # (tc, name, args)：本批待审批项
            call_record = set()

            for tc in tool_calls:
                func_name = tc.function.name
                args_hash = hashlib.md5(tc.function.arguments.encode()).hexdigest()
                key = (func_name, args_hash)
                if key in call_record:
                    log.warning(f"检测到重复调用，已跳过: {func_name}")
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "⚠️ 检测到重复调用，已自动跳过。",
                    })
                    continue
                call_record.add(key)

                try:
                    func_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    log.warning(f"工具参数解析失败: {tc.function.arguments}")
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "❌ 参数解析失败: 请提供有效的 JSON 参数",
                    })
                    continue

                audit_items.append((tc, func_name, func_args))

            # ---- 审批闸门（agent/approval.py）：整批原子 ----
            # 本批只要有一条没拿到批准，全部工具都不提交编排器 ——
            # 模型把它们放在同一批，说明这是同一个意图的整体；只做一半可能留下
            # 比全不做更糟的中间态。通过的原样进编排器，依赖分层与串并行仍由
            # 编排器决定，审批只控制"请求能不能传过去"。
            if audit_items:
                _dec = {}
                try:
                    from approval import gate_batch as _gate_batch
                    # 只路由：本批工具 + 整个会话交给引擎，审批上下文由引擎自己取。
                    _dec, _grants = _gate_batch(
                        [(t[0].id, t[1], t[2]) for t in audit_items],
                        session_id=session_id, cwd=str(PROJECT_ROOT),
                        conversation=conversation)
                except BaseException as _be:
                    # 等裁决时被 Ctrl+C / 停止按钮打断：整批补闭合响应，否则会话里
                    # 留下"有 tool_call 无 tool 响应"的断头，下次加载直接 400。
                    for _tcx, _nmx, _kwx in audit_items:
                        conversation.append({
                            "role": "tool", "tool_call_id": _tcx.id,
                            "content": "❌ 审批未完成（等待裁决时回合被中断），本批全部未执行。",
                        })
                    if isinstance(_be, Exception):
                        log.warning("审批引擎异常，本批按未获批处理: " + str(_be))
                    else:
                        raise
                for _tc, _nm, _kw in audit_items:
                    _ok, _why = _dec.get(_tc.id, (True, ""))
                    if not _ok:
                        log.warning("审批未通过: " + _nm)
                        conversation.append({
                            "role": "tool", "tool_call_id": _tc.id,
                            "content": _why or "❌ 审批未通过。",
                        })
                        continue
                    parsed_calls.append(ToolCall(
                        id=_tc.id, name=_nm, arguments=_kw, depends_on=[],
                    ))

            if parsed_calls:
                try:
                    batch_result = orchestrator.execute(parsed_calls)
                except Exception as e:
                    log.error(f"编排器执行异常: {e}")
                    batch_result = None

                if batch_result is None:
                    for tc in parsed_calls:
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": "❌ 编排器执行失败",
                        })
                else:
                    result_map = {res.tool_call_id: res for res in batch_result.results}
                    for tc in parsed_calls:
                        res = result_map.get(tc.id)
                        if res is None:
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": "❌ 编排器未返回该工具结果",
                            })
                        else:
                            content = str(res.result) if res.success else f"❌ {res.error}"
                            conversation.append({
                                "role": "tool",
                                "tool_call_id": res.tool_call_id,
                                "content": content,
                            })
                    if batch_result.is_interrupted:
                        log.warning("编排器被中断，部分工具未完成")

            # ---- 中期交互（agent/mid_turn.py）：本批工具结果已落进 conversation，
            # 此刻把主人趁跑动时追加的「用户交代」作为独立 user 消息一并带回模型。
            # 信箱为空时直接返回 0，零开销、零行为差异；实现全在 mid_turn 里。
            mid_turn.flush_after_tools(conversation, session_id, log)

            save_session(session_id, conversation, log)

    except KeyboardInterrupt:
        try:                                  # 先闭合断头，再保存
            close_dangling_tool_calls(conversation, tool_calls, log)
        except NameError:
            pass
        log.warning("Agent 被用户中断，正在保存当前进度...")
        try:
            save_session(session_id, conversation, log, status="interrupted")
        except Exception as e:
            log.error(f"中断保存失败: {e}")
        return {
            "content": "(被中断，未完成)",
            "conversation": conversation,
            "iterations": iteration,
            "interrupted": True
        }

    log.warning(f"Agent 达到最大迭代次数 {max_iterations}，强制终止")
    try:
        save_session(session_id, conversation, log, status="interrupted")
    except Exception as e:
        log.error(f"保存失败: {e}")
    return {
        "error": f"Agent 循环超过最大迭代次数 {max_iterations}",
        "conversation": conversation,
        "iterations": iteration
    }


# ========== 5. 提示词注入 ==========

def load_system_prompt(log: SessionLogger) -> Tuple[str, List[str]]:
    """
    组装系统提示（SOUL/AGENTS/MEMORY/技能注册表 + 项目信息）——【仅新对话调用】。

    返回 (prompt_text, injected_sources)，injected_sources 记录本次实际读入的
    源文件（"注入 xxx.md: path"）。快照冻结（Freeze on Start）语义：
    main() 只在「无已保存快照 = 新对话」时调用本函数并注入；
    续聊同一对话时无条件复用已持久化的快照，不调用本函数（不读源文件、
    不 sync），源 .md 变更需开新对话才生效。
    """
    parts: List[str] = []
    injected: List[str] = []
    if SOUL_PATH.exists():
        try:
            with open(SOUL_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 SOUL.md: {SOUL_PATH}")
        except Exception as e:
            log.warning(f"加载 SOUL.md 失败: {e}")
    else:
        log.debug(f"SOUL.md 不存在: {SOUL_PATH}，跳过")

    if AGENTS_PATH.exists():
        try:
            with open(AGENTS_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 AGENTS.md: {AGENTS_PATH}")
        except Exception as e:
            log.warning(f"加载 AGENTS.md 失败: {e}")
    else:
        log.debug(f"AGENTS.md 不存在: {AGENTS_PATH}，跳过")

    if MEMORY_PATH.exists():
        try:
            with open(MEMORY_PATH, 'r', encoding='utf-8') as f:
                parts.append(f.read().strip())
                injected.append(f"注入 MEMORY.md: {MEMORY_PATH}")
        except Exception as e:
            log.warning(f"加载 MEMORY.md 失败: {e}")
    else:
        log.debug(f"MEMORY.md 不存在: {MEMORY_PATH}，跳过")

    # 技能注册表：每会话启动同步一次（多增少删），随后把注册表全文作为
    # 「技能目录快照」注入上下文（会话内冻结；需要执行技能时按路径读正文）
    try:
        sync_result = sync_registry(
            skills_dir=SKILLS_DIR, registry_path=SKILL_REGISTRY_PATH
        )
        if sync_result.changed:
            log.info(f"SKILL_REGISTRY.md 已同步: {sync_result.summary()}")
        else:
            log.debug("SKILL_REGISTRY.md 无变化")
        if sync_result.issues:
            for issue in sync_result.issues:
                log.warning(f"技能扫描提示: {issue}")
        registry_block = build_injection_block(
            skills_dir=SKILLS_DIR, registry_path=SKILL_REGISTRY_PATH
        )
        if registry_block:
            parts.append(registry_block)
            injected.append(f"注入 SKILL_REGISTRY.md: {SKILL_REGISTRY_PATH}")
    except Exception as e:
        log.warning(f"SKILL_REGISTRY.md 加载失败: {e}")

    # MCP 服务站注册表：同一处、同一套"会话内冻结"语义（v2，见 docs/MCP设计.md §四）。
    # 只注入**注册表**（每 station 一行：名字/用途/工具数/开关）——**不含任何工具 schema**：
    # 一个 station 可能上百个工具，全量注入是纯浪费；模型要用时先 mcp_search 取 schema。
    # 冻结 ≠ 不热：mcp_search/mcp_call 每次实时读 station 文件夹，新加的 station 同会话就能用。
    try:
        if not mcp_station.mcp_enabled():
            log.info("MCP 总闸关闭（config.yaml 的 mcp.enabled=false），跳过注册表注入")
        else:
            mcp_sync_result = mcp_station.sync_registry()
            if mcp_sync_result.changed:
                log.info(f"MCP_REGISTRY.md 已同步: {mcp_sync_result.summary()}")
            else:
                log.debug("MCP_REGISTRY.md 无变化")
            for issue in (mcp_sync_result.issues or []):
                log.warning(f"MCP station 提示: {issue}")
            mcp_block = mcp_station.build_injection_block()
            if mcp_block:
                parts.append(mcp_block)
                injected.append("注入 MCP_REGISTRY.md: %s" % mcp_station.registry_path())
            elif mcp_sync_result.stations:
                log.debug("MCP 注册表为空（没有可见的 station），不注入")
    except Exception as e:
        log.warning(f"MCP_REGISTRY.md 加载失败: {e}")

    if not parts:
        parts.append("你是一个智能助手，可以调用工具来完成复杂任务。")
        injected.append("注入 内置默认系统提示（未找到任何源文件）")

    base_prompt = "\n\n---\n\n".join(parts)

    project_info = f"""

    ## 项目信息（由系统自动注入，无需用户说明）
    - 项目根目录：`{PROJECT_ROOT}`
    - 工作空间：`{WORKSPACE_ROOT}`
    - 所有文件操作都相对于项目根目录。除非用户明确指定绝对路径，否则不要访问项目根目录以外的文件。
    - 如果你需要读取或操作文件，请基于上述路径进行。
    - 知识库目录：`{KNOWLEDGE_BASE_DIR}`
    - 会话存储目录：`{WORKING_MEMORY_DIR}`
    """

    return base_prompt + project_info, injected


# ========== 6. 主程序入口 ==========

def main():
    session_id = get_session_id()

    log = SessionLogger(session_id)
    log.info("会话启动", session_id=session_id)

    history, saved_snapshot = load_session(session_id, log)

    # 上下文管理器：接上本会话日志 + 清理上次写盘中断留下的临时文件
    cm = get_context_manager(log)
    _n_orphan = cm.clean_orphans()
    if _n_orphan:
        log.warning(f"清理上下文视图孤儿临时文件 {_n_orphan} 个")

    # 快照冻结（Freeze on Start）：同一对话的 system prompt 在对话开始时
    # 构建一次并随会话持久化；此后每次续聊【无条件复用】保存的快照——
    # 不读源 .md、不 sync 注册表、不做任何比较。任务中 memory.md 被多次
    # 修改、或新增了技能，本对话都不会重新加载；要生效必须开新对话。
    if saved_snapshot is not None:
        system_prompt = saved_snapshot
        log.info("语境快照沿用（快照冻结：本对话不随 .md 变更重新加载）")
        print("🧊 语境快照沿用")
    else:
        # 新对话：读取全部源文件组装快照，注入后即冻结
        system_prompt, injected_sources = load_system_prompt(log)
        for src in injected_sources:
            log.info(src)
        print(f"🧊 新对话：语境快照已注入并冻结（{len(injected_sources)} 个源文件）")

    messages = [{"role": "system", "content": system_prompt}] + history

    print("\n" + "=" * 60)
    print(f"💬 会话: {session_id}")
    print(f"📝 历史消息: {len(history)} 条")
    print("输入 'exit' 或 'quit' 退出并保存")
    print("=" * 60 + "\n")

    exit_status = "complete"
    try:
        while True:
            user_input = input("👤 你: ").strip()
            if user_input.lower() in ['exit', 'quit', 'q']:
                break
            if not user_input:
                continue

            messages.append({"role": "user", "content": user_input})
            log.info(f"用户输入: {user_input[:100]}")

            result = call_agent_with_tools(messages, session_id, log)

            if "error" in result:
                print(f"\n❌ 错误: {result['error']}\n")
                log.error(f"Agent 执行错误: {result['error']}")
                messages.pop()
                continue

            messages = result["conversation"]
            print(f"\n🤖 Agent: {result['content']}\n")

    except KeyboardInterrupt:
        print("\n\n⚠️ 检测到 Ctrl+C，正在保存会话...")
        exit_status = "interrupted"
    finally:
        save_session(session_id, messages, log, status=exit_status)
        log.info(f"会话结束，状态: {exit_status}")
        shutdown_orchestrator()
        log.close()
        print(f"✅ 会话已保存: {session_id} (状态: {exit_status})")
        print("👋 下次启动时输入相同会话ID即可继续对话")


if __name__ == "__main__":
    main()