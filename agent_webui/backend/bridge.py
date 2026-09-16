# -*- coding: utf-8 -*-
"""bridge.py —— AetherBreath 本体的子进程入口（可被强制 kill）。

启动方式（由 backend/agent_proc.py 拉起）：
    <项目根 venv>/python  agent_webui/backend/bridge.py
协议：
    stdout 首行 = JSON {"port": N, "pid": P}，之后 stdout 不再使用
    其余输出全部走 stderr（可自由 print，不污染协议）
    HTTP 仅监听 127.0.0.1:<随机端口>，所有请求需 X-Bridge-Token 校验

⚠️ 零侵入承诺：本文件不修改 agent/ 与 agent_tools/ 下任何文件。
所有能力扩展（ask_user 工具、工具事件上报）都通过「运行时对象引用可变」
这一 Python 语义在 bridge 自己的进程内完成：
    agent_tools.AVAILABLE_TOOLS  -> dict，追加键即对 agent 模块生效
    agent_tools.TOOLS_SCHEMA     -> list，追加元素即对 agent 模块生效
    agent._ORCHESTRATOR          -> 单例实例，可读引用
    agent.mid_turn.BOX           -> 模块级单例信箱，import 即与主循环共享（中期交互）
例外说明：中期交互的**注入点**是 agent.py 自己的一行 flush_after_tools()（主循环
主动接线，不是 bridge 去改它）；bridge 只提供 WebUI 独有的投递入口 /mid_turn。
CLI 模式（python agent/agent.py）不会 import 本文件，行为完全不变。
"""
from __future__ import annotations

import ctypes
import functools
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import uuid
_LAYER_SEQ = [0]
_LAYER_CUR = (None,)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

# ============================================================
# 0. 协议保护：先抢下真实 stdout，再把 stdout 让给 stderr
# ============================================================
_REAL_STDOUT = sys.stdout
try:
    _REAL_STDOUT.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.stdout = sys.stderr  # import 期 agent 内部 print 不再污染首行协议

# ============================================================
# 1. 路径装配
# ============================================================
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
WEBUI_DIR = os.path.dirname(BACKEND_DIR)
PROJECT_ROOT = os.path.dirname(WEBUI_DIR)
AGENT_DIR = os.path.join(PROJECT_ROOT, "agent")
for p in (PROJECT_ROOT, AGENT_DIR, BACKEND_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# 中期交互信箱（agent/mid_turn.py）：与 AB 主循环同进程共享的模块级单例，
# import 即共享，不需要任何跨进程通道。CLI 不 import 本文件 → 没有投递入口。
import mid_turn  # noqa: E402  （AGENT_DIR 已在上方入 path）

# clarify 多卡共享窗口（纯逻辑，可单测）：一批并行 ask_user 卡共享一条 deadline，
# 有人在答就往后续，连续 CLARIFY_TIMEOUT 秒没人答才算没人应答。
import clarify_batch  # noqa: E402

TOKEN = os.environ.get("AETHER_BRIDGE_TOKEN", "")
CLARIFY_TIMEOUT = int(os.environ.get("AETHER_CLARIFY_TIMEOUT", "120"))  # 主人回答的标准窗口
CLARIFY_TOOL = "ask_user"
CLARIFY_WINDOW = clarify_batch.WindowPool()   # 全局单批次（单回合，_TURN_GATE 保证）
MODE = os.environ.get("AETHER_AB_MODE", "")  # 扩展位：将来按 mode 换 persona/工具集

_EVENT_TYPES = {
    "turn_phase", "stage", "tool_begin", "tool_end", "progress",
    "text", "clarify_request", "approval_request", "approval_expired",
    "done", "error", "heartbeat",
}


# ============================================================
# 2. 事件总线：广播到所有 SSE 订阅者
# ============================================================
class EventBus:
    def __init__(self, maxsize: int = 2000) -> None:
        self._subs: List[queue.Queue] = []
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self.seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, etype: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self.seq += 1
        evt = dict(payload or {})  # 保留键后置，防止 payload 覆盖 type/seq/ts
        evt.update({"seq": self.seq, "type": etype, "ts": time.time()})
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                pass  # 慢消费者丢事件，绝不阻塞 agent 回合

    def to_stderr(self, etype: str, payload: Dict[str, Any]) -> None:
        try:
            print(f"[bridge-event] {etype} {json.dumps(payload, ensure_ascii=False)[:400]}",
                  file=sys.__stderr__, flush=True)
        except Exception:
            pass


BUS = EventBus()


# ============================================================
# 3. stdout 捕获：agent 的中期进度 print -> progress 事件
# ============================================================
class _BytesSink:
    """字节汇：接住 codecs.StreamWriter 编码后的字节，解码送回捕获器。

    存在的原因（实查）：agent_tools/multi_search.py 曾在 win32 上 import 时执行
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
    （2026-09-14 起它只在 stdout **没被宿主替换过**时才重包，所以本进程不会再触发；
      这里保留兼容写法，免得将来有人再引入同类 rewrap 时把捕获器写坏。）
    """

    def __init__(self, cap: "StdoutCapture") -> None:
        self._cap = cap

    def write(self, data) -> int:
        if isinstance(data, (bytes, bytearray)):
            self._cap._append(data.decode("utf-8", "replace"))
            return len(data)
        self._cap._append(str(data))
        return len(data)

    def flush(self) -> None:
        self._cap.flush()

    def writelines(self, lines) -> None:
        for ln in lines:
            self.write(ln)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        pass

    @property
    def closed(self) -> bool:
        return False


class StdoutCapture:
    """把 agent 内部 print 按行转成 progress 事件（无活动回合时落 stderr）。

    兼容性：实现 detach/buffer/writable 等流协议成员，
    以便被第三方模块 detach 再包装后仍能收回输出。
    """

    def __init__(self) -> None:
        self._buf = ""
        self._lock = threading.Lock()

    # ---- 流协议 ----
    def detach(self):
        return _BytesSink(self)

    @property
    def buffer(self):
        return _BytesSink(self)

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return False

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"

    def fileno(self) -> int:
        return sys.__stdout__.fileno()

    # ---- 写入 ----
    def write(self, s) -> int:
        if not s:
            return 0
        self._append(str(s))
        return len(s)

    def writelines(self, lines) -> None:
        for ln in lines:
            self._append(str(ln))

    def _append(self, text: str) -> None:
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit_line(line)

    def _emit_line(self, line: str) -> None:
        line = line.rstrip("\r")
        if not line.strip():
            return
        try:
            print(f"[ab] {line}", file=sys.__stderr__, flush=True)
        except Exception:
            pass
        ctx = current_run()
        if ctx is not None:
            BUS.emit("progress", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                                  "text": line.strip()[:2000]})

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self._emit_line(self._buf)
            self._buf = ""


# ============================================================
# 4. 摘要工具（时间线用，控制事件体积）
# ============================================================
def brief(obj: Any, limit: int = 700) -> str:
    try:
        if isinstance(obj, dict):
            parts = []
            for k, v in list(obj.items())[:12]:
                if isinstance(v, (str, int, float, bool)) or v is None:
                    sval = "" if v is None else str(v).replace("\n", " ")
                    if len(sval) > 120:
                        sval = sval[:120] + "…"
                    parts.append(f"{k}={sval}")
                elif isinstance(v, (list, tuple)):
                    sval = ", ".join(str(x) for x in v[:8])
                    if len(v) > 8:
                        sval += f", …(+{len(v) - 8})"
                    if not sval:
                        sval = "(空)"
                    if len(sval) > 160:
                        sval = sval[:160] + "…"
                    parts.append(f"{k}=[{sval}]")
                else:
                    parts.append(f"{k}=<{type(v).__name__}>")
            text = " ".join(parts)
        else:
            text = str(obj)
    except Exception as e:  # noqa: BLE001
        text = f"<摘要失败 {type(e).__name__}>"
    text = text.replace("\r", " ").replace("\n", " ")
    if len(text) > limit:
        text = text[:limit] + f"…(+{len(text) - limit}字)"
    return text


