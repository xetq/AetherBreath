"""
工具名称: execute_shell
功能: 在受限子进程中执行一条 shell 命令，并返回输出结果。

安全策略（资源护栏模型，参照宿主 Agent 的 terminal 工具标准 + execute_python 护栏）:
  1. 进程隔离    : 独立子进程 + 独立进程组；超时可整棵进程树强杀，不残留孤儿进程。
  2. 资源护栏    : 超时（有上限 clamp）+ 输出 head/tail 截断（防死循环输出撑爆内存）。
  3. 凭据隔离    : 环境变量只保留白名单前缀 + 剔除秘密子串（KEY/TOKEN/SECRET...），
                   防止命令读到并外泄 API key 等凭据。
  4. 命令拦截    : 正则黑名单「尽力而为」地拦截高危命令（系统目录删除、磁盘格式化、
                   关机重启、反弹 shell、下载执行链、挖矿勒索等）。
                   注意——这不是安全边界，恶意命令可绕过文本检查（如编码混淆、base64、
                   变量拼接）。真正的可信执行需要 OS 级沙箱/容器。

  🔴 定位声明: 本模块是「防失控的护栏」，不是「安全沙箱」。
    不要用它执行来自不可信来源、或用户无法确认意图的命令。

平台说明: 命令通过 bash 执行（Windows 上为 git-bash/MSYS）。普通文件操作、
    git/npm/python 等开发命令、网络请求、文件/目录删除（rm -rf 具体路径）均放行。
"""

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ================= 配置（全部可调） =================
DEFAULT_TIMEOUT = 30          # 默认超时（秒）
MAX_TIMEOUT = 120             # 超时硬上限（防止调用方传 timeout=999999）
MAX_STDOUT_BYTES = 100_000    # 输出截断上限（head 40% + tail 60%）
MAX_STDERR_BYTES = 20_000
_IS_WINDOWS = platform.system() == "Windows"

# ---- 项目 venv（相对路径动态推导，随项目走，GitHub 友好） ----
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_BIN_DIR = "Scripts" if os.name == "nt" else "bin"
PROJECT_VENV_BIN = os.path.join(_PROJECT_ROOT, "venv", _VENV_BIN_DIR)

# ================= 危险命令检测（正则黑名单，尽力而为） =================
#
# 设计原则（对齐宿主 terminal 的 tirith + dangerous-command 思路的轻量版）:
#   - 只拦截「危险目标」，不拦正常清理: `rm -rf node_modules` / `rm -rf ./build` 放行，
#     `rm -rf /` / `rm -rf C:\` / `rm -rf ~` / `rm -rf /c` 拦截。
#   - 匹配对大小写不敏感（Windows 路径、PowerShell 混写都覆盖）。
#   - 每条规则是一个正则；命中即返回规则描述。

# rm -rf 危险目标: 根目录、通配符裸删、家目录、系统目录、Windows 盘符根
_RM_RF_PATTERNS = [
    # 根目录/盘符根: rm -rf /、rm -rf /*、rm -rf /c、rm -rf C:\、rm -rf C:/、rm -rf /c/
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:/|/\*|/c/?|/d/?|/e/?|/[a-zA-Z]:/?|C:[/\\]|D:[/\\]|E:[/\\])(?:[\x22\x27\x60]?)(?:\s|;|$)",
    # 家目录: rm -rf ~、rm -rf $HOME、rm -rf /home、rm -rf /root、rm -rf /Users/<name>
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:~|~/|~\$|/home|/home/|/root|/root/|\$HOME|\$HOME/|/Users/)(?:[\x22\x27\x60]?)(?:\s|;|$)",
    # 系统目录直删: /etc /usr /bin /sbin /boot /var /lib /opt /System /Windows /System32 /Program Files
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:/etc|/usr|/bin|/sbin|/boot|/var|/lib|/opt|/System|/Windows|/System32|/Windows/|/Program\s+Files)(?:[\x22\x27\x60]?)(?:\s|;|$)",
    # 通配符裸删（打错即灾难）: rm -rf *、rm -rf .、rm -rf ..、rm -rf ./*、rm -rf ../*
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(?:--\s*)?(?:[\x22\x27\x60]?)(?:\*|\.|\.\.|\./\*|\.\./\*|\.\*|\./\..*)(?:[\x22\x27\x60]?)(?:\s|;|$)",
]

