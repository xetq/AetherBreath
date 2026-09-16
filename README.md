# AetherBreath ☲

一个**可自我成长**的终端 Agent（自研学习项目）：对话连续性、工具调用、并行任务编排、
审批安全层、长期记忆、技能系统、MCP 集成、以及一套独立 WebUI。

主对话走 **OpenAI 兼容协议** —— 改 `.env` 就能换任意厂商（DeepSeek / 智谱 GLM / 硅基流动 /
Kimi / OpenAI / Ollama 本地…），**零代码改动**。

> ### 🌱 这是初始化版本
> 四个长期记忆文件（`SOUL.md` / `AGENTS.md` / `MEMORY.md` / `USER.md`）都是**模板**。
> 第一次启动时，agent 会读到模板里的引导指令，**主动问你**：怎么称呼你、想给它起什么名字、
> 什么说话风格、哪些事必须先问过你…… 然后把答案写进这些文件。
>
> **克隆 → 配 key → 跑起来，它就会变成"你的" agent。**

---

## 快速开始

### 0. 环境要求

- **Python 3.10+**（开发环境为 3.11）
- **Node 18+** —— *可选*，只有你要改 WebUI 前端源码才需要。已构建好的 `dist/` 随仓库附带，开箱即用

### 1. 安装依赖

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt     # Linux/macOS: venv/bin/python
```

### 2. 配置 LLM（唯一必做的一步）

```bash
cp .env.example .env          # 放项目根；也可放 agent/.env（两处都认，项目根优先）
```

编辑 `.env`，填三个核心变量：

```bash
LLM_API_KEY=sk-你的密钥
LLM_BASE_URL=https://api.deepseek.com/v1     # 任意 OpenAI 兼容端点
LLM_MODEL=deepseek-chat
```

各厂商的端点与模型名示例（DeepSeek / 智谱 / 硅基流动 / Kimi / OpenAI / Ollama / 百炼…）
全写在 `.env.example` 的注释里。

> `.env` 含密钥，已加入 `.gitignore`，**不要提交**。
> `thinking` 与 `reasoning_effort` 默认关闭（兼容性最好）；只有目标模型明确支持时再开，否则部分厂商会报 400。

### 3. 首次启动 —— 初始化引导

```bash
python agent/agent.py
```

启动时会打印 LLM 配置摘要，核对是否切到了目标厂商：

```
📄 已加载 .env: D:\...\.env
🤖 LLM: model=deepseek-chat | base_url=https://api.deepseek.com/v1 | key=sk-...xxxx
```

然后 agent 会**主动开始问你**（称呼 / 时区 / 语言 / 风格 / 边界 / 技术背景），
并把答案写进 `agent_memory/long_memory/` 下的四个文件。

> ⚠️ **写完要「新开一个对话」才生效。**
> 系统提示是**对话创建时组装一次并冻结**的（语境快照冻结），续聊同一对话不会重读 `.md`。
> 这是刻意设计（防止任务中途提示词漂移），细节见 `agent_memory/long_memory/AGENTS.md`。

### 4. 停止

| 动作 | 效果 |
|---|---|
| 输入 `exit` / `quit` / `q` | 正常保存会话并退出 |
| `Ctrl+C` | 保存为"中断"状态；下次启动输入同一会话 ID 可续聊 |

---

## WebUI（图形界面）

WebUI 是**配套组件**，不是可选装饰 —— 审批卡片、反问卡、工具时间线、中期交互等能力只在 UI 里才完整。

```bash
cd agent_webui
webui.bat --setup        # 建 venv-gateway + 装后端依赖（只需一次）
webui.bat                # 启动网关并自动打开浏览器 → http://127.0.0.1:8900
```

浏览器打开后，点界面里的**绿色电源键**启动 agent（`--ab` 参数也能让 bat 直接代发开机请求）。

**能做什么：**

- **电源控制** —— 开机 / 优雅关机 / 强制关闭，状态机可见
- **聊天** —— 多会话侧栏、真实历史（含工具调用配对）、Markdown 渲染、流式输出
- **工具时间线** —— 每次调用的 `工具 / 参数 / 结果 / 耗时`，按编排器线程分组，并行调用一目了然
- **审批卡片** —— 危险操作（改系统盘、外发数据、读凭据、写项目外…）弹卡问你
- **反问卡（clarify）** —— agent 需要你补充信息时挂卡暂停回合，你答完它原地续跑（支持同批多卡）
- **中期交互** —— 回合跑动时你盯着时间线发现它跑偏，可以直接打字"用户交代"，随下一批工具返回进模型
- **工作区 / 语境面板** —— 会话文件、工作区目录、SOUL/AGENTS/MEMORY 加载情况快照

完整说明（启动/停止/开发模式/环境变量/故障排查）见 **[`agent_webui/README.md`](agent_webui/README.md)**。

---

## 回归测试

```bash
venv/Scripts/python -m pytest tests -q
```

**278 passed / 5 skipped，约 45 秒**，不需要 LLM、不需要网关、不写真实文件（全走临时目录）。

5 条 skip 中：2 条需要网关（真机探针，`AETHER_PROBE_LIVE=1` 时执行），
3 条依赖**工作区探针资产**（`agent_workspace/` 里的历史取证脚本）—— 初始化仓库不附带它们，
在完整开发仓库里会真跑。详见 [`tests/README.md`](tests/README.md)。

> ⚠️ 跑法必须带 `tests` 范围：根目录裸跑 `pytest` 会去收集 `agent_workspace/**` 下的独立探针
> （那些脚本模块级 `sys.exit()`，是**刻意**不纳入 pytest 的）。
> 测试覆盖地图与分层设计见 [`tests/README.md`](tests/README.md)。

---

## 项目结构

```
agent/                  主程序（agent.py 入口）+ 审批引擎 + 编排器 + 上下文管理器 + 日志
  approvals/            审批规范（一类一文件：系统盘 / 凭证 / 外发 / 项目外…）
agent_tools/            工具层（文件、搜索、网页、执行 shell / Python、浏览器、MCP 网关）
agent_webui/            WebUI：backend/（FastAPI 网关）+ frontend/（Vite + React + TS）
agent_memory/
  long_memory/          长期记忆 —— SOUL.md / AGENTS.md / MEMORY.md / USER.md（**首次启动时由引导填写**）
  working_memory/       会话存档（每会话一个 .json）
agent_skills/           技能库：一个文件夹 = 一个技能（含 SKILL_REGISTRY.md 自动注册表）
agent_MCP/              MCP 服务站：一个 server = 一个文件夹（含 MCP_REGISTRY.md）
agent_workspace/        agent 的工作区（任务按子文件夹隔离）
agent_knowledge_base/   RAG 知识库语料（.md）
agent_logs/             会话日志（.jsonl）
docs/                   设计文档、审计报告、接入教程、测试用例
tests/                  回归测试集
config.yaml             路径与默认值（LLM 三件套以 .env 为准，这里仅兜底）
.env.example            LLM 配置模板（复制为 .env 填写）
```

---

## 核心机制速览

| 机制 | 位置 | 一句话 |
|---|---|---|
| **语境快照冻结** | `agent/agent.py` | 系统提示在对话创建时组装一次并冻结；改 `.md` 要**新对话**才生效 |
| **审批引擎** | `agent/approval.py` + `agent/approvals/` | 危险操作按"动词-目标绑定"判定，弹卡给人裁决；规范一类一文件 |
| **任务编排器** | `agent/task_orchestrator.py` | 模板/管道复用，支持真并行；串行名单防止读-改-写竞态 |
| **上下文管理器** | `agent/context_manager.py` | 原文永不动，压缩只生产"视图"发给模型；保护最近 N 轮 |
| **技能系统** | `agent/skill_system.py` | 文件夹即技能，启动时自动同步注册表并注入目录快照（渐进式披露） |
| **MCP 集成** | `agent/mcp_client.py` + `agent/mcp_station.py` | 手写 stdio JSON-RPC 客户端；station 形态；工具 schema **不进**常驻上下文 |
| **中期交互** | `agent/mid_turn.py` | 回合运行中用户可追加交代，随下一批工具返回回灌模型 |
| **长期记忆** | `agent_memory/long_memory/` | SOUL / AGENTS / MEMORY 三个文件每轮注入；USER.md 作档案 |

设计文档见 [`docs/`](docs/)。

---

## 常见问题

**改了 `.env` 没生效？**
确认改的是项目根 `.env` 或 `agent/.env`（不是 `.env.example`），且 agent 已完全重启。
启动时会打印"已加载 .env"的路径与 LLM 摘要，用它核对。

**改了 `SOUL.md` / `AGENTS.md` / `MEMORY.md` 没生效？**
这是**预期行为** —— 语境快照冻结。**新开一个对话**即可，不用重启进程。

**换厂商后报 400 / 参数不支持？**
大概率是厂商不认 `thinking` 或 `reasoning_effort`：把 `.env` 里 `LLM_THINKING_ENABLED=false`、
`LLM_REASONING_EFFORT=off`（默认即如此，若改过请改回）。

**模型名怎么填？** 以厂商文档为准（智谱是 `glm-4.7-flash` 这种，硅基流动常带命名空间如 `Qwen/Qwen3-8B`）。

**WebUI 打不开 / 网关没起来？**
先 `webui.bat --setup` 建网关环境；端口默认 8900（用 `AETHER_WEBUI_PORT` 改）。
Windows 上若 `npm` 被 WSL 劫持（报 `wsl: <3>InternalError`），改用 `npm.cmd`。

**MCP 提示没有 token？**
`agent_MCP/github/` 需要 `GITHUB_PERSONAL_ACCESS_TOKEN`（写在 `.env`）。
没有 token 也能握手列工具，但一个都调不动 —— 步骤见 [`docs/MCP-GitHub接入.md`](docs/MCP-GitHub接入.md)。

**用脚本 / 管道喂输入跑 CLI，需要审批的操作全被拒？**
这是 **fail-closed 保护**，不是 bug：CLI 审批面板（`ConsolePort`）要求 stdin 是真实终端（TTY）。
管道、重定向、后台任务下判定为"无通道"，需要审批的操作**一律拒绝并记账**（账本里记 `how: no_channel`）。
要在自动化/无人值守场景里跑，请改用 **WebUI 网关**（审批卡片走 HTTP 通道），或让 stdin 接上真实终端。

**首次启动引导写到一半没写完？**
引导要写 4 个文件，涉及多次工具调用 + 可能触发审批。**建议在 WebUI 里做首次引导**（审批有卡片通道），
或确保 CLI 跑在真实终端里、给足时间（模型开了 thinking 时每轮响应较慢）。

---

## 许可

[MIT](LICENSE)