# ============================================================
# 5. 回合（run）管理
# ============================================================
class RunContext:
    def __init__(self, run_id: str, session_id: str, message: str) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.message = message
        self.thread_id: Optional[int] = None
        self.started_at = time.time()
        self.interrupt_requested = False
        self.tool_count = 0
        self.active_tools = 0
        self.lock = threading.Lock()
        self.result: Optional[Dict[str, Any]] = None


_RUNS: Dict[str, RunContext] = {}
_RUNS_LOCK = threading.RLock()  # 可重入：历史教训见 _stop_run（不可重入锁嵌套 acquire 会自死锁）
_TURN_GATE = threading.Lock()  # v1：同一 bridge 一次只跑一个回合（与 CLI 语义一致）

# 活动回合指针。工具跑在编排器的 worker 线程里（不是回合线程），
# 只按线程 ID 匹配会让 tool_begin/tool_end 全部丢失 —— 实查踩过这个坑。
# v1 单回合（_TURN_GATE 保证），故用全局指针归属即可。
_CURRENT: Optional["RunContext"] = None
_CURRENT_LOCK = threading.Lock()


def _set_current(ctx: Optional["RunContext"]) -> None:
    global _CURRENT
    with _CURRENT_LOCK:
        _CURRENT = ctx


def current_run() -> Optional[RunContext]:
    """归属当前事件应该挂到哪个回合。

    先按线程匹配（回合线程自身），否则取活动回合指针
    （编排器 worker 线程里的工具调用、以及工具内部的 print）。
    """
    tid = threading.get_ident()
    with _RUNS_LOCK:
        for ctx in _RUNS.values():
            if ctx.thread_id == tid:
                return ctx
    with _CURRENT_LOCK:
        return _CURRENT


def active_run() -> Optional[RunContext]:
    with _RUNS_LOCK:
        for ctx in _RUNS.values():
            if ctx.thread_id:
                return ctx
    return None


def _set_turn(ctx: RunContext, phase: str, **extra: Any) -> None:
    BUS.emit("turn_phase", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                            "phase": phase, **extra})


def _inject_keyboard_interrupt(tid: int) -> bool:
    """向指定线程抛 KeyboardInterrupt —— 让 agent 自己的 except 分支保存退出。

    这是不改 agent 源码的唯一正规停止通道：agent.py 的回合循环只在
    KeyboardInterrupt 时 save_session(status='interrupted')。
    线程若阻塞在 C 层（网络/锁），异常在下一个字节码边界生效。
    """
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(tid), ctypes.py_object(KeyboardInterrupt))
    if res == 0:
        return False
    if res > 1:  # 波及多个线程，撤销以免误伤
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        return False
    return True


# ============================================================
# 6. clarify 通道（ask_user 的真实实现）
# ============================================================
# ============================================================
# 5.5 编排器状态机上报（运行期包装，零改 agent/）
# ============================================================
# 为什么必须包装：ToolPipeline 是 _run_one 里的局部变量，编排器不留存清单，
# 外部既看不到 pending（排队中）也看不到层号；而界面要"和编排器对齐颗粒度"。
# 全部包在 try/except 里：观测面绝不允许影响工具执行本身。
_PIPES: Dict[str, Dict[str, Any]] = {}
_PIPE_ORDER: List[str] = []
_PIPE_LOCK = threading.Lock()
_PIPE_MAX = 240                      # 长跑进程里防累积：终态即删 + 兜底裁剪
_TLS = threading.local()             # 让被包装的工具函数能看到真实 tool_call.id
PATCH_STATE = {"tpl": False, "pipe": False, "batch": False}


def _pipe_emit(status: str, tc_id: str, tool: str, elapsed=None, layer=None) -> None:
    # 白名单外的未知状态一律降级为 failed：_PIPES 只认 done|failed|cancelled，
    # 透传未知值会让槽位永久挂在 pending 态（biz_failed 就是这种值）。
    if status not in ("pending", "running", "done", 
                    "failed", "cancelled"):
        status = "failed"
    try:
        ctx = active_run()
        with _PIPE_LOCK:
            if status in ("done", "failed", "cancelled"):
                _PIPES.pop(tc_id, None)
                if tc_id in _PIPE_ORDER:
                    _PIPE_ORDER.remove(tc_id)
            else:
                if tc_id not in _PIPES:
                    _PIPE_ORDER.append(tc_id)
                    for old in _PIPE_ORDER[:-_PIPE_MAX]:
                        _PIPES.pop(old, None)
                _PIPES[tc_id] = {"tool": tool, "status": status, "layer": layer}
        BUS.emit("pipeline", {
            "run_id": ctx.run_id if ctx else None,
            "session_id": ctx.session_id if ctx else None,
            "tc_id": tc_id, "tool": tool, "status": status,
            "layer": layer, "elapsed": elapsed,
            "thread": threading.current_thread().name,   # 槽位身份：orch-parallel_3 等
            "live": len(_PIPE_ORDER),
        })
    except Exception:
        pass


def install_orchestrator_watch(orch) -> Dict[str, bool]:
    """包装 ToolTemplate.create_pipeline / ToolPipeline.run / _run_on_pool。"""
    try:
        import task_orchestrator as tot
    except Exception:
        return PATCH_STATE
    try:
        tpl, pipe = tot.ToolTemplate, tot.ToolPipeline
    except AttributeError:
        return PATCH_STATE
    if not getattr(orch, "_abw_watch", None):
        if not getattr(tpl, "_abw_patched", False):
            orig_cp = tpl.create_pipeline

            def create_pipeline(self, tool_call):
                p = orig_cp(self, tool_call)
                _pipe_emit("pending", getattr(tool_call, "id", "?"), getattr(self, "name", "?"))
                return p

            tpl.create_pipeline = create_pipeline
            tpl._abw_patched = True
            PATCH_STATE["tpl"] = True
        if not getattr(pipe, "_abw_patched", False):
            orig_run = pipe.run

            def run(self):
                tc = getattr(self, "tool_call", None)
                tc_id = getattr(tc, "id", "?")
                tool = getattr(getattr(self, "template", None), "name", "?")
                _TLS.tc_id = tc_id
                lay = None
                try:
                    lay = _LAYER_CUR[0]
                except Exception:
                    lay = None
                _pipe_emit("running", tc_id, tool, layer=lay)
                try:
                    r = orig_run(self)
                    st = getattr(self, "status", None) or ("done" if getattr(r, "success", True) else "failed")
                    # 新版 orchestrator 会标 biz_failed；_pipe_emit 白名单只认 done|failed|cancelled，
                    # 未知状态会被当作活跃槽位挂住不放，所以必须映射（getattr 兜住旧版无此字段）
                    if st == "biz_failed" or getattr(r, "biz_fail", False):
                        st = "failed"
                    _pipe_emit(st, tc_id, tool, elapsed=round(self.elapsed, 3), layer=lay)
                    return r
                except BaseException as e:
                    _pipe_emit("failed", tc_id, tool, layer=lay)
                    raise
                finally:
                    _TLS.tc_id = None

            pipe.run = run
            pipe._abw_patched = True
            PATCH_STATE["pipe"] = True
    if not getattr(orch, "_abw_batch", False):
        orig_pool = orch._run_on_pool

        def _on_pool(pool, tasks, timeout=None):
            global _LAYER_CUR
            try:
                _LAYER_SEQ[0] += 1
                _LAYER_CUR = (_LAYER_SEQ[0],)
                label = "serial" if pool is getattr(orch, "_serial_pool", None) else "parallel"
            except Exception:
                pass
            return orig_pool(pool, tasks, timeout)

        orch._run_on_pool = _on_pool
        orch._abw_batch = True
        PATCH_STATE["batch"] = True
    orch._abw_watch = True
    return PATCH_STATE