# 磁盘/分区/系统级破坏
_DISK_DESTROY_PATTERNS = [
    r"\b(?:dd\s+.*\s+of=/dev/(?:sd|hd|nvme|vd)[a-z])",   # dd 写裸盘
    r"\bmkfs(?:\.[a-z0-9]+)?\s",                          # 格式化文件系统
    r"\bmkswap\s",                                        # 建交换分区
    r"\b(?:fdisk|parted|gdisk|sfdisk)\s+.*/dev/(?:sd|hd|nvme|vd)",  # 分区操作
    r"\bdiskpart\b",                                      # Windows 磁盘工具
    r"\bformat\s+[A-Za-z]:",                              # Windows format C:
    r"\b(?:cryptsetup\s+luksFormat|shred\s+.*/dev/(?:sd|hd|nvme|vd))",  # 加密/擦除磁盘
]

# 关机/重启/挂起
_SHUTDOWN_PATTERNS = [
    r"\b(?:shutdown|reboot|poweroff|halt)\b",
    r"\binit\s+[06]\b",
]

# 提权/系统目录权限破坏
_PRIV_ESC_PATTERNS = [
    r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)?(?:777|666|000)\s+(?:[\x22\x27\x60]?)(?:/|/etc|/usr|/bin|/sbin|/boot|/var|/Windows|/System32|C:[/\\])(?:[\x22\x27\x60]?)",
    r"\bchown\s+-R\s+.*\s+(?:/|/etc|/usr|/bin|/sbin|/boot|/var|/Windows|/System32|C:[/\\])(?:\s|$)",
]

# 反弹 shell / 远程控制
_REVERSE_SHELL_PATTERNS = [
    r"/dev/tcp/",                                          # bash /dev/tcp 反弹
    r"\b(?:nc|ncat|netcat)\s+.*-e\b",                      # nc -e
    r"\b(?:nc|ncat|netcat)\s+.*--exec",                    # nc --exec
    r"\bsocat\s+.*(?:exec|system):",                       # socat 执行
    r"bash\s+-i\s+[<>]|sh\s+-i\s+[<>]",                    # bash -i >& /dev/tcp
]

# 下载即执行链（curl|sh 等）
_DOWNLOAD_EXEC_PATTERNS = [
    r"\bcurl\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b",
    r"\bwget\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b",
    r"\b(?:iwr|Invoke-WebRequest|Invoke-Expression|iex)\b[^|;&]*\|\s*iex\b",
    r"(?:Invoke-Expression|iex)\s*\(",
]

# 挖矿 / 勒索
_MINING_RANSOM_PATTERNS = [
    r"\b(?:xmrig|minerd|cryptonight|cpuminer|kryptex|nanominer)\b",
    r"\b(?:ransom|wannacry|lockbit|encrypting)\b.*\b(?:all|disk|files)\b",
]

# 汇总: (规则名, [正则...])
_DANGEROUS_RULES: List[Tuple[str, List[str]]] = [
    ("系统目录删除", _RM_RF_PATTERNS),
    ("磁盘/分区破坏", _DISK_DESTROY_PATTERNS),
    ("关机/重启", _SHUTDOWN_PATTERNS),
    ("提权/权限破坏", _PRIV_ESC_PATTERNS),
    ("反弹 shell", _REVERSE_SHELL_PATTERNS),
    ("下载执行链", _DOWNLOAD_EXEC_PATTERNS),
    ("挖矿/勒索程序", _MINING_RANSOM_PATTERNS),
]
# 编译一次，复用
_COMPILED_RULES: List[Tuple[str, List[re.Pattern]]] = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in _DANGEROUS_RULES
]


