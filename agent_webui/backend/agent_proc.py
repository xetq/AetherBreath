# -*- coding: utf-8 -*-
"""AgentManager —— AB bridge 子进程生命周期唯一权威。

职责：开机 / 优雅退出 / 强制 kill / 健康探测 / 回合转发 / 事件泵。
设计约束：
  - bridge 只用随机端口+一次性token「token 仅存内存与环境，不落盘不落日志」
  - 事件泵是单条 SSE 连接（网关是唯一订阅者），断线重连失败即判 bridge 失连
  - 任意运行态 -> OFF 都必须发 agent_phase，保证前端徽标不卡在过渡态
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import procguard
import config
from agent_client import BridgeClient, BridgeError
from events import (
    Ev, PHASE_OFF, PHASE_STARTING, PHASE_ON, PHASE_STOPPING,
    TURN_IDLE, TURN_ERROR, TURN_INTERRUPTED,
)
from sse import SSEHub
from state import StateMachine

BRIDGE_LOG_KEEP = 5


class AgentStartError(Exception):
    pass


class AgentManager:
    def __init__(self, hub: SSEHub, sm: StateMachine) -> None:
        self.hub = hub
        self.sm = sm
        self._proc: Optional[subprocess.Popen] = None
        self._client: Optional[BridgeClient] = None
        self._token: str = ""
        self._lock = threading.RLock()
        self._pump_thread: Optional[threading.Thread] = None
        self._pump_stop = threading.Event()
        self._stderr_file: Optional[Any] = None
        self._hello: Dict[str, Any] = {}
        self._operating = False  # 手动 start/stop 期间抑制崩溃判定
        self._last_tools: Dict[str, Any] = {}
        config.WEBUI_LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ==================== 广播 ====================
    def _emit_phase(self, reason: str = "", **extra: Any) -> None:
        snap = self.sm.snapshot()
        self.hub.publish(Ev.AGENT_PHASE, {
            "phase": snap["phase"], "turn_phase": snap["turn_phase"],
            "pid": snap["pid"], "bridge_url": snap["bridge_url"],
            "session_id": snap["session_id"], "run_id": snap["run_id"],
            "can_send": snap["can_send"], "busy": snap["busy"],
            "reason": reason, **extra,
        })

    def _emit_turn(self, phase: str, reason: str = "") -> None:
        snap = self.sm.snapshot()
        self.hub.publish(Ev.TURN_PHASE, {
            "phase": phase, "turn_phase": phase, "pid": snap["pid"],
            "session_id": snap["session_id"], "run_id": snap["run_id"],
            "can_send": snap["can_send"], "busy": snap["busy"], "reason": reason,
        })

    # ==================== 开机 ====================
    def start(self, mode: str = "") -> Dict[str, Any]:
        with self._lock:
            if self.sm.phase == PHASE_ON and self._proc and self._proc.poll() is None:
                return {"ok": True, "already": True, **self.sm.snapshot()}
            if self._proc and self._proc.poll() is None:
                self._kill_locked()
            self._operating = True
            self.sm.set_error(None)
            self.sm.set_phase(PHASE_STARTING)
            self._emit_phase("开机中")
        try:
            return self._start_unlocked(mode)
        finally:
            with self._lock:
                self._operating = False

    def _start_unlocked(self, mode: str) -> Dict[str, Any]:
        try:
            py = config.agent_python()
        except FileNotFoundError as e:
            self._fail_start(str(e))
            raise AgentStartError(str(e))

        self._token = secrets.token_urlsafe(24)
        env = os.environ.copy()
        env["AETHER_BRIDGE_TOKEN"] = self._token
        env["AETHER_APPROVAL_TIMEOUT"] = str(config.APPROVAL_TIMEOUT_SEC)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        if mode:
            env["AETHER_AB_MODE"] = mode
        else:
            env.pop("AETHER_AB_MODE", None)

        log_path = config.WEBUI_LOG_DIR / f"bridge_{datetime.now():%Y%m%d_%H%M%S}.log"
        try:
            self._stderr_file = open(log_path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            self._fail_start(f"日志文件不可写: {e}")
            raise AgentStartError(f"日志文件不可写: {e}")
        self._rotate_logs()

        try:
            self._proc = subprocess.Popen(
                [str(py), str(config.BRIDGE_SCRIPT)],
                cwd=str(config.PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._stderr_file,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **self._spawn_flags(),
            )
        except OSError as e:
            self._fail_start(f"子进程启动失败: {e}")
            raise AgentStartError(f"子进程启动失败: {e}")

        hello = self._read_hello(self._proc, config.START_TIMEOUT_SEC)
        if hello is None:
            alive = self._proc.poll() is None
            tail = self.stderr_tail()
            self._kill_locked()
            msg = "bridge 未上报端口（首行协议超时或进程提前退出）"
            if not alive:
                msg = "bridge 启动即退出"
            detail = f"{msg}；stderr 摘要: {tail[-600:]}" if tail else msg
            self._fail_start(detail)
            raise AgentStartError(detail)

        port = int(hello.get("port") or 0)
        if port <= 0:
            self._kill_locked()
            self._fail_start("bridge 首行端口非法")
            raise AgentStartError("bridge 首行端口非法")

        url = f"http://127.0.0.1:{port}"
        self._client = BridgeClient(url, self._token)
        health = self._wait_health(config.START_TIMEOUT_SEC)
        if health is None:
            tail = self.stderr_tail()
            self._kill_locked()
            detail = f"bridge 健康检查失败: {url}" + (f"；stderr: {tail[-500:]}" if tail else "")
            self._fail_start(detail)
            raise AgentStartError(detail)

        self._hello = hello
        self.sm.set_process_info(pid=hello.get("pid") or self._proc.pid,
                                 bridge_url=url,
                                 health_at=datetime.now().isoformat(timespec="seconds"))
        self.sm.set_phase(PHASE_ON)
        self.sm.set_turn(TURN_IDLE)
        self._last_tools = {"tools": health.get("tools"), "injected": hello.get("injected", [])}
        self._start_pump()
        self._emit_phase("开机完成", model=hello.get("model"),
                         tools=hello.get("tools"), injected=hello.get("injected"),
                         pid=self.sm.snapshot()["pid"])
        return {"ok": True, **self.sm.snapshot(), "model": hello.get("model"),
                "tools": hello.get("tools"), "injected": hello.get("injected"),
                "mode": hello.get("mode", "default")}

    # ==================== 首行协议 ====================
    @staticmethod
    def _read_hello(proc: subprocess.Popen, timeout: float) -> Optional[Dict[str, Any]]:
        """独立线程读 stdout 首行，避免 bridge 崩溃时无限阻塞。"""
        box: Dict[str, Any] = {}

        def _reader():
            try:
                line = proc.stdout.readline() if proc.stdout else ""
                box["line"] = line
            except Exception as e:  # noqa: BLE001
                box["err"] = str(e)

        t = threading.Thread(target=_reader, name="bridge-hello", daemon=True)
        t.start()
        t.join(timeout=timeout)
        line = box.get("line")
        if not line:
            return None
        try:
            return json.loads(line.strip())
        except Exception:
            return None

    def _wait_health(self, timeout: float) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return None
            try:
                h = self._client.health()
                if h.get("ok"):
                    return h
            except BridgeError:
                time.sleep(0.4)
        return None

    def _fail_start(self, detail: str) -> None:
        self.sm.set_error(detail)
        self.sm.set_phase(PHASE_OFF)
        self._emit_phase("启动失败", error=detail)

    def stderr_tail(self, n: int = 25) -> str:
        """bridge stderr 尾部（诊断用，路由可安全调用）。"""
        try:
            path = self._stderr_path
            if path and Path(path).exists():
                lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
                return "\n".join(lines[-n:])
        except Exception:
            pass
        return ""

    @property
    def _stderr_path(self) -> Optional[Path]:
        f = self._stderr_file
        return Path(f.name) if f and hasattr(f, "name") else None

    def _rotate_logs(self) -> None:
        """只保留最近 BRIDGE_LOG_KEEP 个 bridge 日志，避免磁盘堆积。"""
        try:
            logs = sorted(config.WEBUI_LOG_DIR.glob("bridge_*.log"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for old in logs[BRIDGE_LOG_KEEP:]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    # ==================== 事件泵 ====================
    def _start_pump(self) -> None:
        self._pump_stop.clear()
        self._pump_thread = threading.Thread(target=self._pump_loop,
                                             name="bridge-event-pump", daemon=True)
        self._pump_thread.start()

    def _pump_loop(self) -> None:
        client = self._client

        def on_event(evt: Dict[str, Any]) -> None:
            self._forward(evt)

        def on_fail(msg: str) -> None:
            if self._operating or self._pump_stop.is_set():
                return
            if self._proc is not None and self._proc.poll() is not None:
                self._handle_crash(f"bridge 进程已退出（{msg}）")
            else:
                self.hub.publish(Ev.ERROR, {"message": f"bridge 事件通道异常: {msg}",
                                            "scope": "pump"})

        if client is None:
            return
        client.stream_events(on_event, self._pump_stop, on_fail, max_failures=4)

    def _forward(self, evt: Dict[str, Any]) -> None:
        """bridge 事件 -> 前端事件，同时同步回合级状态机。"""
        etype = evt.get("type")
        payload = {k: v for k, v in evt.items() if k not in ("seq", "ts")}
        if etype == "turn_phase":
            phase = payload.get("phase") or TURN_IDLE
            self.sm.set_turn(phase)
            payload["can_send"] = self.sm.snapshot()["can_send"]
            payload["busy"] = self.sm.snapshot()["busy"]
        elif etype in ("done", "error"):
            self.sm.set_turn(TURN_IDLE if etype == "done" else TURN_ERROR)
            self.sm.set_run(None)
            payload["can_send"] = True
            payload["busy"] = False
        elif etype == "tool_begin":
            self.sm.set_turn("TOOL_RUNNING")
        elif etype == "clarify_request":
            self.sm.set_turn("CLARIFY_WAIT")
        elif etype in ("approval_request",):
            self.sm.set_turn("AUDIT_WAIT")
        elif etype in ("approval_resolved", "approval_expired"):
            # 门禁结束（批准/拒绝/超时）：交还控制权给主循环
            self.sm.set_turn("THINKING")
        elif etype in ("approval_request", "approval_expired"):
            # 审批门禁挂起：回合仍在跑，但明确停在"等人裁决"
            if etype == "approval_request":
                self.sm.set_turn("AUDIT_WAIT")
        self.hub.publish(etype or Ev.STAGE, payload)

    def _handle_crash(self, reason: str) -> None:
        with self._lock:
            if self.sm.phase == PHASE_OFF:
                return
            self.sm.set_error(reason)
            self.sm.set_phase(PHASE_OFF)
            self._client = None
            self._proc = None
            self._emit_phase("异常退出", error=reason)

    # ==================== 关机 ====================
    def stop(self, graceful_timeout: Optional[float] = None) -> Dict[str, Any]:
        gt = graceful_timeout or config.GRACEFUL_STOP_TIMEOUT_SEC
        with self._lock:
            if self.sm.phase == PHASE_OFF:
                return {"ok": True, "already_off": True, **self.sm.snapshot()}
            proc, client = self._proc, self._client
            self._operating = True
            self.sm.set_phase(PHASE_STOPPING)
            self._emit_phase("优雅关机中")
        killed = False
        reason = "graceful"
        try:
            if client is not None:
                try:
                    client.exit_gracefully(min(gt, max(gt - 2, 3)))
                except BridgeError:
                    pass
            if self._wait_exit(gt):
                reason = "graceful"
            else:
                killed = True
                reason = f"优雅退出超时 {gt}s，已强制终止"
                self.kill()
        finally:
            with self._lock:
                if self.sm.phase != PHASE_OFF:
                    self.sm.set_phase(PHASE_OFF)
                self._client = None
                self._pump_stop.set()
                self._close_stderr()
                self._operating = False
                self._emit_phase(reason)
        return {"ok": True, "killed": killed, "reason": reason, **self.sm.snapshot()}

    def _wait_exit(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is None or self._proc.poll() is not None:
                return True
            time.sleep(0.25)
        return False

    def kill(self) -> Dict[str, Any]:
        with self._lock:
            proc, client = self._proc, self._client
            self._operating = True
            if self.sm.phase != PHASE_OFF:
                self.sm.set_phase(PHASE_STOPPING)
                self._emit_phase("强制关闭")
            # 强杀语义 = 立即。只有确实有回合在跑时才补发一次中断（让它有机会
            # 落盘），且用短超时；空闲时绝不为这次请求白等（实测曾拖 15s）。
            try:
                if client is not None and self.sm.snapshot()["busy"]:
                    client.stop(None, timeout=2.5)
            except Exception:
                pass
            result = self._kill_locked()
            self._pump_stop.set()
            self._close_stderr()
            self.sm.set_phase(PHASE_OFF)
            self._client = None
            self._operating = False
            self._emit_phase("已强制终止",
                             note="最多丢失当前半轮，重开后可续聊")
            return {"ok": True, "killed": result, **self.sm.snapshot()}

    @staticmethod
    def _spawn_flags() -> Dict[str, Any]:
        """让 bridge 自成进程组/会话，保证能被整树回收。"""
        if sys.platform == "win32":
            return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        return {"start_new_session": True}

    def _kill_locked(self) -> bool:
        """强制回收 bridge —— 必须按【进程树】杀。

        踩过的坑：本机 venv 由 uv 创建，Windows 上 venv\Scripts\python.exe 只是
        trampoline，Popen.pid 是壳，真身是它的子进程。只 proc.kill() 会杀掉壳而
        留下真身 —— 状态显示 OFF，端口与 LLM 配置却仍被占用。树杀交给 procguard，
        失败必须上报（旧版只把 died 记成 False 就继续写 OFF，孤儿就是这么攒的）。
        """
        proc = self._proc
        if proc is None:
            return False
        pid = proc.pid
        died = True
        try:
            if proc.poll() is None:
                dead, survivors, notes = procguard.kill_pids([pid])
                if pid not in dead:
                    died = False
                    why = notes.get(pid) or ("仍存活" if pid in survivors else "状态未知")
                    self.sm.set_error(f"强杀 bridge(pid={pid}) 未成功：{why}")
                    print(f"[gateway] ⚠️ 强杀 bridge(pid={pid}) 未成功：{why}",
                          file=sys.stderr, flush=True)
            try:
                proc.wait(timeout=8)
            except Exception:
                died = False
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            died = False
            print(f"[gateway] ⚠️ 强杀路径异常：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
        self._proc = None
        return died
    def _close_stderr(self) -> None:
        try:
            if self._stderr_file:
                self._stderr_file.close()
        except Exception:
            pass
        self._stderr_file = None

    # ==================== 业务转发 ====================
    @property
    def client(self) -> BridgeClient:
        c = self._client
        if c is None or self.sm.phase != PHASE_ON:
            raise BridgeError("AB 进程未运行（请先开机）")
        return c

    def chat(self, session_id: str, message: str) -> Dict[str, Any]:
        res = self.client.chat(session_id, message)
        if res.get("ok"):
            self.sm.set_session(session_id)
            self.sm.set_run(res.get("run_id"))
            self.sm.set_turn(TURN_IDLE)
            self._emit_turn("THINKING", "回合已受理")
        return res

    def stop_run(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        res = self.client.stop(run_id)
        if res.get("stopped"):
            self.sm.set_turn(TURN_INTERRUPTED)
            self._emit_turn(TURN_INTERRUPTED, "已请求中断，将在工具/请求边界保存退出")
        return res

    def mid_turn(self, session_id: str, text: str,
                 run_id: Optional[str] = None) -> Dict[str, Any]:
        """中期交互：把「用户交代」投给正在跑的回合。纯投递，不改网关侧状态机 ——
        回合该是 THINKING/TOOL_RUNNING 就还是那个状态，注入与否由 AB 侧决定
        （真实结果由 bridge 的 mid_turn 事件回报，网关不替它表态）。"""
        return self.client.mid_turn(session_id, text, run_id)

    def clarify_answer(self, ask_id: str, answer: str) -> Dict[str, Any]:
        return self.client.clarify_answer(ask_id, answer)

    def approval_answer(self, ask_id: str, choice: str = "", payload=None) -> Dict[str, Any]:
        return self.client.approval_answer(ask_id, choice, payload or {})

    def approval_rules(self, session_id: str = "") -> Dict[str, Any]:
        return self.client.approval_rules(session_id)

    def approval_revoke(self, payload=None) -> Dict[str, Any]:
        return self.client.approval_revoke(payload or {})

    # ---- 卡片恢复：前端一加载就会调，所以"没开机"必须是空列表而不是异常 ----
    # 但空列表不等于"服务端说没有卡"。用 authoritative 把「问到了，确实没有」
    # 和「压根没问到」分开：前端只有在前者才允许剪枝，否则会把屏幕上真挂着的
    # 卡片删掉 —— 那正是本按钮要修的那类 bug 的镜像。别让文案承载这个语义。
    def approval_pending(self) -> Dict[str, Any]:
        try:
            c = self.client
        except BridgeError as e:
            return {"ok": True, "cards": [], "authoritative": False,
                    "error": "bridge 未就绪：%s" % e}
        return c.approval_pending()

    def clarify_pending(self) -> Dict[str, Any]:
        try:
            c = self.client
        except BridgeError as e:
            return {"ok": True, "cards": [], "authoritative": False,
                    "error": "bridge 未就绪：%s" % e}
        return c.clarify_pending()

    def tools(self) -> Dict[str, Any]:
        try:
            t = self.client.tools()
            self._last_tools = t
            return t
        except BridgeError as e:
            return {"ok": False, "error": str(e), **self._last_tools}

    def health(self) -> Dict[str, Any]:
        try:
            return {"ok": True, **self.client.health()}
        except BridgeError as e:
            return {"ok": False, "error": str(e)}

    def status(self) -> Dict[str, Any]:
        snap = self.sm.snapshot()
        snap["alive"] = bool(self._proc and self._proc.poll() is None)
        snap["hello"] = self._hello
        snap["mode"] = self._hello.get("mode", "default")
        snap["injected"] = self._hello.get("injected", [])
        snap["subscribers"] = self.hub.subscriber_count()
        snap["graceful_timeout"] = config.GRACEFUL_STOP_TIMEOUT_SEC
        snap["clarify_timeout"] = config.CLARIFY_TIMEOUT_SEC
        snap["gateway"] = {"host": config.GATEWAY_HOST, "port": config.GATEWAY_PORT,
                           "pid": os.getpid()}
        return snap

    def shutdown_if_running(self) -> None:
        """网关退出前的最后努力：尽量优雅带走 bridge，避免孤儿进程。"""
        with self._lock:
            if self.sm.phase == PHASE_OFF:
                return
        try:
            self.stop(graceful_timeout=min(6.0, config.GRACEFUL_STOP_TIMEOUT_SEC))
        except Exception:
            try:
                self.kill()
            except Exception:
                pass


# ==================== 进程内单例 ====================
_manager: Optional[AgentManager] = None
_manager_lock = threading.Lock()


def get_manager(hub: Optional[SSEHub] = None,
                sm: Optional[StateMachine] = None) -> AgentManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _hub = hub or SSEHub()
            _sm = sm or StateMachine()
            _manager = AgentManager(_hub, _sm)
        return _manager


def try_get_manager() -> Optional[AgentManager]:
    return _manager


# ==================== 孤儿 bridge 回收 ====================
def cleanup_orphan_bridges():
    """回收上一代遗留的 bridge —— 判据与实现全在 procguard（可独立单测）。

    旧版用 CommandLine -like '*bridge.py*' 子串匹配，会误杀任何命令行里出现过
    bridge.py 字样的无关 python 进程（实测复现：只含该字符串的 sleep 进程被判定为
    bridge）。现改为「命令行以 backend/bridge.py 结尾 + 父链无活网关 + 自身祖先
    无条件保护」。返回明细 dict，日志与界面能看到到底杀了谁、为什么没杀。
    """
    return procguard.cleanup_orphan_bridges()