def _pool_capacity(orch) -> Dict[str, Any]:
    """编排器池容量 + 线程名前缀 —— 前端据此预画固定槽位。

    关键：ThreadPoolExecutor 是懒起线程的，没用过的槽位在任何事件里都不会出现，
    所以"9 个常驻坑位"必须由容量显式告知，不能靠观察过的线程名倒推。
    """
    out = {"parallel": {"workers": 8, "prefix": "orch-parallel"},
           "serial": {"workers": 1, "prefix": "orch-serial"}}
    try:
        out["parallel"]["workers"] = int(getattr(orch, "max_workers", 8) or 8)
        out["serial"]["workers"] = int(getattr(orch, "serial_workers", 1) or 1)
        for key, pool in (("parallel", getattr(orch, "_parallel_pool", None)),
                          ("serial", getattr(orch, "_serial_pool", None))):
            pre = getattr(pool, "_thread_name_prefix", None)
            if pre:
                out[key]["prefix"] = str(pre)
            th = getattr(pool, "_threads", None)
            if th is not None:
                out[key]["started"] = len(th)
    except Exception:
        pass
    return out


def _install_clarify_window(orch) -> bool:
    """让 clarify 真的能等：编排器对【一批工具】共用一个 timeout（agent.py 里
    default_timeout=30），而 ask_user 是在 worker 线程里等主人回答 —— 30 秒一到
    future 被判超时，可它的线程仍在后台跑（future.cancel() 对已运行的任务无效），
    于是主人晚到的答复投给一个已被结算的等待者，等于静默吞掉。

    这里按批判定：本批含 ask_user 就把窗口抬到 CLARIFY_TIMEOUT + 10。
    +10 的余量是刻意的 —— 让 waiter 自己先超时并返回「主人没答」的正常结果，
    而不是让编排器抢先结算。零侵入：不碰 agent/ 源码，只在运行期包一层方法。
    """
    if orch is None or getattr(orch, "_abw_clarify_patch", False):
        return False
    orig = orch._run_on_pool

    def _patched(pool, tasks, timeout=None):
        try:
            # 数出本批有几张卡：N 张并行卡共享同一段等待时间，所以要按卡数放宽，
            # 否则用户答到第二、三张时批次已经被结算，答复投给死掉的等待者。
            n = sum(1 for tc in (tasks or []) if getattr(tc, "name", None) == CLARIFY_TOOL)
            if n:
                timeout = max(int(timeout or 0), clarify_batch.batch_timeout(n, CLARIFY_TIMEOUT))
        except Exception:
            pass
        return orig(pool, tasks, timeout)

    try:
        orch._run_on_pool = _patched
        setattr(orch, "_abw_clarify_patch", True)
        return True
    except Exception:
        return False


def _clarify_channel(question: str, options: List[str], kind: str,
                     timeout: Optional[int] = None) -> str:
    """问主人一个问题并阻塞等待答复。

    **支持同批并行多张卡**：一批卡的等待时间由 CLARIFY_WINDOW 统一管理 ——
    有人作答就往后续窗，不会出现"人还在一张张答，第二三张卡已经到点"的静默吞答复。
    每张卡仍各有一个 ask_id / waiter / box，互不干扰；谁的答复回给谁。
    """
    ctx = active_run()
    ask_id = uuid.uuid4().hex[:12]
    waiter = threading.Event()
    box: Dict[str, Any] = {"answer": None}
    window = int(timeout or CLARIFY_TIMEOUT)
    batch = CLARIFY_WINDOW.join(window)       # 加入（或开启）本批共享窗口

    with _CLARIFY_LOCK:
        # 与审批同一套道理：提问也得在服务端留一份，主人刷新/切会话才拿得回来。
        _CLARIFY[ask_id] = {"waiter": waiter, "box": box, "ask_id": ask_id,
                            "question": question, "options": list(options or []),
                            "mode": kind, "timeout": window,
                            "born": time.time(),
                            "batch_id": batch["batch_id"], "batch_size": batch["size"],
                            "run_id": ctx.run_id if ctx else None,
                            "session_id": ctx.session_id if ctx else None}

    if ctx:
        _set_turn(ctx, "CLARIFY_WAIT")
    BUS.emit("clarify_request", {
        "run_id": ctx.run_id if ctx else None,
        "session_id": ctx.session_id if ctx else None,
        "ask_id": ask_id, "question": question, "options": options,
        "mode": kind, "timeout": window,
        # 批次信息：前端要显示「共 N 问 · 还有 M 个待答」，倒计时也用共享窗口的真值
        "batch_id": batch["batch_id"], "batch_size": batch["size"],
        "batch_live": batch["live"], "remaining": int(batch["remaining"]),
    })

    got = False
    try:
        # 等**共享窗口**，而不是各卡各算一个固定秒数：后者在并行多卡时，
        # 主人答第一张的时间里，后面几张就已经被自己的计时判死了。
        while True:
            rem = CLARIFY_WINDOW.remaining()
            if rem <= 0:
                break
            if waiter.wait(min(rem, 0.5)):
                got = True
                break
    finally:
        with _CLARIFY_LOCK:
            _CLARIFY.pop(ask_id, None)
            left = sum(1 for x in _CLARIFY.values()
                       if x.get("run_id") == (ctx.run_id if ctx else None))
        CLARIFY_WINDOW.leave()
        # 只有本回合一张卡都不剩了，才把回合状态交还主循环 ——
        # 否则界面上还有卡挂着，状态却已显示「思考中」。
        if ctx and not left:
            _set_turn(ctx, "THINKING")
    if not got:
        return "⏱️ 主人在限定时间内没有回答。请基于合理假设继续，并在回复里说明你做了什么假设。"
    answer = box.get("answer") or ""
    if answer.startswith("__CANCEL__"):
        return "主人取消了这个回合，请停止后续动作并简短收尾。"
    return answer


def _cancel_all_clarify(reason: str = "stopped") -> int:
    """让所有挂着的卡立即出局（回合被终止、回合收尾时用）。

    不这么做：主人点了停止，卡还留在界面上，worker 线程还要把整个窗口等满 ——
    而那些等待者永远等不到人（回合已经结束了），卡会在屏幕上多挂几分钟。
    """
    with _CLARIFY_LOCK:
        items = list(_CLARIFY.values())
    for it in items:
        box, waiter = it.get("box"), it.get("waiter")
        if box is None or waiter is None:
            continue
        box["answer"] = "__CANCEL__ %s" % reason
        waiter.set()
    if items:
        CLARIFY_WINDOW.reset()
    return len(items)


_CLARIFY: Dict[str, Any] = {}
_CLARIFY_LOCK = threading.Lock()


# ============================================================
# 7. 零侵入注入：ask_user 工具 + 工具事件包装
# ============================================================
_agent = None          # agent 模块
_ToolCall = None
_injected: List[str] = []
ORCH_STATE: Dict[str, Any] = {"value": "pending"}
# 审批系统状态：注册结果 + 自检探针结论，供 /agent/status 与前端徽标查询
APPROVAL_STATE: Dict[str, Any] = {"value": "pending"}


