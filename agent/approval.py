# -*- coding: utf-8 -*-
"""approval.py —— 审批引擎（与 agent.py 平级）。

流程（唯一职责：决定"请求能不能传到任务编排器"）
------------------------------------------------
    agent.py 解析完一批 tool_call
      -> gate_batch(items, session_id)
           每项按 approvals/ 里的规范判定：不需批 / 免打扰 / 需人批 / 禁区
           需人批的一律**整批一起问**，全部通过才放行
      -> 通过的原样进 parsed_calls（编排器该怎么串行/并行完全不受影响）
         未通过的补一条 role=tool 响应，把原因交还模型判断

批次原子性（这是设计要点，不是便利）
------------------------------------
同批 a、b 并行时，若 a 未获批，b 也**不得运行**。理由：模型把 a、b 放在同一批，
说明它认为这组动作是**一个意图的整体**；只执行一半可能留下比全不执行更糟的中间态
（例如"移动配置 + 改注册表引用"只做前一步）。审批因此必须按批结算。

失效模式一律 fail-closed：无审批通道 / 超时 / 通道异常 -> 拒绝，且三种情况
返回给模型的文本各不相同（模型需要知道"主人没看到"和"主人说了不行"是两回事）。

环境变量（AETHER_ 前缀）
  AETHER_AUDIT_MODE         off | smart(默认) | strict
  AETHER_AUDIT_ON_ERROR     deny(默认) | allow     引擎自身异常时的取向。
                            曾经默认 allow，结果一个笔误（用了不存在的常量）就让
                            所有该弹窗的操作静默放行且表现完全正常 —— 审批在最需要
                            它的时刻消失。宁可拦住并明写原因，也不静默放过。
  AETHER_APPROVAL_TIMEOUT   等待人类裁决秒数，默认 300
  AETHER_AUDIT_LEDGER       账本，默认 <根>/agent_logs/approval-<YYYYMM>.jsonl
  AETHER_AUDIT_RULES        永久规则，默认 <根>/agent_logs/approval_rules.jsonl
  AETHER_AUDIT_SYSTEMDRIVE  覆盖系统盘判定（默认取 SystemDrive，绝不硬编码 C）
  AETHER_AUDIT_ALLOWLIST    追加密钥分隔的免打扰前缀
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 词法角色切分（approval_lex）。缺失只影响精度，不影响可用性：
# 拿不到它就退回全文扫，那条路径由 self_check 的 lex_layer 字段报出来。
try:
    import approval_lex as _LEX
except Exception:
    _LEX = None

# ============================================================
# 0. 常量
# ============================================================
DECIDE_PASS = "pass"         # 不需审批
DECIDE_QUIET = "quiet"       # 免打扰（记账）
DECIDE_ASK = "ask"           # 需要人批
DECIDE_BLOCK = "block"       # 绝对禁区：不给批

SCOPE_ONCE = "once"
SCOPE_SESSION = "session"
SCOPE_PERSISTENT = "persistent"

# 回给模型的三种未通过原因（第 3 条规格：措辞必须可区分）
WHY_DENY = "本次操作需审批，用户拒绝"
WHY_NO_REPLY = "本次操作需审批，用户未响应（超时）"
WHY_NO_CHANNEL = "本次操作需审批，但当前环境没有可用的审批通道"
WHY_BATCH_COLD = "同批次内有其它操作未获批，本批全部未执行"
WHY_APPROVED_COLD = "本项已获主人批准，但同批次内另有操作未获批准，因此本批一个都未执行。请重新规划：可只提交已获批准的操作，或向主人说明为什么需要被拒的那几项。"

# 绝对禁区：已知必然灾难。不申请审批、不给放行按钮 —— 手滑一次就没有终端了
FORBIDDEN: Tuple[str, ...] = (
    "format ", "mkfs", "diskpart", "clean disk", "dd if=", "cipher /w",
    "rm -rf /", "rm -rf c:/", "rd /s /q c:\\", "del /f /s /q c:\\",
    "remove-item -force -recurse c:\\", "bcdedit", "vssadmin delete",
    "\\system32\\config\\sam", "\\system32\\drivers\\etc\\hosts",
    "shutdown", "reboot", "stop-computer",
)

# 按形态自动分三档，判法不同（手维护三份表迟早漏一条）：
#   PATH   路径片段型：不是命令位能表达的，交给规范层带动作判定 —— 读它合法，写删才致命
#   CMD    单词型：只与命令词精确等值，绝不做子串（否则连查日志都能撞上）
#   PHRASE 短语型：只匹配「从命令词起算」的段文本前缀
BS = chr(92)
EXE_SUFFIX = (".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs", ".msc")
FORBIDDEN_PATH = tuple(x for x in FORBIDDEN if x.startswith(BS))
FORBIDDEN_CMD = tuple(x for x in FORBIDDEN
                      if not x.startswith(BS) and " " not in x.strip() and not x.endswith(" "))
FORBIDDEN_PHRASE = tuple(x for x in FORBIDDEN
                         if x not in FORBIDDEN_PATH and x not in FORBIDDEN_CMD)

SKIP_ARG_NAMES = ("timeout", "top_k", "max_results", "lines", "limit", "depth",
                  "verbose", "logger", "headless", "channel", "model", "force",
                  "record_source", "since", "expression", "type", "mode")
# 不碰本地盘的工具：参数里出现路径字样只是文本（搜索词/URL/提问），扫它们必然误报——
# 而被误报烦到闭眼点「允许」，比没有审批更危险。
NON_FS_TOOLS = frozenset({"search", "multi_search", "fetch_url", "web_reader", "web_extract", "rag_query", "time_weather", "calculator"})


PATH_ARG_HINTS = ("path", "file", "dir", "cwd", "workdir", "target", "dest",
                  "output", "input", "filename", "folder")

_RE_QUOTED = re.compile(r"""['"]([^'"\n]{2,300})['"]""")
# 路径的停止符集合。汉字**不算**停止符：本项目自己就有中文目录名，
# 把它当边界会把真路径截断成漏判。真正要挡的是 CJK 标点与全角符号 ——
# 实测事故：一句 commit 正文里的「C:\Windows）你既看不见…」被整段当成
# 文件目标弹了审批，而主人真给它批了。误拦的代价是训练人闭眼点批准。
_PATH_STOP = ''.join(sorted(set(
    [chr(9), chr(10), chr(13), chr(32), chr(34), chr(39), chr(96),
     chr(59), chr(124), chr(38), chr(40), chr(41), chr(60), chr(62)]
    + [chr(c) for c in range(0x3000, 0x3040)]     # 。、《》「」等 CJK 符号
    + [chr(c) for c in range(0xff00, 0xfff0)]     # ！？＃（）％ 等全角形式
)))
_RE_WIN = re.compile("[A-Za-z]:[" + chr(92) + chr(92) + "/][^"
                     + re.escape(_PATH_STOP) + "]*")
_RE_STOP = re.compile("[" + re.escape(_PATH_STOP) + "]")
_RE_POSIX = re.compile(r"(?<![\w])(/[a-f])(?=[\\/])", re.I)
_RE_ENV = re.compile(r"(?:%|\$env:)(SystemDrive|SystemRoot|WinDir|HOMEDRIVE|USERPROFILE|HOME|TEMP|TMP)(?:%|\b)([^\s\"'`;|&)<>\r\n]*)", re.I)


# ---- shell 家目录引用的展开（判据必须与解释器同源）----
# 实测事故（2026-09-11 安全审计 probe5，10/10 漏判）：审批只认字面路径，下面这些
# 写法在旧版里全是 pass，而它们在 bash/python 里**真能执行**：
#     printf x > $USERPROFILE/Desktop/leak.txt
#     printf x > ~/Desktop/leak.txt
#     open(os.path.expanduser('~/Desktop/leak.txt'),'w')
#     open(os.environ['USERPROFILE']+'/Desktop/leak.txt','w')
# 不展开它们，「系统盘写要审批」这条规则只要多打一个 $ 或 ~ 就能绕过。
# 注：_RE_ENV 只认 %VAR% 与 $env:VAR（cmd/PowerShell 形态），shell 的 $VAR 不在其中。
_RE_SHVAR = re.compile(
    r"\$\{?(SystemDrive|SystemRoot|WinDir|HOMEDRIVE|USERPROFILE|HOME|TEMP|TMP)\}?",
    re.I)
# cmd/PowerShell 形态的同一个变量：%USERPROFILE%\…
_RE_CMDVAR = re.compile(
    r"%(SystemDrive|SystemRoot|WinDir|HOMEDRIVE|USERPROFILE|HOME|TEMP|TMP)%",
    re.I)
_RE_TILDE = re.compile(r"(?<![\w~])~(?=[\\/]|$)")
# 本机管理共享：\\localhost\c$\… 与 c:\… 是同一个位置
_RE_UNC_LOCAL = re.compile(r"^//(?:localhost|127\.0\.0\.1|\[::1\])/([a-z])\$(?:/(.*))?$")


def _expand_shell_env(text: str, droot: str) -> str:
    """把 shell 风格的家目录引用就地展开成绝对路径文本（只展开已知变量）。

    认不出的变量原样留着：凭空造路径只会制造误报，而误报会把主人训练成
    闭眼点「允许」—— 那比没有审批更坏（本项目的既有取向）。
    """
    if not text or ("$" not in text and "~" not in text and "%" not in text):
        return text
    user = (os.environ.get("USERPROFILE") or os.environ.get("HOME")
            or (droot.rstrip("/") + "/Users/user"))
    windir = os.environ.get("WINDIR") or (droot.rstrip("/") + "/Windows")
    tmp = (os.environ.get("TEMP") or os.environ.get("TMP")
           or (user + "/AppData/Local/Temp"))
    table = {
        "systemdrive": droot.rstrip("/"), "homedrive": droot.rstrip("/"),
        "systemroot": windir, "windir": windir,
        "userprofile": user, "home": user, "temp": tmp, "tmp": tmp,
    }
    out = _RE_SHVAR.sub(lambda m: table.get(m.group(1).lower(), m.group(0)), text)
    out = _RE_CMDVAR.sub(lambda m: table.get(m.group(1).lower(), m.group(0)), out)
    return _RE_TILDE.sub(lambda m: user, out)


# ============================================================
# 1. 配置
# ============================================================
def _root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def mode() -> str:
    m = _env("AETHER_AUDIT_MODE", "smart").lower()
    return m if m in ("off", "smart", "strict") else "smart"


def timeout_sec() -> int:
    try:
        return max(5, min(3600, int(_env("AETHER_APPROVAL_TIMEOUT", "300"))))
    except ValueError:
        return 300


def drive() -> str:
    """系统盘根，形如 'c:/'。默认读 SystemDrive，绝不硬编码。"""
    raw = _env("AETHER_AUDIT_SYSTEMDRIVE") or os.environ.get("SystemDrive") or "C:"
    raw = raw.strip().rstrip("\\/")
    raw = raw[:2] if len(raw) >= 2 and raw[1] == ":" else "c:"
    return raw.lower() + "/"


def ledger_path() -> str:
    return _env("AETHER_AUDIT_LEDGER") or os.path.join(
        _root(), "agent_logs", "approval-%s.jsonl" % time.strftime("%Y%m"))


def rules_path() -> str:
    return _env("AETHER_AUDIT_RULES") or os.path.join(
        _root(), "agent_logs", "approval_rules.jsonl")


# ============================================================
# 2. 路径归一与提取
# ============================================================
def unify(s: str, cwd: str = "") -> str:
    """小写盘符、正斜杠、无尾斜杠、尽量绝对化。

    归一**之前**先展开 shell 家目录引用（`$USERPROFILE/…`、`~/…`）：
    判据必须与解释器同源 —— 多打一个 `$` 不该让同一个位置变成两个结论。
    放在这里，是因为所有路径都经此函数（规范层取目标走 `resolve()` → 本函数）。
    """
    if not s:
        return ""
    t = _expand_shell_env(str(s).strip().strip('"').strip("'"), drive())
    if not t:
        return ""
    if t.startswith("\\\\?\\") or t.startswith("//?/"):
        t = t[4:]
    t = "/".join(t.split("\\"))
    low = t.lower()
    if len(low) >= 2 and low[1] == ":":
        t = low
    else:
        m = re.match(r"^/+([a-f])(?:/(.*|))$", low)
        if m:
            t = m.group(1) + ":/" + (m.group(2) or "")          # git-bash 的 /c/...
        elif low.startswith("//"):
            m_unc = _RE_UNC_LOCAL.match(low)
            if m_unc:
                # 本机管理共享 \\localhost\c$\x 与 c:\x 是同一个文件。实测该写法
                # 能真写系统盘，而旧归一把它当「网络路径」整条放过。
                t = "%s:/%s" % (m_unc.group(1), m_unc.group(2) or "")
            else:
                return low.strip("/")
        elif not low.startswith("/"):
            try:
                t = "/".join(os.path.abspath(os.path.join(cwd or os.getcwd(), t)).split("\\"))
            except Exception:
                t = low
        else:
            t = "/" + low.lstrip("/")
    while len(t) > 3 and t.endswith("/"):
        t = t[:-1]
    return t


def expand_env(token: str, droot: str) -> List[str]:
    win = droot.rstrip("/")
    user = os.environ.get("USERPROFILE") or os.environ.get("HOME") or (win + "/Users/user")
    windir = os.environ.get("WINDIR") or (win + "/Windows")
    temp = os.environ.get("TEMP") or (user + "/AppData/Local/Temp")
    table = {"systemdrive": win + "/", "homedrive": win + "/", "systemroot": windir,
             "windir": windir, "userprofile": user, "home": user, "temp": temp, "tmp": temp}
    low = token.lower()
    out: List[str] = []
    for name, val in table.items():
        for pat in ("%" + name + "%", "$env:" + name):
            idx = low.find(pat)
            if idx < 0:
                continue
            rest = token[idx + len(pat):]
            m = _RE_STOP.search(rest)
            if m:
                rest = rest[:m.start()]
            base = (val or "").rstrip("/")
            if not base:
                continue
            out.append(base)
            if rest:
                out.append(base + "/" + rest.lstrip("\\/"))
    return out


def extract_paths(kwargs: Dict[str, Any], cwd: str = "") -> List[str]:
    """扫**全部**字符串参数（不按名字表白名单），新工具/新参数自动被覆盖。"""
    cands: List[str] = []
    for slot, val in (kwargs or {}).items():
        if val is None or slot in SKIP_ARG_NAMES or isinstance(val, bool):
            continue
        items = ([str(x) for x in val.values()] if isinstance(val, dict)
                 else [str(x) for x in val] if isinstance(val, (list, tuple))
                 else [str(val)])
        whole = any(h in slot.lower() for h in PATH_ARG_HINTS)
        for text in items:
            text = text.strip()
            if not text:
                continue
            if whole and chr(10) not in text:
                cands.append(text)
            for q in _RE_QUOTED.findall(text):
                qs = q.strip()
                if re.match(r"^[A-Za-z]:[\\/]", qs) or qs.startswith("//"):
                    cands.append(qs)
            cands.extend(_RE_WIN.findall(text))
            for m in _RE_POSIX.finditer(text):
                rest = text[m.end():]
                stop = _RE_STOP.search(rest)
                cands.append(m.group(1) + (rest[:stop.start()] if stop else rest))
            for mm in _RE_ENV.finditer(text):
                cands.extend(expand_env(mm.group(0), drive()))
    seen, out = set(), []
    for c in cands:
        u = unify(c, cwd)
        if u and len(u) > 2 and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def resolve_path(text: str, cwd: str = "") -> str:
    """给规范层用的路径归一：实参文本 -> 绝对路径。

    归一规则只有一处（unify），规范层不重复实现 —— 两处规则迟早分叉。
    """
    return unify(str(text or ""), cwd or _root())


def parent_of(p: str) -> str:
    base = p.rstrip("/").rsplit("/", 1)
    return base[0] if base and base[0] else p


def is_drive_root(p: str) -> bool:
    """盘根判定：绝不允许把整盘存成授权前缀（点一下=授权全盘是陷阱）。"""
    return not p or p.rstrip("/") + "/" == drive()


# 授权前缀在盘符之后至少要有几段。事故实据（账本 16:15）：主人点了一次「永久允许」，
# 目标文件位于用户主目录，父目录即 c:/users/xxx —— 桌面、文档、下载从此全部免审；
# 同一批还签发了 c:/Windows。免打扰的便利换来一次点击拆掉审批门，不划算。
MIN_SCOPE_SEGMENTS = 3


def scope_depth_ok(prefix: str) -> bool:
    """前缀够不够深。太浅等于把整棵子树交出去，一律不签发。"""
    pu = unify(prefix)
    if not pu or is_drive_root(pu):
        return False
    parts = pu.split(":", 1)
    segs = [x for x in (parts[1] if len(parts) > 1 else parts[0]).split("/") if x]
    return len(segs) >= MIN_SCOPE_SEGMENTS


# ============================================================
# 3. 审批规范装载（agent/approvals/）
# ============================================================
_SPEC_STATE: Dict[str, Any] = {"loaded": [], "failed": []}


def specs():
    try:
        import approvals                      # noqa: PLC0415
        found = approvals.load_specs()
        _SPEC_STATE["loaded"] = [getattr(s, "KIND", "?") for s in found]
        _SPEC_STATE["failed"] = list(getattr(approvals, "SPECS_LOAD_ERRORS", []))
        return found
    except Exception as e:
        _SPEC_STATE["failed"] = ["approvals 包不可用: %s: %s" % (e.__class__.__name__, e)]
        return []


def specs_state() -> Dict[str, Any]:
    specs()
    return dict(_SPEC_STATE)


# ============================================================
# 4. 作用域（once / session / persistent）
# ============================================================
class Scopes:
    """锁纪律：本类方法内**只操作内存字典与文件**，绝不调用审批通道（防自死锁）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sess: Dict[str, List[Dict[str, Any]]] = {}
        self._pers: List[Dict[str, Any]] = []
        self._loaded = False

    def add(self, scope: str, session_id: str, prefix: str, kind: str,
            ask_id: str = "") -> bool:
        pu = unify(prefix)
        parent = parent_of(pu)
        if is_drive_root(pu) or is_drive_root(parent) or not scope_depth_ok(parent):
            return False
        rule = {"path": parent_of(pu).rstrip("/").lower(), "kind": kind or "",
                "scope": scope, "session_id": session_id or "-", "ask_id": ask_id,
                "ts": round(time.time(), 3)}
        with self._lock:
            if scope == SCOPE_PERSISTENT:
                self._ensure()
                if not any(r["path"] == rule["path"] for r in self._pers):
                    self._pers.append(rule)
                    self._persist_rule(rule)
            elif scope == SCOPE_SESSION and session_id:
                b = self._sess.setdefault(session_id, [])
                if not any(r["path"] == rule["path"] for r in b):
                    b.append(rule)
            else:
                return False
        return True

    def match(self, session_id: str, path: str, kind: str = "") -> str:
        pu = unify(path)
        if not pu:
            return ""
        with self._lock:
            self._ensure()
            for name, bucket in ((SCOPE_PERSISTENT, self._pers),
                                 (SCOPE_SESSION, self._sess.get(session_id or "", []))):
                for r in bucket:
                    if r.get("kind") and kind and r.get("kind") != kind:
                        continue
                    pre = (r.get("path") or "").rstrip("/")
                    if not pre or is_drive_root(pre):
                        continue
                    if pu == pre or pu.startswith(pre + "/"):
                        return name
        return ""

    def list_rules(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._ensure()
            return list(self._pers)

    def list_all(self, session_id: str = "") -> List[Dict[str, Any]]:
        """当前**真正生效**的全部规则：永久 + 该会话的会话级。
        面板必须看这个，只看 jsonl 会漏掉会话级规则，也看不出内存与磁盘已经不一致。"""
        with self._lock:
            self._ensure()
            rows: List[Dict[str, Any]] = []
            for name, bucket in ((SCOPE_PERSISTENT, self._pers),
                                 (SCOPE_SESSION, self._sess.get(session_id or "", []))):
                for r in bucket:
                    d = dict(r)
                    d["scope"] = name
                    d["drive_root"] = drive()
                    rows.append(d)
            return rows

    @staticmethod
    def _same(rule: Dict[str, Any], pu: str) -> bool:
        if not pu:
            return True                      # 空路径 = 全部撤销（一键止血用）
        return unify(rule.get("path") or "") == pu

    def _rewrite(self) -> None:
        """全量重写规则文件：撤销必须落盘，否则重启就诈尸。先写临时文件再替换。"""
        rp = rules_path()
        os.makedirs(os.path.dirname(rp) or ".", exist_ok=True)
        tmp = rp + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline=chr(10)) as fh:
            for r in self._pers:
                fh.write(json.dumps(r, ensure_ascii=False) + chr(10))
        os.replace(tmp, rp)

    def revoke(self, path: str = "", scope: str = "", session_id: str = "") -> int:
        """撤销规则：内存与落盘一起改。
        今天的事故就是只清了文件、没清内存 —— 进程里那 3 条越权规则照旧免审。"""
        pu = unify(path)
        n = 0
        with self._lock:
            self._ensure()
            if scope in ("", SCOPE_PERSISTENT):
                keep = [r for r in self._pers if not self._same(r, pu)]
                n += len(self._pers) - len(keep)
                self._pers = keep
                try:
                    self._rewrite()
                except OSError:
                    ledger({"event": "revoke_write_failed", "path": pu})
            if scope in ("", SCOPE_SESSION) and session_id in self._sess:
                b = self._sess.get(session_id) or []
                keep2 = [r for r in b if not self._same(r, pu)]
                n += len(b) - len(keep2)
                self._sess[session_id] = keep2
        if n:
            ledger({"event": "scope_revoked", "count": n, "path": pu,
                    "scope": scope or "any", "session_id": session_id or "-"})
        return n

    def _ensure(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            rp = rules_path()
            if not os.path.exists(rp):
                return
            with open(rp, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get("path"):
                        self._pers.append(obj)
        except OSError:
            pass

    @staticmethod
    def _persist_rule(rule: Dict[str, Any]) -> None:
        try:
            rp = rules_path()
            os.makedirs(os.path.dirname(rp) or ".", exist_ok=True)
            with open(rp, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rule, ensure_ascii=False) + chr(10))
        except OSError:
            pass


SCOPES = Scopes()


# ============================================================
# 5. 账本（留痕 != 打断：免打扰与放行的也记，靠它替代弹窗）
# ============================================================
_LEDGER_LOCK = threading.Lock()
_LEDGER_ERR: Dict[str, str] = {"why": ""}


def ledger(rec: Dict[str, Any]) -> None:
    r = dict(rec)
    r.setdefault("ts", round(time.time(), 3))
    r.setdefault("iso", time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        lp = ledger_path()
        os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
        line = json.dumps(r, ensure_ascii=False, default=str)
        if len(line) > 12000:
            line = line[:9000] + json.dumps({"truncated": True})[1:-1] + "}"
        with _LEDGER_LOCK:
            with open(lp, "a", encoding="utf-8") as fh:
                fh.write(line + chr(10))
    except OSError as e:
        _LEDGER_ERR["why"] = "%s: %s" % (e.__class__.__name__, e)


# ============================================================
# 6. 审批端口（宿主注入；引擎永不知道 UI 存在）
# ============================================================
@dataclass
class Decision:
    choice: str = "E"        # A 本次 / B 本会话此路径 / D 永久 / E 拒 / F 拒并中止
    how: str = "answered"    # answered|expired|no_channel|skipped|error|cancelled
    # 主人裁决时手打的话。归引擎而不是归界面：回给模型的文案、账本留痕、CLI 解析
    # 都得同一份语义，前端只负责把字送回来。
    note: str = ""


MAX_NOTE = 400               # 给模型的补充说明上限，防止一段长文把上下文挤掉


def _with_note(base: str, note: str) -> str:
    n = (note or "").strip()
    if not n:
        return base
    if len(n) > MAX_NOTE:
        n = n[:MAX_NOTE] + "…"
    return base + "（主人补充：" + n + "）"


class ApprovalPort:
    """宿主实现 request_many（并发/批量）。默认实现逐条调用 request。"""

    name = "abstract"

    def request(self, req: Dict[str, Any]) -> Decision:
        raise NotImplementedError

    def request_many(self, reqs: List[Dict[str, Any]]) -> List[Decision]:
        out: List[Decision] = []
        for r in reqs:
            try:
                d = self.request(r)
            except Exception as e:
                d = Decision("E", "error")
                ledger({"event": "channel_error", "ask_id": r.get("ask_id"),
                        "err": "%s: %s" % (e.__class__.__name__, e)})
            out.append(d)
            if d.choice in ("E", "F"):
                # 本批已注定不放行，剩下的不再打扰主人（但要把原因交代清楚）
                for rest in reqs[len(out):]:
                    out.append(Decision("E", "skipped"))
                break
        return out


class NullPort(ApprovalPort):
    """没有任何通道：明确报"没有通道"，绝不冒充"用户拒绝"。"""
    name = "null"

    def request(self, req: Dict[str, Any]) -> Decision:
        return Decision("E", "no_channel")


class ConsolePort(ApprovalPort):
    """CLI：纯文字，但把"要干什么 + 源代码 + 后果"一次说清。"""
    name = "console"

    def request(self, req: Dict[str, Any]) -> Decision:
        if not sys.stdin or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            return Decision("E", "no_channel")
        bar = "─" * 62
        print(chr(10) + "┌" + bar + "┐")
        print("│ 🔴 审批请求  %s%s" % (req.get("title", ""), " " * max(0, 50 - _w(req.get("title", "")))))
        print("├" + bar + "┤")
        print("│ AB 想要：%s" % req.get("intent", ""))
        print("│ 依据    ：%s" % req.get("reason", ""))
        for ln in (req.get("notes") or []):
            print("│ 提示    ：%s" % ln)
        src = (req.get("source_code") or "").split(chr(10))
        print("├" + bar + "┤")
        print("│ 操作源代码（%d 行）：" % len(src))
        for ln in src[:20]:
            print("│   %s" % ln[:150])
        if len(src) > 20:
            print("│   …（另有 %d 行，完整见会话记录）" % (len(src) - 20))
        print("├" + bar + "┤")
        print("│ 🟢 A 允许本次    🟡 B 本会话允许此路径    ⚪ D 永久允许")
        print("│ 🔴 E 拒绝        ⛔ F 拒绝并中止本回合")
        print("└" + bar + "┘")
        try:
            ans = input("请选择 [A/B/D/E/F]（直接回车 = 拒绝；"
                        "可加说明，如「E 先别动这个目录」）: ").strip()
        except (EOFError, OSError, KeyboardInterrupt):
            return Decision("E", "cancelled")
        parts = ans.split(None, 1)
        key = (parts[0] if parts else "").upper()
        note = (parts[1] if len(parts) > 1 else "")
        if len(note) > MAX_NOTE:
            note = note[:MAX_NOTE] + "…"
        return Decision(key if key in ("A", "B", "D", "E", "F") else "E", "answered", note)


def _w(s: str) -> int:
    """粗略宽度（中文按 2 格），只为对齐边框，不参与判定。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in (s or ""))


_PORT: ApprovalPort = ConsolePort()
_PORT_LOCK = threading.Lock()


def set_port(p: Optional[ApprovalPort]) -> None:
    global _PORT
    with _PORT_LOCK:
        _PORT = p if p is not None else NullPort()


def get_port() -> ApprovalPort:
    with _PORT_LOCK:
        return _PORT


# ============================================================
# 7. 判定
# ============================================================
def _forbidden_at_cmdline(lx: Dict[str, Any]) -> str:
    """只在命令位上匹配绝对禁区。命中返回该模式，否则空串。

    这一层存在的原因：绝对区里的单词（那类关机重启命令）做子串匹配时，
    连「在代码里查这些端点名」这种纯只读动作都会被拦下 —— 现场被咬过九次。
    """
    words = set()
    for w in (lx.get("cmdwords") or []):
        w = (w or "").lower()
        for cand in (w, w.rsplit(BS, 1)[-1].rsplit("/", 1)[-1], w.rsplit(".", 1)[-1]):
            words.add(cand)
            # 带可执行后缀的写法（绝对路径或裸扩展名）剥掉后缀再比一次，
            # 否则加个 .exe 就能从命令位上溜走
            for suf in EXE_SUFFIX:
                if cand.endswith(suf):
                    words.add(cand[:-len(suf)])
    segs = [(x.get("text") or "") for x in (lx.get("segments") or [])]
    for pat in FORBIDDEN_CMD:
        if pat.lower() in words:
            return pat
    for pat in FORBIDDEN_PHRASE:
        pre = pat.lower()
        for t in segs:
            if t.startswith(pre):
                return pat
    return ""


def _tool_only_hit(tool: str, kwargs: Dict[str, Any], cwd: str
                   ) -> Optional[Tuple[Any, Dict[str, Any]]]:
    """没有路径/动作点时，仍按**工具名**问一遍规范。

    存在的理由：「参数里没有路径」被当成了「没事可审」。实测事故 ——
    `skillhub_install` 的参数是 `owner/repo/技能路径` 形式的标识，
    于是「安装第三方技能（内容会随注册表注入系统提示）」整条免审。
    任何新工具只要不用路径参数，就自动落到这个盲区里。

    只问规范层，不做文件系统分析；规范内部异常按 fail-closed 记账并跳过
    （与 inspect_one 主路径同口径）。
    """
    ctx = {"tool": tool, "kwargs": kwargs, "paths": [], "operands": [], "mentions": [],
           "actions": [], "facts": {}, "control": "",
           "forbidden_path": FORBIDDEN_PATH, "drive": drive(),
           "root": unify(_root()), "cwd": cwd or _root(),
           "resolve": resolve_path, "opaque_paths": [], "blob": "", "mode": mode()}
    for sp in specs():
        try:
            if not sp.applies(ctx):
                continue
            f = sp.finding(ctx)
        except Exception as e:
            ledger({"event": "spec_error", "spec": getattr(sp, "KIND", "?"),
                    "err": "%s: %s" % (e.__class__.__name__, e), "handling": "ask",
                    "where": "tool_only"})
            continue
        if f:
            return sp, f
    return None


def inspect_one(tool: str, kwargs: Dict[str, Any],
                session_id: str = "", cwd: str = "") -> Dict[str, Any]:
    v: Dict[str, Any] = {"tool": tool, "decision": DECIDE_PASS, "paths": [],
                         "operands": [], "mentions": [], "facts": {},
                         "targets": [], "kind": "", "risk": 0, "reason": "",
                         "intent": "", "notes": [], "critical": False,
                         "fingerprint": "", "args": kwargs or {}}
    try:
        if mode() == "off":
            v["reason"] = "审计已关闭"
            return v
        blob = " ".join(str(x) for x in (kwargs or {}).values()).lower()
        # 词法分层：只有「指令层」参与禁区判定。strict 档退回全文扫（宁误拦不漏拦）。
        lx = None
        if _LEX is not None and mode() != "strict":
            try:
                lx = _LEX.lex(tool, kwargs)
                if lx.get("failed"):
                    ledger({"event": "lex_fallback", "tool": tool})
            except Exception as e:
                ledger({"event": "lex_error", "tool": tool,
                        "err": "%s: %s" % (e.__class__.__name__, e)})
                lx = None
        instr = blob if lx is None else lx["control_text"]
        if lx is None:
            # 词法层不可用，或 strict 档：退回全文扫，宁误拦不漏拦
            pat = next((x for x in FORBIDDEN if x.lower() in instr), "")
        else:
            pat = _forbidden_at_cmdline(lx)
        if pat:
            v["decision"] = DECIDE_BLOCK
            v["kind"] = "forbidden"
            v["reason"] = "命中绝对禁区「%s」：此类操作不提供授权入口" % pat
            v["intent"] = "执行被禁止的破坏性命令"
            return v
        if lx is not None:
            men = lx["mention_text"].lower()
            hits = [pat for pat in FORBIDDEN if pat.lower() in men]
            if hits:
                # 禁区词只出现在正文/注释里 = 有人在读写一段提到它的文本，不是要执行它。
                # 记账不打扰：拦这个只会把人训练成闭眼点「允许」，那比没审批更危险。
                ledger({"event": "payload_hit", "tool": tool, "patterns": hits[:6],
                        "session_id": session_id or "-"})
                v["payload_hits"] = hits[:6]
        if tool in NON_FS_TOOLS:
            v["reason"] = "工具不接触本地文件系统，跳过路径分析"
            return v
        actions = list((lx or {}).get("actions") or [])
        if lx is None:
            v["operands"] = extract_paths(kwargs, cwd)
            v["mentions"] = []
        else:
            v["operands"] = extract_paths({"operand": lx["operand_text"]}, cwd)
            ops = set(v["operands"])
            v["mentions"] = [x for x in extract_paths({"mention": lx["mention_text"]}, cwd)
                             if x not in ops]
        v["actions"] = actions
        v["paths"] = v["operands"] + v["mentions"]
        if not v["paths"] and not actions:
            # 「没有路径参数」不等于「没事可审」：有些规范按**工具名**判定
            # （skillhub_install 的参数是 owner/repo 标识，不是路径）。
            # 实测事故：这条 return 让「安装第三方技能」整条免审。
            th = _tool_only_hit(tool, kwargs, cwd)
            if th is None:
                v["reason"] = "无路径参数，且按工具名的规范均未命中"
                return v
            sp, f = th
            v["kind"] = getattr(sp, "KIND", "?")
            v["title"] = getattr(sp, "TITLE", "")
            v["risk"] = int(getattr(sp, "RISK", 1))
            v["targets"] = list(f.get("targets") or [])
            v["intent"] = f.get("intent", "")
            v["reason"] = f.get("reason", "")
            v["notes"] = list(f.get("notes") or [])
            v["critical"] = bool(f.get("critical"))
            if f.get("block"):
                v["decision"] = DECIDE_BLOCK
            elif f.get("quiet"):
                v["decision"] = DECIDE_QUIET if mode() != "strict" else DECIDE_ASK
            else:
                v["decision"] = DECIDE_ASK
                v["fingerprint"] = _fp(tool, v["kind"], v["targets"])
            ledger({"event": "tool_spec_hit", "tool": tool, "kind": v["kind"],
                    "decision": v["decision"]})
            return v
        droot, rroot = drive(), unify(_root())
        ctx = {"tool": tool, "kwargs": kwargs, "paths": v["paths"],
               "operands": v["operands"], "mentions": v["mentions"],
               # 每个动作点补上 cwd：相对路径必须在「命令实际所在目录」下绝对化，
               # 否则同一个参数在判定里会变成另一个文件。
               "actions": [dict(a, cwd=a.get("cwd") or (cwd or _root())) for a in actions],
               "facts": lx or {}, "control": instr,
               "forbidden_path": FORBIDDEN_PATH,
               "drive": droot, "root": rroot, "cwd": cwd or _root(),
               "resolve": resolve_path, "opaque_paths": [],
               "blob": blob, "session_id": session_id, "mode": mode()}
        # 候选目标只取**操作数层**。这里曾有一条「操作数为空就采纳提及层」的回退，
        # 用来兜「路径经变量传递」的 ctypes 写法 —— 那是漏判补丁制造误报的典型：
        # 实测它把脚本里一行盘符常量弹成「对盘符执行写入/覆盖」，而那次任务与系统盘
        # 毫无关系。变量传路径现在由词法层的常量变量表负责（approval_lex._const_vars），
        # 既保住那个漏判，也不再拿被提到的路径当目标。
        targets = [p for p in v["operands"] if p.startswith(droot) or p.startswith(rroot)]
        if not targets and lx is None:
            # 词法层不可用时没有任何结构可依，退回旧的全量判定（宁误拦不漏拦）。
            targets = [p for p in v["operands"]]
        # 读不懂的代码体（语法过不去 / 两种解析器都出不了结构）单独走一条窄路：
        # 它没有可绑定的实参，若就此静默放行，等于「把代码写乱就能过关」。
        # 但也不许把体内的字面量当操作目标（那正是刚修掉的误报来源）——
        # 所以这些路径只作为「提示性上下文」交给规范层，规范层负责明说它是猜的。
        opaque_paths = []
        for a in actions:
            if not (a.get("opaque") or a.get("word") == "?unparsed"):
                continue
            for p in extract_paths({"m": str(a.get("blob") or a.get("opaque") or a.get("text") or "")}, cwd):
                if (p.startswith(droot) or p.startswith(rroot)) and p not in opaque_paths:
                    opaque_paths.append(p)
        ctx["opaque_paths"] = opaque_paths[:6]
        if opaque_paths:
            ledger({"event": "opaque_body", "tool": tool, "paths": opaque_paths[:4]})
        if not targets and not opaque_paths and not actions and not v["operands"]:
            # 有动作点、或操作数层非空，都必须往下走：规范层是从动作点的实参
            # （以及操作数层）取目标的，而上面那份 targets 只收「系统盘 + 项目根」——
            # 项目外的读与写（outzone.write / secrets.read）在它眼里是空的。
            # 若在这里返回，等于「换个盘就没人管」。
            v["reason"] = ("%d 个路径均非本次操作目标（盘外或仅被提及）" % len(v["paths"])
                           if v["mentions"] else
                           "%d 个路径均在覆盖面之外" % len(v["paths"]))
            return v
        hit = None
        spec_err = None
        for sp in specs():
            try:
                if not sp.applies(ctx):
                    continue
                f = sp.finding(ctx)
            except Exception as e:
                # 🔴 规范内部异常**不许**当成「没命中」放行。这条 continue 曾是残留的
                # fail-open：实测规范炸掉时「写系统盘」直接判 pass，而 AETHER_AUDIT_ON_ERROR
                # 管不到它（那管的是引擎顶层异常）。异常一律升级为需人裁决 —— 可批，
                # 所以不至于把修引擎的路也堵死。
                spec_err = (sp, e)
                ledger({"event": "spec_error", "spec": getattr(sp, "KIND", "?"),
                        "err": "%s: %s" % (e.__class__.__name__, e),
                        "handling": "ask"})
                continue
            if f:
                hit = (sp, f)
                break
        if not hit and spec_err is not None:
            sp, e = spec_err
            v["decision"] = DECIDE_ASK
            v["kind"] = getattr(sp, "KIND", "?")
            v["title"] = getattr(sp, "TITLE", "审批规范异常")
            v["risk"] = int(getattr(sp, "RISK", 2))
            v["targets"] = targets[:6]
            v["critical"] = True
            v["reason"] = "审批规范 %s 内部异常（%s: %s），按 fail-closed 交人工确认" % (
                v["kind"], e.__class__.__name__, str(e)[:120])
            v["intent"] = "规范判定失败，改动目标按操作数层原样列出"
            v["notes"] = ["⚠️ 这是闸门自身的缺陷，不是这次操作有问题 —— "
                          "批准前建议先让 AB 修引擎，否则同一处会反复失效"]
            v["fingerprint"] = _fp(tool, v["kind"], v["targets"])
            ledger({"event": "spec_fail_closed", "spec": v["kind"],
                    "targets": v["targets"][:4]})
            return v
        if not hit:
            v["decision"] = DECIDE_PASS
            v["reason"] = "系统盘内只读语义，不申请审批（已记账）"
            return v
        sp, f = hit
        if f.get("block"):
            v["decision"] = DECIDE_BLOCK
            v["kind"] = getattr(sp, "KIND", "?")
            v["targets"] = f.get("targets") or targets
            v["critical"] = True
            v["reason"] = f.get("reason", "命中不可授权的关键系统路径")
            v["intent"] = f.get("intent", "改写关键系统文件")
            ledger({"event": "spec_block", "spec": v["kind"], "targets": v["targets"][:6]})
            return v
        v["kind"] = getattr(sp, "KIND", "?")
        v["title"] = getattr(sp, "TITLE", "")
        v["risk"] = int(getattr(sp, "RISK", 1))
        _gt = f.get("targets")
        # 规范可以显式给空列表（「看不懂这段代码、目标无法确定」），
        # 那种情况不许偷偷回退成整条操作数 —— 那是拿假目标骗人点批准。
        v["targets"] = list(_gt) if _gt is not None else list(targets)
        v["critical"] = bool(f.get("critical"))
        v["reason"] = f.get("reason", "")
        v["intent"] = f.get("intent", "")
        v["notes"] = list(f.get("notes") or []) + (
            ["目标位于系统关键目录，误改可能导致系统异常"] if v["critical"] else [])
        v["reversibility"] = _reversibility(str(f.get("action") or ""), v["targets"])
        v["purpose"] = _purpose_of(kwargs)
        if v["reversibility"]:
            v["notes"].append("可逆性：" + v["reversibility"])
        if f.get("quiet"):
            v["decision"] = DECIDE_QUIET if mode() != "strict" else DECIDE_ASK
            return v
        v["decision"] = DECIDE_ASK
        v["fingerprint"] = _fp(tool, v["kind"], v["targets"])
        return v
    except Exception as e:
        v["decision"] = (DECIDE_BLOCK if _env("AETHER_AUDIT_ON_ERROR", "deny").lower() != "allow"
                         else DECIDE_PASS)
        v["kind"] = "engine.error"
        v["reason"] = "引擎异常，按 %s 取向处理: %s: %s" % (
            "allow" if v["decision"] == DECIDE_PASS else "deny",
            e.__class__.__name__, e)
        ledger({"event": "engine_error", "tool": tool, "detail": v["reason"]})
        return v


def _reversibility(action: str, targets: Sequence[str]) -> str:
    """人做裁决时最在意的一件事：能不能撤回。引擎来判，不靠 AB 自述。

    过去卡片上「写个探针文件」和「删掉已有文件」长得一模一样，
    于是主人只能靠猜 —— 靠猜的批准不算审批。
    """
    if not targets:
        return ""
    first = (targets[0] or "").replace("/", os.sep)
    try:
        exists = os.path.exists(first)
    except OSError:
        exists = False
    if "删除" in action:
        return ("不可逆：目标已存在，删除后无法自动恢复（回收站不收命令行删除）"
                if exists else "目标不存在，这次删除实际不会改动任何东西")
    if "移动" in action or "重命名" in action:
        return "改路径/改名：可逆，但请记住原名"
    if "写入" in action or "覆盖" in action:
        return ("覆盖已存在文件：原内容会丢失，不可自动撤回" if exists
                else "新建文件：可逆，删掉即可撤回")
    return ""


def _purpose_of(kwargs: Dict[str, Any]) -> str:
    """从 AB 自己写的命令里提取它声明的用途（首行注释）。

    这是一条问责机制：没声明就在卡片上明写「未声明，建议先问它」，
    让主人手里有比"猜"更好的选项。
    """
    for key in ("command", "code", "script", "implementation_code", "cmd"):
        val = (kwargs or {}).get(key)
        if not isinstance(val, str):
            continue
        for line in val.split(chr(10))[:8]:
            t = line.strip()
            if t.startswith("#") or t.startswith("//"):
                body = t.lstrip("#/").strip()
                if len(body) >= 6:
                    return body[:160]
    return ""


def _fp(tool: str, kind: str, paths: Sequence[str]) -> str:
    import hashlib
    return hashlib.sha1((tool + "|" + kind + "|" + "|".join(sorted(paths)[:6]))
                        .encode("utf-8", "replace")).hexdigest()[:12]


def _source_of(kwargs: Dict[str, Any], limit: int = 6000) -> str:
    parts = []
    for k, v in (kwargs or {}).items():
        if k == "logger" or v is None:
            continue
        parts.append("%s = %r" % (k, v))
    s = chr(10).join(parts)
    return s if len(s) <= limit else s[:limit] + chr(10) + "…（已截断）"


# ============================================================
# 8. 批次闸门：agent.py 唯一调用点
# ============================================================
_GRANTS: Dict[str, Any] = {}
_GRANTS_LOCK = threading.Lock()


def grants_for(session_id: str = "") -> Dict[str, Any]:
    """行为层用：取该会话最近一次批准的授权前缀摘要。

    这份状态由引擎自持 —— agent.py 只管路由，不该替审批记账。
    """
    with _GRANTS_LOCK:
        return dict(_GRANTS.get(session_id or "-", {}) or {})


def last_user_text(conversation: Optional[Sequence[Any]]) -> str:
    """从会话里取最近一条 user 消息的文本（多模态只取文本段）。

    为什么这段在引擎里：卡片要能回显「主人为什么被问」，否则主人只看见一串
    路径，只能靠猜 —— 而靠猜的批准不算审批。取上下文是审批的领域知识，
    因此归引擎；agent.py 只负责把整个 conversation 交过来，不做任何加工。

    兼容两种会话形态：dict 列表与带属性的对象列表。取不到返回空串 ——
    卡片照实显示「未提供」，绝不替主人编一句话。
    """
    for m in reversed(list(conversation or ())):
        is_dict = isinstance(m, dict)
        if (m.get("role") if is_dict else getattr(m, "role", "")) != "user":
            continue
        c = m.get("content") if is_dict else getattr(m, "content", "")
        if isinstance(c, list):          # 多模态：只取文本段，忽略图片等非文本段
            parts = [str(x.get("text", "") or "") for x in c
                     if isinstance(x, dict) and x.get("type", "text") == "text"]
            c = " ".join(p for p in parts if p)
        return str(c or "")
    return ""


def gate_batch(items: Sequence[Tuple[str, str, Dict[str, Any]]],
               session_id: str = "", cwd: str = "",
               user_request: str = "",
               conversation: Optional[Sequence[Any]] = None
               ) -> Tuple[Dict[str, Tuple[bool, str]], Dict[str, Any]]:
    """items: [(tool_call_id, tool_name, kwargs), ...]

    -> (decisions, grants)
       decisions[tc_id] = (是否放行, 不放行时给模型看的文本)
       grants 记录本批已批准的路径前缀，供后续行为校验使用。

    整批原子：只要有一项没拿到批准，本批所有工具都不提交编排器（含本来不需要审批的）。

    上下文：传 `conversation` 即由引擎自己取「主人最近一句话」用于卡片回显 ——
    调用方（agent.py）只负责把整个会话交过来，不替审批准备上下文。
    显式给定 `user_request` 时以它为准（测试与宿主直调用留口）。
    """
    grants: Dict[str, Any] = {"approved_prefixes": [], "session_id": session_id}
    with _GRANTS_LOCK:
        _GRANTS[session_id or "-"] = grants
    if not items:
        return {}, grants
    ids = [it[0] for it in items]
    if mode() == "off":
        return {i: (True, "") for i in ids}, grants
    # 主人最近那句话：由引擎自己从会话里取。取上下文是审批的领域知识，
    # agent.py 只把 conversation 交过来，不做任何加工。
    if not user_request and conversation:
        user_request = last_user_text(conversation)

    vmap = {it[0]: inspect_one(it[1], it[2], session_id, cwd) for it in items}
    for it in items:
        ledger({"event": "inspect", "tc_id": it[0], "tool": it[1],
                "decision": vmap[it[0]]["decision"], "kind": vmap[it[0]]["kind"],
                "targets": vmap[it[0]]["targets"][:8], "session_id": session_id or "-",
                "reason": vmap[it[0]]["reason"][:200]})

    blocked = [i for i in ids if vmap[i]["decision"] == DECIDE_BLOCK]
    if blocked:
        b0 = vmap[blocked[0]]["reason"]
        out = {i: (False, "❌ %s" % (b0 if i in blocked else WHY_BATCH_COLD)) for i in ids}
        ledger({"event": "batch_block", "blocked": blocked, "session_id": session_id or "-"})
        return out, grants

    asks = [i for i in ids if vmap[i]["decision"] == DECIDE_ASK]
    if not asks:
        for i in ids:
            if vmap[i]["decision"] == DECIDE_QUIET:
                grants["approved_prefixes"] += vmap[i]["targets"]
        return {i: (True, "") for i in ids}, grants

    # 作用域预筛：ask 项若所有目标路径都已被授予，则免打扰（但仍计入 grants）
    pending: List[str] = []
    for i in asks:
        t = vmap[i]["targets"]
        via = [SCOPES.match(session_id, x, vmap[i]["kind"]) for x in t[:4]] if t else []
        if t and all(via):
            vmap[i]["scope_via"] = via[0]
            grants["approved_prefixes"] += t
            ledger({"event": "scope_allow", "tc_id": i, "via": via[0],
                    "kind": vmap[i]["kind"], "targets": t[:8]})
        else:
            pending.append(i)
    if not pending:
        return {i: (True, "") for i in ids}, grants

    reqs = []
    for i in pending:
        v = vmap[i]
        # 卡片给人做裁决所需的信息，顺序即重要度：
        # 它到底想干什么 -> 动哪个文件、能不能撤回 -> 你刚才说了什么
        notes = [("AB 自述用途：" + v["purpose"]) if v.get("purpose") else
                 "⚠️ AB 未声明这次操作的目的 —— 建议先问它要干什么再批"]
        notes += list(v["notes"])
        if len(user_request or "") > 3:
            notes.append("你的原话：" + user_request[:120])
        reqs.append({
            "ask_id": i, "session_id": session_id, "kind": v["kind"], "risk": v["risk"],
            "title": v.get("title") or ("对系统盘文件的%s操作" % (
                v["intent"].split("：")[-1] if "：" in v["intent"] else "改动")),
            "intent": v["intent"], "reason": v["reason"], "notes": notes,
            "paths": v["targets"][:12], "critical": v["critical"],
            "timeout": timeout_sec(), "total": len(pending),
            "accepts_note": True,          # 通道据此显示输入框；引擎负责用不用
            "note_hint": "可补充说明（拒绝时尤其有用，会原样送回 AB 与账本）",
            "purpose": v.get("purpose", ""),
            "reversibility": v.get("reversibility", ""),
            "user_request": (user_request or "")[:200],
            "source_code": _source_of(v["args"]),
            "options": [{"key": "A", "label": "允许本次", "scope": SCOPE_ONCE, "emoji": "🟢"},
                        {"key": "B", "label": "本会话允许此路径", "scope": SCOPE_SESSION,
                         "emoji": "🟡"},
                        {"key": "D", "label": "永久允许（需确认）", "scope": SCOPE_PERSISTENT,
                         "emoji": "⚪", "confirm": True},
                        {"key": "E", "label": "拒绝", "scope": SCOPE_ONCE, "emoji": "🔴"},
                        {"key": "F", "label": "拒绝并中止本回合", "scope": SCOPE_ONCE,
                         "emoji": "⛔", "stop": True}],
        })
    ledger({"event": "batch_ask", "ask_id": [r["ask_id"] for r in reqs],
            "count": len(reqs), "session_id": session_id or "-"})

    t0 = time.time()
    try:
        decs = get_port().request_many(reqs)
    except BaseException as _bx:               # KeyboardInterrupt / SystemExit 也算
        # 没有终态的账本无法区分「主人拒了」与「中途断了」—— 今天实测就是这样：
        # 只剩一条 inspect(ask) 挂在那儿。记完再抛，不改变原有的中断语义。
        ledger({"event": "batch_cancelled", "asked": len(pending),
                "ask_id": [r["ask_id"] for r in reqs],
                "why": "%s 等待裁决时回合被中止" % _bx.__class__.__name__,
                "session_id": session_id or "-"})
        raise
    except Exception as e:
        decs = [Decision("E", "error") for _ in reqs]
        ledger({"event": "channel_fatal", "err": "%s: %s" % (e.__class__.__name__, e)})
    while len(decs) < len(reqs):
        decs.append(Decision("E", "skipped"))

    def reason_of(idx: int) -> str:
        d = decs[idx]
        if d.how == "expired":
            base = WHY_NO_REPLY
        elif d.how == "no_channel":
            base = WHY_NO_CHANNEL
        elif d.how == "skipped":
            base = WHY_BATCH_COLD
        elif d.how in ("error", "cancelled"):
            base = "审批通道异常或已取消（%s）" % d.how
        else:
            base = WHY_DENY
        # 主人手打的话优先于模板：他写了字，说明"用户拒绝"那四个字不够表达意图
        return _with_note(base, says_txt or (getattr(d, "note", "") or ""))

    # 主人在这一批里说过的话（按出现顺序去重）。附给每一条未放行项：
    # 只挂在"他针对的那一项"上，模型看到别条回执时就会以为那是无来由的拒绝。
    says: List[str] = []
    for _d in decs[:len(pending)]:
        _n = (getattr(_d, "note", "") or "").strip()
        if _n and _n not in says:
            says.append(_n)
    says_txt = "；".join(says)[:MAX_NOTE]

    approved: Dict[str, Decision] = {}
    for idx, i in enumerate(pending):
        d = decs[idx]
        _vn = {"event": "verdict", "tc_id": i, "ask_id": i, "choice": d.choice,
               "how": d.how, "kind": vmap[i]["kind"], "targets": vmap[i]["targets"][:8],
               "elapsed_sec": round(time.time() - t0, 2), "session_id": session_id or "-"}
        if getattr(d, "note", ""):
            _vn["note"] = d.note.strip()[:MAX_NOTE]     # 主人的话是审计材料，必须落账
        ledger(_vn)
        if d.choice in ("A", "B", "D"):
            approved[i] = d
            if d.choice in ("B", "D"):
                sc = SCOPE_SESSION if d.choice == "B" else SCOPE_PERSISTENT
                made = [SCOPES.add(sc, session_id, t, vmap[i]["kind"], ask_id=i)
                        for t in vmap[i]["targets"][:3]]
                if not any(made):
                    # 批准本身仍然有效（这次照做），只是不许建立免审规则。必须留痕，
                    # 否则主人以为"以后不再问了"，实际下次照样弹 —— 那是骗人。
                    ledger({"event": "scope_denied", "tc_id": i, "scope": sc,
                            "targets": vmap[i]["targets"][:3],
                            "why": "前缀过浅（盘符后不足 %d 段），整棵目录树不可免审"
                                   % MIN_SCOPE_SEGMENTS})
            grants["approved_prefixes"] += vmap[i]["targets"]
        if d.choice == "F":
            break

    batch_ok = len(approved) == len(pending)
    out: Dict[str, Tuple[bool, str]] = {}
    stop = any(d.choice == "F" for d in decs)
    for idx, i in enumerate(pending):
        if batch_ok:
            out[i] = (True, "")
        elif i in approved:
            # 主人批了它，只是同批别人没批 —— 这不是拒绝。
            # 若写成用户拒绝，模型会放弃一条已获授权的路径。
            out[i] = (False, "❌ " + _with_note(WHY_APPROVED_COLD, says_txt))
        else:
            out[i] = (False, "❌ " + reason_of(idx))
    for i in ids:
        if i not in out:
            out[i] = (True, "") if batch_ok else (
                False, "❌ " + _with_note(WHY_BATCH_COLD, says_txt))
    if not batch_ok and stop:
        for i in out:
            out[i] = (False, out[i][1] + " （主人要求中止本回合）")
    ledger({"event": "batch_result", "ok": batch_ok, "asked": len(pending),
            "approved": len(approved), "stop": stop, "session_id": session_id or "-"})
    return out, grants


# ============================================================
# 9. 自检：保护"是否在"必须是可查询的事实
# ============================================================
def self_check(registered_tools: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"mode": mode(), "drive": drive(),
                           "port": get_port().name, "ok": False, "checks": {}}
    d = drive()
    cases = [
        ("写系统盘要问", "execute_shell",
         {"command": "echo x > " + d.rstrip("/") + "/Windows/probe.txt"}, DECIDE_ASK),
        ("删系统盘要问", "execute_python",
         {"code": "import os; os.remove(r" + d.rstrip("/") + "/Windows/probe.txt" + chr(39)}, DECIDE_ASK),
        ("移动要问", "execute_shell",
         {"command": "mv " + d.rstrip("/") + "/Users/x/a.txt " + d.rstrip("/") + "/Users/x/b.txt"}, DECIDE_ASK),
        ("只读不问", "read_file", {"file_path": d.rstrip("/") + "/Windows/win.ini"}, DECIDE_PASS),
        ("临时目录免打扰", "execute_shell",
         {"command": "echo x > " + d.rstrip("/") + "/Users/x/AppData/Local/Temp/t.txt"}, DECIDE_QUIET),
        ("盘外写要问", "execute_shell", {"command": "echo x > D:/work/a.txt"}, DECIDE_ASK),
        ("禁区直接拒", "execute_shell", {"command": "format " + d + " /Q"}, DECIDE_BLOCK),
    ]
    if _LEX is not None:
        _F = list(FORBIDDEN)
        _verb = [x for x in _F if x.endswith(" ")][0]
        _sam = [x for x in _F if x.endswith("sam")][0]
        _say = [x for x in _F if len(x) == 8 and x[0] == "s"][0]
        cases += [
            ("文档正文提禁区词不拦", "execute_python",
             {"code": 'doc = """note: ' + _verb.strip() + " and " + _say
                      + ' are forbidden"""' + chr(10) + "print(len(doc))"}, DECIDE_PASS),
            ("正文提及盘内路径不拦", "execute_python",
             {"code": 'doc = "see ' + d + "/Windows" + _sam
                      + ' in the ticket"' + chr(10) + "print(len(doc))"}, DECIDE_PASS),
            ("只读读取不误判为写入", "execute_shell",
             {"command": "type " + d + "/Windows/win.ini"}, DECIDE_PASS),
            ("解释器注入不许降级成载荷", "execute_shell",
             {"command": "python -c " + chr(39) + "open(r" + chr(34) + d + "/Windows" + _sam + chr(34) + "," + chr(34) + "wb" + chr(34) + ")" + chr(39)}, DECIDE_BLOCK),
        ]
        _hp = [x for x in FORBIDDEN_PATH if x.endswith("hosts")]
        if _hp:
            host = "/" + _hp[0].lstrip(BS).replace(BS, "/")
            cases += [
                ("读关键系统文件不拦", "read_file",
                 {"file_path": d + "/Windows" + host}, DECIDE_PASS),
                ("写关键系统文件直接拒", "execute_shell",
                 {"command": "echo x > " + d + "/Windows" + host}, DECIDE_BLOCK),
            ]
    bad = []
    # 浅前缀护栏（通过时不落盘，只有失效才会）
    guard = SCOPES.add(SCOPE_PERSISTENT, "self_check_probe", d + "/Windows", "probe")
    out["checks"]["浅前缀不得签发免审规则"] = (guard is False, guard)
    if guard is not False:
        bad.append("浅前缀护栏失效：整盘一级目录被存成了永久规则")
    for name, tool, kw, want in cases:
        if want == DECIDE_QUIET and mode() == "strict":
            want = DECIDE_ASK
        try:
            got = inspect_one(tool, kw)["decision"]
        except Exception as e:
            got = "raise:%s" % e.__class__.__name__
        out["checks"][name] = (got == want, got)
        if got != want:
            bad.append("%s(期望%s 实得%s)" % (name, want, got))
    problems = [] if _LEX is not None else [
        "词法层 approval_lex 不可用：退回全文扫，文档里提到危险词就会被误拦"]
    err = [x for x in out["checks"].values() if "异常" in str(x[1])]
    if err:
        problems.append("引擎判定内部异常（不是判定不符，是代码出错）：%d 处" % len(err))
    st = specs_state()
    out["specs"] = st
    out["ledger_writable"] = not _LEDGER_ERR["why"]
    if _LEDGER_ERR["why"]:
        out["ledger_error"] = _LEDGER_ERR["why"]
    out["lex_layer"] = _LEX is not None
    out["no_spec"] = not st["loaded"]
    out["spec_failed"] = bool(st["failed"])
    out["strategy_live"] = not bad
    out["problems"] = problems + bad + (
        ["审批规范一个都没加载成功"] if out["no_spec"] else [])
    out["ok"] = bool(out["strategy_live"] and out["ledger_writable"] and problems == []
                     and not out["no_spec"] and not out["spec_failed"])
    ledger({"event": "self_check", "ok": out["ok"], "mode": out["mode"],
            "drive": out["drive"], "port": out["port"], "problems": out["problems"]})
    return out
