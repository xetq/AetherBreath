"""
工具名称: execute_python
功能: 执行一段 Python 代码，并返回输出结果。

安全策略（资源护栏模型，参照宿主 Agent 的 execute_code 标准）:
  1. 进程隔离    : 独立子进程 + 独立进程组；超时可整棵进程树强杀，不残留孤儿进程。
  2. 资源护栏    : 超时（有上限 clamp）+ 输出 head/tail 截断（防死循环 print 撑爆内存）。
  3. 凭据隔离    : 环境变量只保留白名单前缀 + 剔除秘密子串（KEY/TOKEN/SECRET...），
                   防止脚本读到并外泄 API key 等凭据。
  4. 能力拦截    : AST 黑名单「尽力而为」地拦截危险能力调用。注意——这不是安全边界，
                   恶意代码可绕过 AST 文本检查（如 __class__.__base__.__subclasses__、
                   getattr(__builtins__,'__import__')）。真正的可信执行需要 OS 级沙箱/容器。

  🔴 定位声明: 本模块是「防失控的护栏」，不是「安全沙箱」。
    不要用它执行来自不可信来源、或用户无法确认意图的代码。
"""

import ast
import logging
import os
import platform
import signal
import subprocess
import sys
import tempfile
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ================= 配置（全部可调） =================
DEFAULT_TIMEOUT = 30          # 默认超时（秒）
MAX_TIMEOUT = 120             # 超时硬上限（防止调用方传 timeout=999999）
MAX_STDOUT_BYTES = 50_000     # 输出截断上限（head 40% + tail 60%）
MAX_STDERR_BYTES = 10_000
_IS_WINDOWS = platform.system() == "Windows"

# ---- 工作区临时目录（替代系统 Temp，避免占用 C 盘） ----
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_TMP_DIR = os.path.join(_PROJECT_ROOT, "agent_workspace", ".tmp_exec")

# ---- 危险能力黑名单（AST 层，尽力而为） ----
# 危险模块导入：凡 import 到这些模块直接拦截（importlib 等是逃逸工具）
DANGEROUS_IMPORTS = {
    "ctypes", "cffi", "pickle", "marshal", "importlib", "pty", "telnetlib",
    "pty", "_thread", "threading",  # threading 可用于脱离主流程
}
# 危险函数调用（无论以哪个名字/对象出现，按函数名匹配）
DANGEROUS_FUNCS = {
    "eval", "exec", "compile", "__import__",
    # os 高危
    "system", "popen", "spawn", "fork", "execl", "execv", "execle", "execve",
    "execlp", "execvp", "remove", "removedirs", "rmdir", "unlink", "replace",
    # subprocess 高危
    "run", "call", "check_output", "check_call", "Popen", "getoutput",
}
# 对象 -> 允许的属性白名单：白名单内显式放行（优先于黑名单），
# 白名单之外且命中高危对象的属性调用才被拦截。文件/目录删除已放行，
# 否则只能建不能删。
ALLOWED_ATTRIBUTES = {
    "os": {"path", "getcwd", "chdir", "listdir", "stat", "getenv", "environ", "name",
           "sep", "linesep", "pathsep", "curdir", "pardir", "mkdir", "makedirs",
           # 文件/目录删除与重命名（agent 需要清理能力）
           "remove", "unlink", "rmdir", "removedirs", "replace", "rename"},
    "sys": {"path", "version", "version_info", "platform", "executable", "stdout",
            "stderr", "stdin", "getrecursionlimit", "setrecursionlimit"},
    "shutil": {"which", "get_terminal_size", "rmtree",
               # rmtree 递归删除非空目录树（仅空目录时 os.rmdir 不够用）
               "move", "copy", "copyfile"},
    "subprocess": {"PIPE", "STDOUT", "DEVNULL", "check_output"},
}

# ---- 环境变量清洗规则 ----
# 白名单前缀（保留）；秘密子串（剔除）；Windows 必备（保留，否则 socket/subprocess 崩）
_SAFE_ENV_PREFIXES = (
    "PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMPDIR", "TMP", "TEMP",
    "SHELL", "LOGNAME", "XDG_", "VIRTUAL_ENV", "CONDA", "PYTHONPATH", "PYTHONHOME",
)
_SECRET_SUBSTRINGS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH", "DSN",
    "WEBHOOK", "CREDS", "BEARER", "APIKEY", "PRIVATE",
)
_WINDOWS_ESSENTIAL_ENV_VARS = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "PUBLIC", "ALLUSERSPROFILE",
    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "APPDATA", "LOCALAPPDATA",
    "USERPROFILE", "USERDOMAIN", "USERNAME", "HOMEDRIVE", "HOMEPATH", "COMPUTERNAME",
})