# ============================================================
# 7.05 token 计量：直接从 API 返回值读 usage（零改 agent.py 源码）
# client 是 agent.py 的模块级对象，替换它绑定的 create 方法即对本进程内
# 所有调用生效。stream=False（compose_chat_kwargs 写死），故 response.usage
# 一定带在对象上，不需要 stream_options。
# ============================================================
USAGE: Dict[str, Any] = {
    "total": {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0, "total": 0},
    "by_sid": {},
}
USAGE_LOCK = threading.Lock()


def _usage_node() -> Dict[str, int]:
    # last_prompt = 最近一次请求的输入 token（= 当前上下文占用，压缩后会回落）；
    # cached = 命中前缀缓存的输入 token（厂商不返回则恒为 0）
    return {"calls": 0, "prompt": 0, "completion": 0, "reasoning": 0, "total": 0,
            "cached": 0, "last_prompt": 0}


def _pick_cached(u, extra) -> int:
    """命中缓存的输入 token：各家常放在 prompt_tokens_details.cached_tokens /
    prompt_cache_hit_tokens 等字段里，逐个探，拿不到就 0（前端显示 —）。"""
    cands = []
    for src in (u, extra or {}):
        if isinstance(src, dict):
            for key in ("prompt_tokens_details", "input_tokens_details"):
                cands.append(src.get(key))
        else:
            cands.append(getattr(src, "prompt_tokens_details", None))
    for det in cands:
        for key in ("cached_tokens", "cache_hit_tokens", "prompt_cache_hit_tokens"):
            try:
                v = int((det.get(key) if isinstance(det, dict) else getattr(det, key, 0)) or 0)
            except (TypeError, ValueError, AttributeError):
                v = 0
            if v > 0:
                return v
    return 0


_USAGE_CORE = {"prompt_tokens", "completion_tokens", "total_tokens",
               "completion_tokens_details", "prompt_tokens_details"}


def _norm_usage(resp):
    """把各家 OpenAI 兼容端点的 usage 归一成一套字段（纯函数，可离线单测）。

    返回 None 表示这次拿不到量；拿到则给 prompt/completion/total/reasoning/model/extra。
    SDK 的 CompletionUsage 是 extra=allow，厂商私有字段不会丢（都在 model_extra 里），
    一并回传 extra，以后适配新厂商时可直接看原始值。
    """
    if resp is None:
        return None
    u = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
    if u is None:
        return None

    if isinstance(u, dict):
        extra = {k: v for k, v in u.items() if k not in _USAGE_CORE}
    else:
        extra = dict(getattr(u, "model_extra", None) or {})

    def pick(names):
        for nm in names:
            try:
                v = u.get(nm) if isinstance(u, dict) else getattr(u, nm, None)
            except Exception:
                continue
            if v is None:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv > 0:
                return iv
        return None

    # 命名差异：OpenAI / DeepSeek / 智谱 / Kimi / Ollama-compat 走 *_tokens；
    # Anthropic 原生与部分转发端点走 input_tokens / output_tokens。
    prompt = pick(["prompt_tokens", "input_tokens"]) or 0
    completion = pick(["completion_tokens", "output_tokens"]) or 0
    total = pick(["total_tokens"]) or 0
    if total <= 0:
        total = prompt + completion

    # 思考链 token：按字段族逐个试，命中即止；再兜底扫一遍私有字段里的 *_details。
    if isinstance(u, dict):
        details = [u.get("completion_tokens_details"), u.get("output_tokens_details"),
                   u.get("reasoning_details")]
    else:
        details = [getattr(u, "completion_tokens_details", None),
                   getattr(u, "output_tokens_details", None),
                   getattr(u, "reasoning_details", None)]
    reasoning = 0
    for det in details:
        rv = 0
        if isinstance(det, dict):
            rv = det.get("reasoning_tokens")
        elif isinstance(det, (int, float)):
            rv = det
        elif det is not None:
            rv = getattr(det, "reasoning_tokens", None)
        try:
            rv = int(rv or 0)
        except (TypeError, ValueError):
            rv = 0
        if rv > 0:
            reasoning = rv
            break
    if reasoning == 0:
        for kv in (extra or {}).values():
            if not isinstance(kv, dict):
                continue
            try:
                rv = int(kv.get("reasoning_tokens") or 0)
            except (TypeError, ValueError, AttributeError):
                rv = 0
            if rv > 0:
                reasoning = rv
                break

    model = resp.get("model") if isinstance(resp, dict) else getattr(resp, "model", None)
    if not isinstance(model, str) or not model:
        mv = (extra or {}).get("model")
        model = mv if isinstance(mv, str) else None

    return {"prompt": prompt, "completion": completion, "total": total,
            "reasoning": reasoning, "cached": _pick_cached(u, extra),
            "model": model, "extra": extra}


# ============================================================
# 7.06 计量账本：跨网关重启常驻
# 主人 2026-09-13 要求：概况与上下文占比「任何情况下都在」，不必先发一条消息才有数。
# 做法：每次采到 usage 后落盘，bridge 启动时载入继续累加；前端用 hello 里的账本校准。
# 口径与概况卡一致（只覆盖 WebUI 回合）；文件损坏/写失败一律降级为空账本，绝不拖垮回合。
# ============================================================
LEDGER_PATH = os.path.join(
    os.environ.get("AETHER_USAGE_LEDGER") or os.path.join(PROJECT_ROOT, "agent_webui", ".state"),
    "usage_ledger.json",
)


def _load_ledger() -> str:
    """载入上次的计量（重启后接着累计，而不是从 0 起）。"""
    try:
        if not os.path.exists(LEDGER_PATH):
            return "empty"
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with USAGE_LOCK:
            for sid, node in ((data or {}).get("by_sid") or {}).items():
                if not isinstance(node, dict):
                    continue
                base = _usage_node()
                for k in list(base):
                    try:
                        base[k] = int(node.get(k) or 0)
                    except (TypeError, ValueError):
                        base[k] = 0
                USAGE["by_sid"][str(sid)] = base
            tot = (data or {}).get("total") or {}
            for k in list(USAGE["total"]):
                try:
                    USAGE["total"][k] = int(tot.get(k) or 0)
                except (TypeError, ValueError):
                    USAGE["total"][k] = 0
            n = len(USAGE["by_sid"])
        return "loaded(%d 会话)" % n
    except Exception as e:
        return "failed: %s: %s" % (e.__class__.__name__, e)


def _save_ledger() -> None:
    """原子落盘。每轮调用一次（几 KB），失败只忽略。"""
    try:
        with USAGE_LOCK:
            snap = {"total": dict(USAGE["total"]),
                    "by_sid": {k: dict(v) for k, v in USAGE["by_sid"].items()}}
        d = os.path.dirname(LEDGER_PATH)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEDGER_PATH)
    except Exception:
        pass


def _ledger_snapshot() -> Dict[str, Any]:
    with USAGE_LOCK:
        return {"total": dict(USAGE["total"]),
                "by_sid": {k: dict(v) for k, v in USAGE["by_sid"].items()}}