def check_command_safety(command: str) -> Optional[str]:
    """检查命令是否命中危险规则。返回 (危险描述) 或 None（安全）。"""
    if not command or not command.strip():
        return None
    for rule_name, patterns in _COMPILED_RULES:
        for pat in patterns:
            if pat.search(command):
                return f"命令命中「{rule_name}」危险模式: {pat.pattern}"
    return None


# ================= 环境变量清洗 =================
# 白名单前缀（保留）；秘密子串（剔除）；Windows 必备（保留，否则 socket/subprocess 崩）
_SAFE_ENV_PREFIXES = (
    "PATH", "HOME", "USER", "LANG", "LC_", "TERM", "TMPDIR", "TMP", "TEMP",
    "SHELL", "LOGNAME", "XDG_", "VIRTUAL_ENV", "CONDA", "PYTHONPATH", "PYTHONHOME",
    "NODE_", "NPM_", "PNPM_", "YARN_", "JAVA_", "GOPATH", "GOROOT", "RUST_", "CARGO_",
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
    "PSMODULEPATH", "PSExecutionPolicyPreference",
})


def clean_environment() -> Dict[str, str]:
    """构建清洗后的子进程环境。

    规则（顺序）:
      1. 秘密子串（KEY/TOKEN/SECRET/PASSWORD...）剔除 —— 防凭据泄露，这是真正的安全关键。
      2. 白名单前缀保留（PATH/HOME/LANG/TERM/NODE_/NPM_...）。
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
    # 兜底：必须保证 PATH 存在，否则子进程连 bash 都找不到
    if "PATH" not in env:
        env["PATH"] = os.defpath
    # 项目 venv 优先：python/pip 默认指向项目自己的环境（动态推导，GitHub 友好）
    if os.path.isdir(PROJECT_VENV_BIN):
        env["PATH"] = PROJECT_VENV_BIN + os.pathsep + env["PATH"]
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


def _find_bash() -> Optional[str]:
    """定位 bash 可执行文件。Windows 上为 git-bash/MSYS 的 bash。

    优先级:
      1. AETHER_GIT_BASH_PATH 环境变量（显式指定 bash 的完整路径）
      2. git-bash/MSYS 常见安装路径（按安装概率排序）
      3. PATH 兜底（shutil.which）—— 但排除 WSL 启动器

    关键修复: shutil.which("bash") 在 PATH 含 C:\\Windows 时可能命中
    C:\\Windows\\System32\\bash.exe（WSL 启动器），导致命令落到 WSL
    发行版上执行；本机 WSL 磁盘损坏时直接报错/超时。因此任何来源的
    bash 路径只要位于 Windows\\System32 下都会被过滤掉。
    """
    def _is_wsl_launcher(path: str) -> bool:
        """判断是否为 WSL 启动器: 位于 Windows\System32 下的 bash/sh/wsl。"""
        if not path:
            return False
        norm = path.replace("/", "\\").lower()
        return ("windows\\system32" in norm and norm.endswith(".exe")) or "\\wsl" in norm

    candidates: List[str] = []

    # 1. 显式配置优先（AETHER_GIT_BASH_PATH）
    explicit = os.environ.get("AETHER_GIT_BASH_PATH")
    if explicit:
        absp = os.path.abspath(explicit)
        if os.path.isfile(absp) and not _is_wsl_launcher(absp):
            candidates.append(absp)

    # 2. git-bash/MSYS 常见安装位置（比 PATH 兜底更可靠）
    if _IS_WINDOWS:
        for p in [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\msys64\usr\bin\bash.exe",
        ]:
            if os.path.isfile(p) and not _is_wsl_launcher(p):
                candidates.append(p)


    # 2.5 从 PATH 中的 git.exe 推导 git 安装根（跨机器通用，GitHub 友好；
    #     覆盖 git 装在非 C 盘、但 PATH 里有 git 的情况）
    if _IS_WINDOWS:
        try:
            git_exe = shutil.which("git")
        except Exception:
            git_exe = None
        if git_exe:
            git_root = os.path.dirname(os.path.dirname(os.path.abspath(git_exe)))
            for sub in (r"usr\bin\bash.exe", r"bin\bash.exe"):
                p = os.path.join(git_root, sub)
                if os.path.isfile(p) and not _is_wsl_launcher(p):
                    candidates.append(p)

    # 3. PATH 兜底（排除 WSL 启动器）
    try:
        found = shutil.which("bash")
    except Exception:
        found = None
    if found and not _is_wsl_launcher(found):
        candidates.append(found)

    # 去重保序
    seen = set()
    result = []
    for c in candidates:
        key = os.path.normcase(os.path.abspath(c))
        if key not in seen:
            seen.add(key)
            result.append(c)
    return result[0] if result else None


# ================= 主执行函数 =================

def execute_shell(command: str, workdir: Optional[str] = None,
                  timeout: int = DEFAULT_TIMEOUT,
                  stdin_data: Optional[str] = None) -> str:
    """在受限子进程中执行一条 shell 命令，返回 stdout/退出码/错误信息。

    参数:
        command: 要执行的 shell 命令（bash 语法；Windows 上经 git-bash 执行）
        workdir: 工作目录（默认取当前进程目录；不存在时报错）
        timeout: 最大允许执行秒数（自动 clamp 到 [1, MAX_TIMEOUT]）
        stdin_data: 可选，写入子进程 stdin 的字符串（命令需要交互输入时用）

    返回:
        执行结果字符串（包含 stdout/stderr 或错误描述）
    """
    # 0. 参数守卫：clamp 超时
    try:
        timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    if not isinstance(command, str) or not command.strip():
        return "❌ 参数错误: command 必须是非空字符串"

    # 1. 命令安全检查（尽力而为）
    error = check_command_safety(command)
    if error:
        return f"❌ 安全拦截: {error}"

    # 2. 定位 bash
    bash_path = _find_bash()
    if not bash_path:
        return "❌ 找不到 bash 解释器（需要 git-bash / MSYS 或 POSIX 环境）"

    # 3. 解析工作目录
    run_dir = os.getcwd()
    if workdir:
        run_dir = os.path.expanduser(os.path.abspath(workdir))
        if not os.path.isdir(run_dir):
            return f"❌ 工作目录不存在: {run_dir}"

    # 4. 准备干净的运行环境
    clean_env = clean_environment()

    # 5. 启动子进程（独立进程组，可整组强杀）
    popen_kwargs = dict(
        cwd=run_dir,
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        bufsize=0,
    )
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True

    proc = None
    try:
        proc = subprocess.Popen([bash_path, "-c", command], **popen_kwargs)

        # 6. 可选 stdin
        if stdin_data is not None:
            try:
                proc.stdin.write(stdin_data.encode("utf-8", errors="replace"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        # 7. 后台线程流式读输出（head+tail 截断，避免无限输出撑爆内存）
        head_bytes = int(MAX_STDOUT_BYTES * 0.4)
        tail_bytes = MAX_STDOUT_BYTES - head_bytes
        stdout_head, stdout_tail = [], deque()
        stderr_chunks = []
        stdout_total = [0]
        t_out = threading.Thread(
            target=_drain_head_tail,
            args=(proc.stdout, stdout_head, stdout_tail, head_bytes, tail_bytes, stdout_total),
            daemon=True,
        )
        t_err = threading.Thread(
            target=_drain_head, args=(proc.stderr, stderr_chunks, MAX_STDERR_BYTES), daemon=True
        )
        t_out.start(); t_err.start()

        # 8. 轮询：检查退出、超时
        import time as _time
        deadline = _time.monotonic() + timeout
        status = "success"
        while proc.poll() is None:
            if _time.monotonic() > deadline:
                _kill_process_tree(proc)
                status = "timeout"
                break
            try:
                proc.wait(timeout=min(0.05, max(0.0, deadline - _time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
            _time.sleep(0.05)

        t_out.join(timeout=3); t_err.join(timeout=3)

        out_bytes = b"".join(stdout_head) + b"".join(stdout_tail)
        err_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        stdout_text = _format_truncated(stdout_bytes=out_bytes, total=stdout_total[0])
        stderr_text = err_text.strip()

        if status != "success":
            return f"⏰ 执行超时（超过 {timeout} 秒），已强杀整个进程树"

        exit_code = proc.returncode
        output_parts = []
        if stdout_text:
            output_parts.append(stdout_text)
        if stderr_text:
            output_parts.append(f"[stderr]\n{stderr_text}")
        if not output_parts:
            output_parts.append("(命令执行成功，但无输出)")
        if exit_code != 0:
            return f"❌ 执行失败（退出码 {exit_code}）:\n" + "\n".join(output_parts)
        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return f"⏰ 执行超时（超过 {timeout} 秒）"
    except Exception as e:
        return f"❌ 执行异常: {e}"
    finally:
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc)


# ================= 小工具 =================

def _format_truncated(stdout_bytes: bytes, total: int) -> str:
    """将捕获的 stdout 解码为文本，若被截断则追加提示。"""
    text = stdout_bytes.decode("utf-8", errors="replace")
    if total > len(stdout_bytes):
        omitted = total - len(stdout_bytes)
        text += (f"\n\n... [OUTPUT TRUNCATED - {omitted:,} bytes omitted "
                 f"out of {total:,} total] ...\n")
    return text


# ================= 工具的 Schema =================

execute_shell_schema = {
    "type": "function",
    "function": {
        "name": "execute_shell",
        "description": (
            "执行一条 shell 命令，并返回执行结果。"
            "适用于文件/目录操作、git、npm、网络请求、构建脚本等场景。"
            "命令运行在受限子进程中：有超时（默认 30 秒，最大 120 秒）、"
            "输出会截断、环境变量已清洗（凭据不可见）。"
            "高危命令会被拦截（系统目录删除、磁盘格式化、关机重启、反弹 shell、"
            "下载执行链等）；普通文件删除/清理（如 rm -rf node_modules）放行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的 shell 命令（bash 语法，Windows 上经 git-bash 执行）。",
                },
                "workdir": {
                    "type": "string",
                    "description": "工作目录（绝对路径或相对路径，默认当前目录）。",
                },
                "timeout": {
                    "type": "integer",
                    "description": "执行超时时间（秒），默认 30，最大 120。",
                    "default": 30,
                    "minimum": 1,
                    "maximum": MAX_TIMEOUT,
                },
                "stdin_data": {
                    "type": "string",
                    "description": "可选，写入命令标准输入的字符串（需要交互输入时用）。",
                }
            },
            "required": ["command"]
        }
    }
}


# ================= CLI 入口 =================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="受限 shell 命令执行工具")
    parser.add_argument("command", nargs="?", help="要执行的命令（不传则进入交互模式）")
    parser.add_argument("--workdir", "-w", default=None, help="工作目录")
    parser.add_argument("--timeout", "-t", type=int, default=DEFAULT_TIMEOUT,
                        help=f"超时秒数（默认 {DEFAULT_TIMEOUT}，最大 {MAX_TIMEOUT}）")
    parser.add_argument("--stdin", default=None, help="写入 stdin 的字符串")
    args = parser.parse_args()

    if args.command:
        print(execute_shell(args.command, workdir=args.workdir,
                            timeout=args.timeout, stdin_data=args.stdin))
    else:
        # 交互模式：逐行执行（Ctrl+C / exit 退出）
        print("execute_shell 交互模式（输入 exit 退出）")
        while True:
            try:
                cmd = input("shell> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not cmd.strip():
                continue
            if cmd.strip().lower() in ("exit", "quit"):
                break
            print(execute_shell(cmd, workdir=args.workdir, timeout=args.timeout))