# ================= AST 安全检查（尽力而为） =================

def _is_dangerous_call(node: ast.Call) -> Optional[str]:
    """判断一个调用节点是否命中危险能力。"""
    func = node.func
    # 裸函数名: eval(...)
    if isinstance(func, ast.Name):
        if func.id in DANGEROUS_FUNCS:
            return f"禁止调用危险函数: {func.id}()"
        return None
    # 属性调用: os.system(...) / shutil.rmtree(...)
    if isinstance(func, ast.Attribute):
        base = func.value
        attr = func.attr
        if isinstance(base, ast.Name):
            obj = base.id
            allowed = ALLOWED_ATTRIBUTES.get(obj, set())
            # 白名单显式允许 → 直接放行（优先于黑名单，否则放了也白放）
            if allowed and attr in allowed:
                return None
            # 高危对象上未放行的属性 → 拦截
            if obj in ("os", "sys", "shutil", "subprocess", "pickle", "ctypes"):
                if allowed:
                    return f"禁止调用: {obj}.{attr}()"
            # 兜底黑名单函数名（覆盖裸名/未知对象上的危险方法）
            if attr in DANGEROUS_FUNCS:
                return f"禁止调用危险方法: {obj}.{attr}()"
        # obj.getattr(...) 这类动态取属性也拦（可用 getattr 绕黑名单）
        if isinstance(base, ast.Call) and attr == "__import__":
            return "禁止通过动态调用导入模块"
    return None


def _is_dangerous_node(node: ast.AST) -> Optional[str]:
    """递归遍历 AST，返回首个危险描述；无危险则返回 None。"""
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.split(".")[0] in DANGEROUS_IMPORTS:
                return f"禁止导入危险模块: {alias.name}"
    elif isinstance(node, ast.ImportFrom):
        if node.module and node.module.split(".")[0] in DANGEROUS_IMPORTS:
            return f"禁止从危险模块导入: {node.module}"
    elif isinstance(node, ast.Call):
        danger = _is_dangerous_call(node)
        if danger:
            return danger
    for child in ast.iter_child_nodes(node):
        result = _is_dangerous_node(child)
        if result:
            return result
    return None