def _install_usage_watch(ab_agent) -> str:
    """包装 client.chat.completions.create，把每次 API 返回的 usage 发成 usage 事件。"""
    client = getattr(ab_agent, "client", None)
    comp = getattr(getattr(client, "chat", None), "completions", None)
    orig = getattr(comp, "create", None)
    if orig is None or getattr(orig, "_ab_webui_usage", False):
        return "failed"

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        resp = orig(*args, **kwargs)
        try:
            if str(kwargs.get("stream")).lower() == "true":
                return resp        # 流式无同步 usage，放弃计量（当前 agent 写死 stream=False）
            n = _norm_usage(resp)
            if n is None:
                return resp
            prompt, completion = n["prompt"], n["completion"]
            total, reasoning = n["total"], n["reasoning"]
            cached = int(n.get("cached") or 0)
            ctx = current_run()          # 工具跑在别的线程，这里一定是回合线程
            sid = ctx.session_id if ctx else None
            with USAGE_LOCK:
                targets = [USAGE["total"]]
                if sid:
                    targets.append(USAGE["by_sid"].setdefault(sid, _usage_node()))
                for node in targets:
                    node["calls"] += 1
                    node["prompt"] += prompt
                    node["completion"] += completion
                    node["reasoning"] += reasoning
                    node["total"] += total
                    node["cached"] = node.get("cached", 0) + cached
                    node["last_prompt"] = prompt      # 当前上下文占用（压缩后回落）
                tot = USAGE["total"]
                sess = dict(USAGE["by_sid"].get(sid)) if sid else None
            thin_extra = {k: v for k, v in (n["extra"] or {}).items()
                          if isinstance(v, (int, float, str, bool))} or None
            BUS.emit("usage", {
                "run_id": ctx.run_id if ctx else None,
                "session_id": sid,
                "call": {"prompt": prompt, "completion": completion,
                         "total": total, "reasoning": reasoning, "cached": cached},
                "session": sess,
                "global": dict(tot),
                "model": n["model"] or kwargs.get("model"),
                "extra": thin_extra,
            })
            _save_ledger()               # 计量账本落盘：下次重启接着累计
        except BaseException as e:       # 计量绝不允许拖垮回合
            try:
                print("[bridge] usage 采集失败: " + type(e).__name__ + ": " + str(e),
                      file=sys.__stderr__, flush=True)
            except Exception:
                pass
        return resp

    wrapper._ab_webui_usage = True
    comp.create = wrapper
    return "on"



# 业务失败判定（2026-09-09 加）。部分工具（file_read / execute_shell / execute_python / rag /
# create_tool 共 27 处）失败时是【返回以 ❌ 开头的字符串】而不是抛异常。落盘还原
# （sessions.py:132 的 startswith(❌)）认得这种失败，而实时链路只看异常 —— 于是同一批调用
# 实时显示成功、切走再切回显示失败。这里补上，两条链口径对齐。
#
# 戒律（上次事故：引用了未进模块作用域的名字，把全部工具的返回值换成了 NameError）：
#   1) 判定函数与调用点同在 bridge.py 模块作用域，绝不跨模块取名字、不用函数内 import；
#   2) 判定永不抛出：出错就退回原行为（ok=True），显示层绝不反噬工具本身。
BIZ_FAIL_PREFIX = "❌"

def biz_failed(result) -> bool:
    try:
        return isinstance(result, str) and result.lstrip().startswith(BIZ_FAIL_PREFIX)
    except Exception:
        return False

def _wrap_tool(name: str, fn):
    """包装工具函数：调用前后发 tool_begin / tool_end 事件。

    用 functools.wraps 保留 __wrapped__，编排器的 inspect.signature()
    会跟随到原函数 -> logger 自动注入逻辑完全不受影响。
    异常按原对象重抛（保留类型与 traceback），事件里再记录摘要。
    """
    @functools.wraps(fn)
    def inner(**kwargs):
        ctx = current_run()
        # 优先用编排器的真实 tool_call.id（由被包的 ToolPipeline.run 放进 thread-local），
        # 这样 pipeline 事件与 tool_begin/tool_end 指向同一次调用，能拼到同一条管道上。
        call_id = getattr(_TLS, "tc_id", None) or uuid.uuid4().hex[:8]
        idx = 0
        thread_name = threading.current_thread().name
        started = time.time()
        if ctx is not None:
            with ctx.lock:
                ctx.tool_count += 1
                ctx.active_tools += 1
                idx = ctx.tool_count
            BUS.emit("tool_begin", {
                "run_id": ctx.run_id, "session_id": ctx.session_id,
                "call_id": call_id, "tool": name, "index": idx,
                "thread": thread_name,
                "args": brief({k: v for k, v in kwargs.items() if k != "logger"}, 900),
            })
            _set_turn(ctx, "TOOL_RUNNING")
        result, exc = None, None
        try:
            result = fn(**kwargs)
        except BaseException as e:  # noqa: BLE001 - 记录后原样重抛
            exc = e
        finally:
            elapsed = time.time() - started
            if ctx is not None:
                with ctx.lock:
                    ctx.active_tools = max(0, ctx.active_tools - 1)
                    still = ctx.active_tools
                BUS.emit("tool_end", {
                    "run_id": ctx.run_id, "session_id": ctx.session_id,
                    "call_id": call_id, "tool": name, "index": idx,
                    "thread": thread_name,
                    "ok": (exc is None) and not biz_failed(result),   # 业务失败也算失败，与落盘还原同口径
                    "biz_fail": (exc is None) and biz_failed(result),
                    "result": None if exc else brief(result, 900),
                    "error": None if exc is None else f"{type(exc).__name__}: {exc}",
                })
                if still == 0:
                    _set_turn(ctx, "THINKING")
        if exc is not None:
            raise exc
        return result

    return inner


def _wrap_pending_tools() -> int:
    """**启动期**全量包装（幂等）：把 AVAILABLE_TOOLS 里还没包装的工具补上，返回补了几个。

    界面事件通道（tool_begin / tool_end）靠它 —— 工具不包这一次，界面上就是隐形的。
    只在 `_install_runtime()` 里调一次（启动期）：v2 起 MCP 工具不再进常驻工具表、
    也没有"运行期热注册"这回事，所以不再需要每回合兜底（那套兜底是给已删的热注册钩子用的）。
    """
    import agent_tools                            # noqa: PLC0415
    n = 0
    for tool_name in list(agent_tools.AVAILABLE_TOOLS.keys()):
        raw = agent_tools.AVAILABLE_TOOLS[tool_name]
        if getattr(raw, "_ab_webui_wrapped", False):
            continue
        try:
            wrapped = _wrap_tool(tool_name, raw)
            wrapped._ab_webui_wrapped = True
            agent_tools.AVAILABLE_TOOLS[tool_name] = wrapped
            n += 1
        except Exception as e:
            print("[bridge] 包装工具失败 %s: %s: %s" % (tool_name, type(e).__name__, e),
                  file=sys.__stderr__, flush=True)
    return n


def _install_runtime() -> None:
    """import agent 并注入 ask_user + 事件包装。返回注入摘要。"""
    global _agent, _ToolCall, _injected
    import agent as ab_agent                      # noqa: PLC0415  AB 本体
    from task_orchestrator import ToolCall        # noqa: PLC0415
    import agent_tools                            # noqa: PLC0415
    import ask_user_tool                          # noqa: PLC0415

    _agent = ab_agent
    _ToolCall = ToolCall
    ask_user_tool.set_channel(_clarify_channel)

    # 7.05 token 计量（patch 模块级 client，不动 agent 源码）
    USAGE_WATCH_STATE = _install_usage_watch(ab_agent)
    if USAGE_WATCH_STATE == "on":
        _injected.append("usage_watch")

    # 计量账本：重启后接着上次累计（概况/占比常驻，不必先发消息）
    LEDGER_STATE = _load_ledger()
    if LEDGER_STATE.startswith("loaded"):
        _injected.append("usage_ledger")
    print("[bridge] 计量账本: " + LEDGER_STATE, file=sys.__stderr__, flush=True)

    # 7.1 事件包装（**启动期一次性全量**）：替换 dict 的 value（同一 dict 对象 -> agent 模块可见），
    #     让每次工具执行都发得出 tool_begin / tool_end（界面事件通道）。
    _wrap_pending_tools()

    # 7.2 注册 ask_user（dict 追加 + list 追加，均不改源文件）
    if "ask_user" not in agent_tools.AVAILABLE_TOOLS:
        wrapped_ask = _wrap_tool("ask_user", ask_user_tool.ask_user)
        wrapped_ask._ab_webui_wrapped = True
        agent_tools.AVAILABLE_TOOLS["ask_user"] = wrapped_ask
        agent_tools.TOOLS_SCHEMA.append(ask_user_tool.ask_user_schema)
        _injected.append("ask_user")

    # 7.4 审批系统：把 WebUI 通道注册进 agent/approval.py（宿主注入，本体零改动）
    # 判定规则全在 approval.py，这里只负责送达与等待，所以 CLI 与 WebUI 同源。
    try:
        import approval as ab_approval                    # noqa: PLC0415
        import approval_adapter                           # noqa: PLC0415
        approval_adapter.set_emit(BUS.emit)
        ab_approval.set_port(approval_adapter.WebUIPort())
        APPROVAL_STATE["value"] = ab_approval.self_check(_tool_names())
        _injected.append("approval_channel")
    except Exception as e:
        APPROVAL_STATE["value"] = {"ok": False,
                                   "error": "%s: %s" % (e.__class__.__name__, e)}
        print("[bridge] approval channel init failed; system-drive ops denied: %s"
              % APPROVAL_STATE["value"].get("error"),
              file=sys.__stderr__, flush=True)

    # 7.6 中期交互：信箱的阶段回调 → SSE 事件。
    # agent/ 侧只管「收件 + 注入」，通道与呈现全归 WebUI —— 所以这里注入出口，
    # 而不是让 mid_turn 去 import 任何 WebUI 模块（CLI 下没有观察者，一切照常）。
    try:
        mid_turn.BOX.set_observer(lambda _stage, payload: BUS.emit("mid_turn", payload))
        _injected.append("mid_turn")
    except Exception as e:
        print("[bridge] mid-turn channel init failed: %s: %s"
              % (e.__class__.__name__, e), file=sys.__stderr__, flush=True)

    # 7.3 【主线程】预建常驻编排器。
    # 实查发现的硬约束：TaskOrchestrator.__init__ -> _register_signal_handlers()
    # 调用 signal.signal()，只能在主线程执行。若留给回合线程首次创建，
    # 必然抛 ValueError: signal only works in main thread of the main interpreter。
    # 这里在主线程建好单例，回合线程直接复用（agent._ORCHESTRATOR 非 None）。
    try:
        orch = ab_agent._get_orchestrator()
        _orch_state = "ready"
        # 不放宽这一处，clarify 就会被编排器的 30s 批次上限掐断（晚到的答复没人认领）
        if _install_clarify_window(orch):
            _orch_state = f"ready+wait{CLARIFY_TIMEOUT + 10}s"
        # 状态机上报：成功几项就写几项，界面据此区分「真状态」与「推导模式」
        try:
            st = install_orchestrator_watch(orch)
            on = [k for k, v in st.items() if v]
            if on:
                _orch_state += "+watch(" + ",".join(sorted(on)) + ")"
        except Exception as e:
            print(f"[bridge] 状态机监视安装失败（界面将回落推导模式）: {type(e).__name__}: {e}",
                  file=sys.__stderr__, flush=True)
    except ValueError as e:
        _orch_state = f"failed: {e}"
        print(f"[bridge] 编排器主线程预建失败: {e}", file=sys.__stderr__, flush=True)
    ORCH_STATE["value"] = _orch_state

    return {
        "orchestrator": _orch_state,
        "pools": _pool_capacity(getattr(ab_agent, "_ORCHESTRATOR", None)),
        "tools": sorted(agent_tools.AVAILABLE_TOOLS.keys()),
        "injected": list(_injected),
        "model": getattr(ab_agent, "MODEL_NAME", "?"),
        "max_iterations": getattr(ab_agent, "MAX_ITERATIONS", "?"),
        "project_root": str(ab_agent.PROJECT_ROOT),
    }


# ============================================================
# 8. 回合执行
# ============================================================
def _build_messages(session_id: str, user_message: str, log):
    """复刻 agent.main() 的语境快照冻结逻辑（不改 agent 源码）。"""
    history, snapshot = _agent.load_session(session_id, log)
    if snapshot is not None:
        system_prompt = snapshot
        frozen = "reuse"
    else:
        system_prompt, _inj = _agent.load_system_prompt(log)
        frozen = "fresh"
    msgs = [{"role": "system", "content": system_prompt}] + list(history)
    return msgs, len(history), frozen


def _execute_turn(ctx: RunContext) -> None:
    ctx.thread_id = threading.get_ident()
    _set_current(ctx)
    _LAYER_SEQ[0] = 0                      # 层号按回合计，避免越跑越大
    with _PIPE_LOCK:
        _PIPES.clear()
        _PIPE_ORDER.clear()
    log = None
    try:
        from logger import SessionLogger  # noqa: PLC0415
        log = SessionLogger(ctx.session_id)
        log.info("[WebUI] 回合开始", run_id=ctx.run_id, mode=MODE or "default")

        messages, history_count, frozen = _build_messages(ctx.session_id, ctx.message, log)
        BUS.emit("stage", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                           "stage": "context_ready", "history_count": history_count,
                           "snapshot": frozen, "model": getattr(_agent, "MODEL_NAME", "?")})
        messages.append({"role": "user", "content": ctx.message})
        _set_turn(ctx, "THINKING")

        result = _agent.call_agent_with_tools(messages, ctx.session_id, log)
        ctx.result = result

        interrupted = bool(result.get("interrupted"))
        if "error" in result and not interrupted:
            _set_turn(ctx, "ERROR")
            BUS.emit("error", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                               "message": str(result["error"])[:2000],
                               "iterations": result.get("iterations")})
        else:
            _set_turn(ctx, "RESPONDING")
            content = result.get("content") or ""
            if content:
                BUS.emit("text", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                                  "text": content})
            _set_turn(ctx, "INTERRUPTED" if interrupted else "IDLE")
            BUS.emit("done", {
                "run_id": ctx.run_id, "session_id": ctx.session_id,
                "content": content,
                "iterations": result.get("iterations"),
                "tool_calls": ctx.tool_count,
                "elapsed": round(time.time() - ctx.started_at, 2),
                "interrupted": interrupted,
            })
    except BaseException as e:  # noqa: BLE001 - 回合兜底，绝不带崩进程
        msg = f"{type(e).__name__}: {e}"
        try:
            import traceback
            print("回合异常:\n" + traceback.format_exc(), file=sys.__stderr__, flush=True)
        except Exception:
            pass
        _set_turn(ctx, "ERROR")
        BUS.emit("error", {"run_id": ctx.run_id, "session_id": ctx.session_id,
                           "message": msg[:2000]})
    finally:
        # 中期交互：过了本回合还没送出去的交代一律作废，并明确告诉主人 ——
        # 留着它会在下个回合诈尸，让模型执行一个早已不成立的要求。
        try:
            left = mid_turn.BOX.discard(ctx.session_id)
            if left:
                BUS.emit("mid_turn", {
                    "mid": "dropped", "run_id": ctx.run_id, "session_id": ctx.session_id,
                    "ids": [it["id"] for it in left], "count": len(left),
                    "texts": [it["text"] for it in left], "reason": "turn-ended",
                })
        except Exception:
            pass
        # 回合收尾兜底：还挂着的提问卡立刻出局（正常情况下批次结束时它们已各自收尾）
        try:
            _cancel_all_clarify("turn-ended")
        except Exception:
            pass
        _set_current(None)
        with _RUNS_LOCK:
            ctx.thread_id = None
        _TURN_GATE.release()