def validate_code(code: str) -> Optional[str]:
    """验证代码安全性。返回错误信息或 None。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"代码语法错误: {e}"
    return _is_dangerous_node(tree)


# ================= 环境变量清洗 =================

def clean_environment() -> Dict[str, str]:
    """构建清洗后的子进程环境。

    规则（顺序）:
      1. 秘密子串（KEY/TOKEN/SECRET/PASSWORD...）剔除 —— 防凭据泄露，这是真正的安全关键。
      2. 白名单前缀保留（PATH/HOME/LANG/TERM/...）。
      3. Windows 必备变量保留（SYSTEMROOT/COMSPEC/...），否则 socket/subprocess 直接崩。
      4. 其余（含 AETHER_*/非白名单变量）全部丢弃。
    """
    env: Dict[str, str] = {}
    for k, v in os.environ.items():
        if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
            continue  # 凭据，剔除
        if k.startswith(_SAFE_ENV_PREFIXES):
            env[k] = v
            continue
        if _IS_WINDOWS and k.upper() in _WINDOWS_ESSENTIAL_ENV_VARS:
            env[k] = v
            continue
        # 其余全部丢弃（不放进 env）
    # 兜底：必须保证 PATH 存在，否则子进程连解释器都找不到
    if "PATH" not in env:
        env["PATH"] = os.defpath
    return env


# ================= 子进程强杀 =================

def _kill_process_tree(proc: subprocess.Popen) -> None:
    """强杀整个进程树。Windows 用 taskkill /T，POSIX 用进程组 SIGKILL。"""
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ================= 流式输出截断（防 OOM） =================

def _drain_head_tail(pipe, head_chunks, tail_buf, head_bytes, tail_bytes, total_ref):
    """读管道，只保留 head 40% + tail 60%，中间超限部分丢弃。"""
    head_collected = 0
    tail_collected = 0
    try:
        while True:
            data = pipe.read(4096)
            if not data:
                break
            total_ref[0] += len(data)
            if head_collected < head_bytes:
                keep = min(len(data), head_bytes - head_collected)
                head_chunks.append(data[:keep])
                head_collected += keep
                data = data[keep:]
                if not data:
                    continue
            tail_buf.append(data)
            tail_collected += len(data)
            while tail_collected > tail_bytes and tail_buf:
                tail_collected -= len(tail_buf.popleft())
    except (ValueError, OSError):
        pass


def _drain_head(pipe, chunks, max_bytes):
    """读管道，只保留前 max_bytes（用于 stderr，错误通常出现在开头）。"""
    total = 0
    try:
        while True:
            data = pipe.read(4096)
            if not data:
                break
            if total < max_bytes:
                keep = max_bytes - total
                chunks.append(data[:keep])
            total += len(data)
    except (ValueError, OSError):
        pass


# ================= 主执行函数 =================

def _write_script_with_retry(code: str, tmp_dir: str, retries: int = 3):
    """写入脚本文件并做写后确认（stat 校验字节数），失败带退避重试。

    针对 Windows 环境 I/O 抖动（"写文件偶发超时但实际成功"）：
    与其让调用方猜，不如在写入后立刻 stat 二次确认；偶发失败自动重试。

    注意: newline="\\n" 禁止 Windows 换行转换，否则 UTF-8 字节数无法精确预测。

    返回 (tmp_path, attempts, elapsed_sec)。
    全部重试仍失败则抛出最后一次 OSError。
    """
    expected_size = len(code.encode("utf-8"))
    last_exc: Optional[OSError] = None
    attempts = 0
    t0 = _time.perf_counter()
    for attempt in range(1, retries + 1):
        tmp_path = None
        attempts = attempt
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".py", delete=False,
                encoding="utf-8", newline="\n", dir=tmp_dir,
            ) as f:
                f.write(code)
                f.flush()
                os.fsync(f.fileno())
                tmp_path = f.name
            # 写后二次确认：stat 校验存在性与字节数
            st = os.stat(tmp_path)
            if st.st_size != expected_size:
                raise OSError(
                    f"写后确认失败: 期望 {expected_size} bytes, 实际 {st.st_size} bytes"
                )
            return tmp_path, attempts, _time.perf_counter() - t0
        except OSError as e:
            last_exc = e
            # 清理可能残留的半成品文件
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if attempt < retries:
                _time.sleep(0.2 * attempt)  # 退避 0.2s / 0.4s / 0.6s
    assert last_exc is not None
    raise last_exc


def execute_python(code: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """在受限子进程中执行 Python 代码，返回 stdout 或错误信息。

    参数:
        code: 要执行的 Python 代码字符串
        timeout: 最大允许执行秒数（自动 clamp 到 [1, MAX_TIMEOUT]）

    返回:
        执行结果字符串（包含 stdout/stderr 或错误描述）
    """
    t_total0 = _time.perf_counter()

    # 0. 参数守卫：clamp 超时
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    # 1. 代码安全检查（尽力而为）
    error = validate_code(code)
    if error:
        return f"❌ 安全拦截: {error}"

    # 2. 写入临时文件（重试 + 写后确认，规避环境 I/O 抖动）
    write_attempts = 0
    try:
        os.makedirs(WORKSPACE_TMP_DIR, exist_ok=True)
        tmp_path, write_attempts, write_sec = _write_script_with_retry(
            code, WORKSPACE_TMP_DIR
        )
    except Exception as e:
        return f"❌ 创建临时文件失败（尝试 {write_attempts} 次）: {e}"

    # 3. 准备干净的运行环境
    clean_env = clean_environment()
    python_executable = sys.executable
    run_dir = WORKSPACE_TMP_DIR

    # 4. 启动子进程（独立进程组，可整组强杀）
    popen_kwargs = dict(
        cwd=run_dir,
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,          # 输入交给 /dev/null，防 input() 挂死
        bufsize=0,
    )
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    proc = None
    try:
        t_start0 = _time.perf_counter()
        proc = subprocess.Popen([python_executable, tmp_path], **popen_kwargs)
        start_sec = _time.perf_counter() - t_start0

        # 5. 后台线程流式读输出（head+tail 截断，避免死循环 print 撑爆内存）
        head_bytes = int(MAX_STDOUT_BYTES * 0.4)
        tail_bytes = MAX_STDOUT_BYTES - head_bytes
        stdout_head, stdout_tail = [], deque()
        stderr_chunks = []
        stdout_total = [0]
        import threading
        t_out = threading.Thread(
            target=_drain_head_tail,
            args=(proc.stdout, stdout_head, stdout_tail, head_bytes, tail_bytes, stdout_total),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain_head, args=(proc.stderr, stderr_chunks, MAX_STDERR_BYTES), daemon=True
        )
        t_out.start(); t_err.start()

        # 6. 轮询：检查退出、超时、输出总量
        deadline = time_now() + timeout
        status = "success"
        while proc.poll() is None:
            if time_now() > deadline:
                _kill_process_tree(proc)
                status = "timeout"
                break
            try:
                proc.wait(timeout=min(0.05, max(0.0, deadline - time_now())))
            except subprocess.TimeoutExpired:
                pass
            time_sleep(0.05)
        exec_sec = _time.perf_counter() - t_start0

        t_out.join(timeout=3); t_err.join(timeout=3)

        out_bytes = b"".join(stdout_head) + b"".join(stdout_tail)
        err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stdout_text = _format_truncated(stdout_bytes=out_bytes, total=stdout_total[0])
        stderr_text = err_text.strip()

        if status != "success":
            return (
                f"⏰ 执行超时（超过 {timeout} 秒），已强杀整个进程树\n"
                f"[耗时] 写脚本 {write_sec:.2f}s（{write_attempts} 次）/ "
                f"启动 {start_sec:.2f}s / 执行 {exec_sec:.2f}s / "
                f"总计 {_time.perf_counter() - t_total0:.2f}s"
            )

        exit_code = proc.returncode
        output_parts = []
        if stdout_text:
            output_parts.append(stdout_text)
        if stderr_text:
            output_parts.append(f"[stderr]\n{stderr_text}")
        if not output_parts:
            output_parts.append("(代码执行成功，但无输出)")
        if exit_code != 0:
            return f"❌ 执行失败（退出码 {exit_code}）:\n" + "\n".join(output_parts)
        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return (
            f"⏰ 执行超时（超过 {timeout} 秒）\n"
            f"[耗时] 写脚本 {write_sec:.2f}s（{write_attempts} 次）/ "
            f"总计 {_time.perf_counter() - t_total0:.2f}s"
        )
    except Exception as e:
        return (
            f"❌ 执行异常: {e}\n"
            f"[耗时] 写脚本 {write_sec:.2f}s（{write_attempts} 次）/ "
            f"总计 {_time.perf_counter() - t_total0:.2f}s"
        )
    finally:
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)
        # 清理临时文件
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---- 小工具 ----
import time as _time


def time_now() -> float:
    return _time.monotonic()


def time_sleep(sec: float) -> None:
    _time.sleep(sec)


def _format_truncated(stdout_bytes: bytes, total: int) -> str:
    """将捕获的 stdout 解码为文本，若被截断则追加提示。"""
    text = stdout_bytes.decode("utf-8", errors="replace")
    if total > len(stdout_bytes):
        omitted = total - len(stdout_bytes)
        text += (f"\n\n... [OUTPUT TRUNCATED - {omitted:,} bytes omitted "
                 f"out of {total:,} total] ...\n")
    return text


# ================= 工具的 Schema =================

execute_python_schema = {
    "type": "function",
    "function": {
        "name": "execute_python",
        "description": (
            "执行一段 Python 代码，并返回执行结果。"
            "适用于需要多步计算、数据处理、文件操作等场景。"
            "代码运行在受限子进程中：有超时（默认 30 秒，最大 120 秒）、"
            "输出会截断、环境变量已清洗（凭据不可见）。"
            "代码禁止调用系统级危险能力（system/eval/importlib 等）；"
            "文件/目录删除（remove/rmdir/rmtree）和复制移动已放行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要执行的 Python 代码（纯文本，不含输入交互）。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "执行超时时间（秒），默认 30，最大 120。",
                    "default": 30,
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT,
                }
            },
            "required": ["code"]
        }
    }
}