def _start_run(session_id: str, message: str) -> Dict[str, Any]:
    # done 事件先于门闩释放（finally 里才放），故给一个短宽限，
    # 避免"上一回合刚结束就立刻追发"被误判 busy。
    if not _TURN_GATE.acquire(blocking=True, timeout=3.0):
        return {"ok": False, "busy": True,
                "error": "当前回合仍在进行，请等待或先停止"}
    ctx = RunContext(uuid.uuid4().hex[:12], session_id, message)
    with _RUNS_LOCK:
        _RUNS[ctx.run_id] = ctx
    t = threading.Thread(target=_execute_turn, args=(ctx,),
                         name=f"ab-turn-{ctx.run_id}", daemon=True)
    t.start()
    return {"ok": True, "run_id": ctx.run_id, "session_id": session_id}


def _stop_run(run_id: Optional[str]) -> Dict[str, Any]:
    try:                                    # 停止时清掉挂起的审批等待者，别留孤儿
        import approval_adapter             # noqa: PLC0415
        approval_adapter.cancel_all("stopped")
    except Exception:
        pass
    try:                                    # 提问卡同理：立刻出局，别在屏幕上多挂几分钟
        n_clar = _cancel_all_clarify("stopped")
        if n_clar:
            BUS.emit("clarify_resolved", {"reason": "stopped", "count": n_clar})
    except Exception:
        pass
    ctx = None
    # 注意：active_run()/current_run() 内部同样取 _RUNS_LOCK，
    # 绝不可在持有该锁时调用它们（曾经的自死锁会让 /stop 与 /health 永久无响应）。
    with _RUNS_LOCK:
        if run_id:
            ctx = _RUNS.get(run_id)
            if ctx is None:  # 指定了 run_id 却查不到：绝不回落到别人的回合
                return {"ok": False, "stopped": False, "reason": "run-not-found",
                        "error": f"未找到回合 {run_id}"}
    if ctx is None:
        ctx = active_run()
    if ctx is None or ctx.thread_id is None:
        return {"ok": True, "stopped": False, "reason": "no-active-run"}
    ctx.interrupt_requested = True
    _set_turn(ctx, "INTERRUPTED")
    hit = _inject_keyboard_interrupt(ctx.thread_id)
    return {"ok": True, "stopped": hit, "run_id": ctx.run_id,
            "note": None if hit else "线程阻塞在原生调用中，中断将在其返回后的边界生效"}


def _mid_turn(session_id: str, text: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    """回合运行中追加「用户交代」：只投给当前活动回合，绝不新起回合、绝不落盘成新发言。

    三道校验（任一不成立就如实拒绝，不做「猜主人想投给谁」的兜底）：
      1. 确实有活动回合 —— 没有的话请直接发消息，这条路不该接；
      2. 给了 run_id 就必须命中该回合 —— 防「打错目标，话被投给下一个回合」；
      3. 会话必须与活动回合一致 —— 防在旁观会话里发交代、话却进了别人的语境。

    投递成功后由信箱的观察者广播 mid_turn:accepted（前端据此把消息落座，
    并拿 item_id 跟踪它后来是「已注入」还是「回合结束被作废」）。
    """
    body = str(text or "").strip()
    if not body:
        return {"ok": False, "error": "交代内容不能为空"}
    if len(body) > mid_turn.MAX_TEXT:
        return {"ok": False, "error": "交代过长（%d 字符，上限 %d）" % (len(body), mid_turn.MAX_TEXT)}
    ctx = active_run()                     # 注意：绝不在持有 _RUNS_LOCK 时调用它
    if ctx is None or ctx.thread_id is None:
        return {"ok": False, "reason": "no-active-run",
                "error": "当前没有正在跑的回合，直接发消息即可"}
    if run_id and run_id != ctx.run_id:
        return {"ok": False, "reason": "run-not-found",
                "error": "那一回合已经结束了，请直接发消息"}
    sid = str(session_id or "").strip()
    if sid and sid != ctx.session_id:
        return {"ok": False, "reason": "session-mismatch",
                "error": "AB 正在跑会话 %s，本会话的交代无处可投" % ctx.session_id}
    item = mid_turn.BOX.push(ctx.session_id, body, origin="webui", run_id=ctx.run_id)
    if item is None:
        return {"ok": False, "reason": "queue-full",
                "error": "待送达的交代已达上限（%d 条），等这批工具返回再发" % mid_turn.MAX_PENDING}
    return {"ok": True, "run_id": ctx.run_id, "session_id": ctx.session_id,
            "item_id": item["id"], "pending": item.get("pending", 1)}


def _clarify_pending() -> list:
    """当前真挂着的 clarify 提问（ask_user）。超时到点的不再报，免得复活僵尸卡。

    剩余时间一律按**批次共享窗口**算（不再逐卡 born+timeout）：主人正在一张张答，
    先答的那张留下的旧计时不该让后面的卡提前消失。
    """
    with _CLARIFY_LOCK:
        items = list(_CLARIFY.values())
    rem = int(CLARIFY_WINDOW.remaining())
    live = CLARIFY_WINDOW.live()
    out = []
    for it in items:
        if it.get("box", {}).get("answer") is not None:
            continue
        if rem <= 0:
            continue
        out.append({"type": "clarify_request", "ask_id": it.get("ask_id"),
                    "run_id": it.get("run_id"), "session_id": it.get("session_id"),
                    "question": it.get("question"), "options": it.get("options"),
                    "mode": it.get("mode"), "timeout": it.get("timeout"),
                    "batch_id": it.get("batch_id"), "batch_size": it.get("batch_size"),
                    "batch_live": live, "remaining": rem, "restored": True})
    return out


def _answer_clarify(ask_id: str, answer: str) -> Dict[str, Any]:
    with _CLARIFY_LOCK:
        item = _CLARIFY.get(ask_id)
    if not item:
        return {"ok": False, "error": "该提问已结束或超时"}
    waiter, box = item.get("waiter"), item.get("box")
    if waiter is None or box is None:
        return {"ok": False, "error": "提问记录不完整，已按失效处理"}
    box["answer"] = answer
    waiter.set()
    # 有人在答 → 把本批共享窗口往后续：主人正一张张回答，剩下的卡不该被判超时
    win = CLARIFY_WINDOW.touch()
    return {"ok": True, "ask_id": ask_id,
            "batch_live": CLARIFY_WINDOW.live(), "remaining": int(win.get("remaining") or 0)}


# ============================================================
# 9. HTTP 服务（127.0.0.1 随机端口 + token）
# ============================================================
class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "AetherBridge/1.0"
    protocol_version = "HTTP/1.1"

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        got = self.headers.get("X-Bridge-Token") or (
            self.path.split("token=", 1)[1].split("&", 1)[0] if "token=" in self.path else "")
        return got == TOKEN

    def _json(self, code: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "bad token"})
        path = self.path.split("?", 1)[0]
        if path == "/health":
            ctx = active_run()
            return self._json(200, {
                "ok": True, "pid": os.getpid(), "mode": MODE or "default",
                "phase": "RUNNING" if ctx else "IDLE",
                "run_id": ctx.run_id if ctx else None,
                "session_id": ctx.session_id if ctx else None,
                "model": getattr(_agent, "MODEL_NAME", "?") if _agent else None,
                "tools": len(_tool_names()), "clarify_pending": len(_CLARIFY),
                "approval": APPROVAL_STATE.get("value"),
                "approval_pending": _approval_pending(),
            })
        if path == "/events":
            return self._sse()
        if path == "/tools":
            return self._json(200, {"ok": True, "tools": _tool_names(),
                                    "injected": list(_injected)})
        if path == "/clarify/pending":
            return self._json(200, {"ok": True, "cards": _clarify_pending()})
        if path == "/approval/pending":
            try:
                import approval_adapter                # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval channel not ready: %s" % e})
            try:
                return self._json(200, {"ok": True, "cards": approval_adapter.pending_cards()})
            except Exception as e:
                # 观测面绝不允许影响裁决本身：出错就报空列表，别把异常抛给网关
                return self._json(200, {"ok": False, "cards": [],
                                        "error": "%s: %s" % (e.__class__.__name__, e)})
        return self._json(404, {"ok": False, "error": "unknown path"})

    def do_POST(self):  # noqa: N802
        if not self._authorized():
            return self._json(401, {"ok": False, "error": "bad token"})
        path = self.path.split("?", 1)[0]
        body = self._body()
        if path == "/start":
            return self._json(200, {"ok": True, "pid": os.getpid(),
                                    "mode": MODE or "default"})
        if path == "/chat":
            sid = str(body.get("session_id") or "").strip()
            msg = str(body.get("message") or "").strip()
            if not sid or not msg:
                return self._json(400, {"ok": False, "error": "session_id 与 message 必填"})
            res = _start_run(sid, msg)
            return self._json(200 if res.get("ok") else 409, res)
        if path == "/stop":
            return self._json(200, _stop_run(body.get("run_id")))
        if path == "/mid_turn":
            # 回合运行中追加「用户交代」（WebUI 独有）：投递给当前活动回合，
            # 由主循环在「下一批工具返回」处取件注入。
            res = _mid_turn(str(body.get("session_id") or ""), str(body.get("text") or ""),
                            body.get("run_id"))
            if res.get("ok"):
                return self._json(200, res)
            # 没有 reason 的失败只可能是入参问题（空文本 / 过长）→ 400；
            # 其余（无活动回合 / 回合或会话不符 / 队列满）都是状态不符 → 409。
            return self._json(400 if not res.get("reason") else 409, res)
        if path == "/clarify/answer":
            ask_id = str(body.get("ask_id") or "").strip()
            if not ask_id:
                return self._json(400, {"ok": False, "error": "ask_id 必填"})
            answer = body.get("answer")
            if answer is None:
                answer = body.get("choice", "")
            if isinstance(answer, (list, tuple)):
                answer = "、".join(str(a) for a in answer)
            return self._json(200, _answer_clarify(ask_id, str(answer)))
        if path == "/approval/answer":
            try:
                import approval_adapter                # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval channel not ready: %s" % e})
            res = approval_adapter.answer(str(body.get("ask_id") or ""),
                                       str(body.get("choice") or ""), body)
            return self._json(200 if res.get("ok") else 409, res)
        if path == "/approval/rules":
            # 面板查询：返回内存里当前真正生效的规则（永久 + 本会话会话级）
            try:
                import approval as _ab                    # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval not ready: %s" % e})
            sid = str(body.get("session_id") or "")
            return self._json(200, {"ok": True, "rules": _ab.SCOPES.list_all(sid),
                                    "session_id": sid})
        if path == "/approval/rules/revoke":
            try:
                import approval as _ab                    # noqa: PLC0415
            except Exception as e:
                return self._json(503, {"ok": False, "error": "approval not ready: %s" % e})
            sid = str(body.get("session_id") or "")
            try:
                n = _ab.SCOPES.revoke(str(body.get("path") or ""),
                                      str(body.get("scope") or ""), sid)
            except Exception as e:
                return self._json(500, {"ok": False, "error": "%s: %s" % (e.__class__.__name__, e)})
            return self._json(200, {"ok": True, "removed": n,
                                    "rules": _ab.SCOPES.list_all(sid)})
        if path == "/exit":
            wait_sec = float(body.get("timeout") or 10)
            self._json(200, {"ok": True, "message": "正在优雅退出"})
            threading.Thread(target=_graceful_exit, args=(wait_sec,),
                             name="bridge-exit", daemon=True).start()
            return
        return self._json(404, {"ok": False, "error": "unknown path"})

    def _sse(self):
        sub = BUS.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": bridge-stream\n\n")
            self.wfile.flush()
            while True:
                try:
                    evt = sub.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(
                        b'event: heartbeat\ndata: {"seq":0,"type":"heartbeat"}\n\n')
                    self.wfile.flush()
                    continue
                payload = json.dumps(evt, ensure_ascii=False)
                self.wfile.write(
                    f"event: {evt['type']}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(sub)

    def log_message(self, fmt, *args):  # 静音 access log，改道 stderr 并脱敏
        try:
            print("[bridge-http] " + _redact_secret(fmt % args), file=sys.__stderr__, flush=True)
        except Exception:
            pass


_SECRET_RE = re.compile("(X-Bridge-Token[=:] ?)[^&\s\"']+", re.IGNORECASE)


def _redact_secret(msg: str) -> str:
    """访问日志里的 bridge token 一律打码：日志可留，凭据不可留。"""
    return _SECRET_RE.sub(lambda m: m.group(1) + "***", msg)


def _approval_pending() -> int:
    """挂起中的审批数；-1 = 审批通道不可用，前端据此显示异常态。"""
    try:
        import approval_adapter                     # noqa: PLC0415
        return approval_adapter.pending_count()
    except Exception:
        return -1


def _tool_names() -> List[str]:
    try:
        import agent_tools  # noqa: PLC0415
        return sorted(agent_tools.AVAILABLE_TOOLS.keys())
    except Exception:
        return []


def _graceful_exit(wait_sec: float) -> None:
    """优雅退出：中断活动回合 -> 等它在边界保存 -> 关编排器 -> 结束进程。"""
    ctx = active_run()
    if ctx is not None:
        _stop_run(ctx.run_id)
        deadline = time.time() + wait_sec
        while time.time() < deadline and active_run() is not None:
            time.sleep(0.2)
    try:
        if _agent is not None:
            _agent.shutdown_orchestrator()
    except Exception:
        pass
    try:
        sys.stdout.flush()
        sys.__stderr__.flush()
    except Exception:
        pass
    print("[bridge] 进程退出", file=sys.__stderr__, flush=True)
    os._exit(0)


# ============================================================
# 10. 入口
# ============================================================
def main() -> None:
    sys.stdout = StdoutCapture()  # import 期 print 也会安全落 stderr
    try:
        info = _install_runtime()
    except BaseException as e:  # noqa: BLE001
        print(f"[bridge] AB 模块加载失败: {type(e).__name__}: {e}",
              file=sys.__stderr__, flush=True)
        try:
            import traceback
            print(traceback.format_exc(), file=sys.__stderr__, flush=True)
        except Exception:
            pass
        os._exit(2)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
    except OSError as e:
        print(f"[bridge] 端口绑定失败: {e}", file=sys.__stderr__, flush=True)
        os._exit(3)
    server.daemon_threads = True

    port = server.server_address[1]
    _cm = getattr(_agent, "CM_PARAMS", None) or {}
    _win = int(_cm.get("window_tokens") or 0)
    hello = {"port": port, "pid": os.getpid(), "mode": MODE or "default",
             "model": info["model"], "tools": len(info["tools"]),
             "injected": info["injected"], "pools": info.get("pools"),
             "usage_watch": "on" if "usage_watch" in info["injected"] else "off",
             # 上下文管理器（视图层）：前端画「上下文占比」圆圈需要窗口与阈值，
             # 一律取 agent 进程里的同一份 CM_PARAMS，避免两边各写一份配置
             "ctx": {"enabled": bool(_cm.get("enabled")),
                     "window": _win,
                     "threshold": int(_win * float(_cm.get("trigger_ratio") or 0.5)),
                     "keep_recent_rounds": int(_cm.get("keep_recent_rounds") or 0)},
             # 计量账本：前端启动即拿到上次的每会话用量（不必先发一条消息）
             "usage_ledger": _ledger_snapshot()}
    _REAL_STDOUT.write(json.dumps(hello, ensure_ascii=False) + "\n")
    _REAL_STDOUT.flush()

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
