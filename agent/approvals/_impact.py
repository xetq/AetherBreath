# -*- coding: utf-8 -*-
"""审批规范共享层：把「动作点」翻译成「要动哪些文件」。

下划线前缀 = 不是规范，loader 会跳过它（见 approvals/__init__.py）。

这里定的是一条此前缺失的纪律：

    目标是**某个动词的直接实参**，不是「这段文本里出现过的路径」。

旧实现是共现式判定（全段扫到动词 + 全段扫到路径 → 成立），代价已被实测钉死：
  · `cmd.lower().replace('a','b')` 被认定「移动文件」；
  · 脚本里一行 `SD = '盘符'` 常量被弹成「对该盘执行写入」，
    而那次任务在改分析脚本，与系统盘毫无关系。

三条方向规则（写在这里，是为了让「读一个文件」不再冒充「写一个文件」）：
  DEST_LAST   动词的**最后一个**位置参数是目的地，前面的是源
              （cp/mv/install/copy*/rename…）→ 只有目的地算被改动
  ALL_TARGET  所有位置参数都是被改动对象（rm/mkdir/tee/truncate…）
  FIRST_ONLY  只有第一个实参是对象（open/imwrite/savetxt/to_*…）
未登记的动词走 ALL_TARGET（保守），但**仍要求实参本身是路径**。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

LF = chr(10)
BS = chr(92)

# ---- 方向表：动词末段 -> 目标取法 ----
DEST_LAST = frozenset({
    "cp", "copy", "copyfile", "copy2", "copyfileex", "copytree", "copy-item",
    "mv", "move", "move-item", "movefilew", "movefilea", "movefileexw",
    "movefileexa", "movefilewithprogressw", "movedirectoryw", "rename",
    "rename-item", "renameservice", "ren", "replace", "replacefilew",
    "replacefilea", "rsync", "install", "scp", "installutil",
    "writeremoveprofilestringw",
})
FIRST_ONLY = frozenset({
    "open", "touch", "mkdir", "makedir", "makedirs", "mkdirs", "new-item",
    "truncate", "imwrite", "savetxt", "savefig", "to_csv", "to_excel",
    "to_json", "to_parquet", "to_pickle", "to_feather", "to_orc", "to_stata",
    "to_sql", "to_xml", "to_xlsx", "to_yaml", "to_npz", "to_npy", "to_wav",
    "writefile", "write_text", "write_bytes", "writeprivateprofilestringw",
    "writeprivateprofilestringa", "dump", "dumps", "save", "savez", "savepd",
})
ALL_TARGET = frozenset({
    "rm", "del", "erase", "unlink", "remove", "rmtree", "rd", "rmdir",
    "delete", "deletefilew", "deletefilea", "deletefile", "removefilew",
    "removefilea", "removedirectoryw", "removedirectorya", "shfileoperationw",
    "shfileoperationa", "ifileoperation", "remove-item", "removeitem",
    "empty-recyclebin", "clear-recyclebin", "rm-rf", "tee", "out-file",
    "set-content", "add-content", "send2trash",
})
# 明确「只读」的动词：即使带路径实参也不申请审批（读系统文件是排障常态）
READ_ONLY = frozenset({
    "read", "readline", "readlines", "open", "exists", "isfile", "isdir",
    "getsize", "getmtime", "stat", "listdir", "scandir", "walk", "cat", "head",
    "tail", "less", "more", "type", "file", "du", "df", "ls", "dir", "find",
    "grep", "rg", "fd", "wc", "sort", "uniq", "awk", "sed", "jq", "md5sum",
    "sha1sum", "sha256sum", "where", "which", "Get-Content", "get-content",
    "select-string", "test-path", "resolve-path", "get-item", "get-childitem",
    "show", "status", "diff", "log", "ls-files", "read_text", "read_bytes",
    "json", "loads", "load", "reader", "scan", "probe", "peek", "headtail",
})
# 只读动词里唯一要单独看的：open 靠 mode 决定（见 _open_is_write）
MODE_WRITE_CHARS = "wax+"


# 只在 shell 里成立、在 python 里几乎必然是别的东西的命令名。
# 实测事故：探针脚本里有个读文件的小函数 def rd(p)，调用 rd('…/bridge.py') 被判成
# 「删除引擎文件」—— 因为 rd 是 Windows 的删目录命令。同理 rm/cp/mv/del/ren。
# python 侧真要删文件必须走 os./shutil./pathlib. 或内置 open，所以这些裸名在 py 动作点上不算数。
SHELL_ONLY = frozenset({"rm", "del", "rd", "rmdir", "mv", "cp", "ren", "erase",
                        "tee", "type", "cat", "move-item", "copy-item", "remove-item",
                        "new-item", "set-content", "add-content", "out-file", "touch"})


def verb_last(word: str) -> str:
    """动词末段：os.path.remove -> remove；/bin/rm -> rm；shutil.copy2 -> copy2"""
    w = (word or "").lower().strip()
    if not w:
        return ""
    w = w.replace(BS, "/").rsplit("/", 1)[-1]
    # 截到第一个非法字符为止：代码残缺时词法层可能交出「remove(r'x」这种脏 token，
    # 原样进卡片就成了「依据：remove(r 的目标取自全部实参」—— 让人读不懂的措辞
    # 等于没给信息。标识符之外的一律断开。
    cut = 0
    for i, ch in enumerate(w):
        if ch.isalnum() or ch in "._-":
            cut = i + 1
        else:
            break
    w = w[:cut]
    return w.rsplit(".", 1)[-1]


def _like_path(s: str) -> bool:
    """这个字符串像不像一个路径。用来把 'a'、'w'、'%s' 这类实参挡在目标之外。"""
    t = (s or "").strip().strip('"').strip("'")
    if len(t) < 2 or len(t) > 500:
        return False
    low = t.lower()
    if low.startswith(("http://", "https://", "ftp://", "mailto:", "data:")):
        return False
    if low in ("/dev/null", "nul", "dev/null"):
        return False
    if any(sep in t for sep in ("/", BS, ":")):
        return True
    if LF in t or " " in t.strip():
        return False
    # 无分隔符的裸名字（def / x / main）要落盘查证才算路径，而 exists 是一次 stat
    # 系统调用 —— 实测它占整条判定链 68% 的耗时，且本函数在每次工具调用上必经。
    # 收紧成「含点号才查磁盘」：真文件名几乎都带扩展名，def/x/tmp 这类裸词直接放过。
    if "." not in t or len(t) > 60:
        return False
    try:
        import os
        return os.path.exists(t)
    except Exception:
        return False


def resolve(text: str, cwd: str = "") -> str:
    """实参文本 -> 归一化的绝对路径（小写盘符、正斜杠）。不是路径就返回空串。"""
    if not _like_path(text):
        return ""
    t = (text or "").strip().strip('"').strip("'")
    if not t:
        return ""
    try:
        import approval
        return approval.unify(t, cwd or approval._root())
    except Exception:
        return ""


def targets_of(act: Dict[str, Any]) -> Tuple[List[str], str]:
    """一个动作点 -> (被改动的绝对路径, 依据说明)。

    取向是不对称的：**漏判比误判严重**，但「引用文件」绝不算「改动文件」。
    以下五条规则每一条都对应一次实测翻车（用例见 tests/test_approval_binding.py）：

      · 只读动词若带 > / >> 重定向，它就是写（cat a > b 的 b 会被覆盖），
        但被改动的是**目标**，源只是被读；
      · 移动类连**源**一起算：mv 把文件搬走 = 源位置的文件没了；
      · 写入类取目的地，且必须是「最后一个像路径的实参」—— Win32 的
        MoveFileExW(源, 目的地, 标志位) 若按「最后一个实参」取会取到 1，整次移动无人拦；
      · `2>` 是 stderr，不是写（词法层已不算 redirect，这里再兜一道）；
      · 认不出类别又没有写信号的动词一律放过 —— 历史重放证明「未知动词保守取
        全部实参」会凭空造出上百条弹窗。
    """
    word = verb_last(str(act.get("word") or ""))
    cwd = str(act.get("cwd") or "")
    mode = str(act.get("mode") or "")
    raw_redir = [str(x) for x in (act.get("redirect") or [])]
    raw_args = [str(x) for x in (act.get("args") or [])]
    writing = bool(raw_redir) or any(c in mode for c in MODE_WRITE_CHARS)
    # 取向判定全部前置且零成本。解析路径要发 os.path.exists（Windows 上是实打实的
    # 系统调用），而本函数在**每一次工具调用**的必经路径上：先问「这个动词关不关
    # 文件的事」，不关就不去解析实参。实测这个顺序把单次判定从 1.4ms 压回 0.1ms 级。
    if word in READ_ONLY and not writing:
        return [], ""
    if str(act.get("lang") or "") == "py" and word in SHELL_ONLY:
        return [], ""                       # shell 命令名撞上了 python 里的同名局部函数
    # open 同时在 READ_ONLY 表里（读它是常态），它的写/读由 mode 决定；
    # 下面那条「只读动词带重定向」必须豁免 open，否则 open(p,'w') 会被吞成无目标。
    if word == "open" and not any(c in mode for c in MODE_WRITE_CHARS):
        return [], ""                       # 读模式：open(路径) 不是改动
    redir = [x for x in (resolve(v, cwd) for v in raw_redir) if x]
    paths = [x for x in (resolve(v, cwd) for v in raw_args) if x]
    if word in READ_ONLY and writing and word != "open":
        return (list(redir), "重定向目标") if redir else ([], "")
    cat = category_of(word)
    if not cat and not writing:
        return [], ""
    if not paths:
        # pathlib 那一族把路径挂在接收者上：Path(x).unlink() / Path(x).write_text(y)
        rp = [x for x in (resolve(str(v), cwd) for v in (act.get("recv_paths") or [])) if x]
        if rp:
            paths, how = rp, "接收者里的路径"
    pool, how = paths, "全部实参"
    if cat == "写入/覆盖":
        if word in DEST_LAST:
            pool, how = paths[-1:], "目的地（最后一个路径实参）"
        elif word in FIRST_ONLY:
            pool, how = paths[:1], "第一个实参"
    chosen = pool + redir
    if cat == "写入/覆盖":
        # 往 glob 模式里写东西不成立（> *.log 是语法错误），这类只可能是被搜索/
        # 被列举的对象。删除与移动仍照 glob 保守处理（rm *.py 是真会没的）。
        chosen = [p for p in chosen if "*" not in p and "?" not in p]
    seen, uniq = set(), []
    for q in chosen:
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    if uniq and redir and not pool:
        how = "重定向目标"
    return uniq, how


def all_touched_paths(act: Dict[str, Any]) -> List[str]:
    """动作点里出现过的全部路径（含源），仅供卡片措辞参考，不作审批目标。"""
    got = []
    for x in list(act.get("args") or []) + list(act.get("redirect") or []):
        p = resolve(str(x), str(act.get("cwd") or ""))
        if p:
            got.append(p)
    return got


def self_hit(p: str, root: str) -> str:
    """路径是否落在审批链/引擎自留地内。命中返回那条前缀，否则空串。

    判据全部相对项目根，不写绝对路径 —— clone 到任何机器上都成立。
    """
    rl = (root or "").lower().rstrip("/") + "/"
    pl = (p or "").lower()
    if not rl or not pl.startswith(rl):
        return ""
    rel = pl[len(rl):]
    for pre in SELF_PREFIXES:
        pe = pre.lower().strip("/")
        # 前缀匹配要放宽到 startswith(pe)：SELF_PREFIXES 里有意写了
        # "agent_logs/approval" 这种半截名，用来盖住 approval-YYYYMM.jsonl 与
        # approval_rules.jsonl 两个文件；只按目录边界匹配就漏掉了它们。
        if rel == pe or rel.startswith(pe) or rel.startswith(pe + "/"):
            return pre
    return ""


def zone(p: str, ctx: Dict[str, Any]) -> str:
    """路径落在哪个后果区：drive 系统盘 / self 引擎自留地 / work 工作区 / other 其它。"""
    d = (ctx.get("drive") or "").lower()
    root = (ctx.get("root") or "").lower()
    low = (p or "").lower()
    if low.startswith(d):
        return "drive"
    if root and low.startswith(root):
        return "self" if self_hit(low, root) else "work"
    return "other"


# 审批链自身与引擎关键文件（相对项目根，零绝对路径 —— clone 即用）
SELF_PREFIXES: Tuple[str, ...] = (
    "agent/approval.py", "agent/approval_lex.py", "agent/approvals",
    "agent_logs/approval", "agent/agent.py", "agent/task_orchestrator.py",
    "agent_webui/backend/approval_adapter.py", "agent_webui/backend/bridge.py",
    "agent_webui/backend/api.py", "agent_webui/backend/main.py",
    ".env", ".git", "tests/test_approval",
    # ---- 2026-09-11 安全审计补齐 ----
    # 这些文件同样决定「agent 的行为约束」与「它看得见什么」，改它们等于改闸门
    # 本身，只是不经 approval.py 这一条路（审计实测原先全部静默放行）：
    "agent/skill_system.py",                  # 技能扫描/注入：决定什么进系统提示
    "agent/logger.py",                        # 留痕模块：改它等于改审计记录
    "agent_tools",                            # 工具实现（含 create_tool 生成的新工具）
    "agent_skills",                           # 技能库正文（第三方下载物也落这里）
    "agent_memory/long_memory/SOUL.md",       # 身份与红线
    "agent_memory/long_memory/AGENTS.md",     # 行为规则
    "agent_memory/long_memory/USER.md",       # 主人画像
)


# ---- 动词末段 -> 人看的动作类别（删除 > 移动 > 写入，破坏性大的优先） ----
CATEGORY: Dict[str, Tuple[str, ...]] = {
    "删除": ("删除", "delete", "remove", "unlink", "rmtree", "erase", "send2trash",
             "empty-recyclebin", "clear-recyclebin"),
    "移动/重命名": ("mv", "move", "rename", "renameservice", "replace",
                 "move-item", "rename-item", "ren"),
    "写入/覆盖": ("cp", "copy", "install", "touch", "mkdir", "makedirs", "makedirs",
               "mkdirs", "tee", "out-file", "set-content", "add-content", "new-item",
               "truncate", "write", "write_text", "write_bytes", "writefile",
               "dump", "save", "savez", "savetxt", "savefig", "imwrite", "rsync"),
}
# 带 W/A 后缀的 Win32 原生 API：另一套命名，逐个点出（历史上就是这样漏判的）
CATEGORY_WIN = {
    "删除": ("deletefile", "deletefilew", "deletefilea", "removefilew", "removefilea",
            "removedirectoryw", "removedirectorya", "shfileoperationw",
            "shfileoperationa", "ifileoperation", "rd", "rmdir", "rm", "del"),
    "移动/重命名": ("movefilew", "movefilea", "movefileexw", "movefileexa",
               "movefilewithprogressw", "movedirectoryw", "replacefilew",
               "replacefilea", "movefile"),
    "写入/覆盖": ("copyfilew", "copyfilea", "copyfileexw", "copy", "copy2", "copyfile",
               "copytree", "writeremoveprofilestringw", "writeprivateprofilestringw",
               "writeprivateprofilestringa", "open"),
}


def category_of(word: str) -> str:
    """动词末段 -> 类别。认不准返回空串（认不准就不许编措辞给人看）。"""
    w = verb_last(word)
    if not w:
        return ""
    for cat, keys in CATEGORY.items():
        if w in keys or w.replace("-", "_") in keys:
            return cat
    for cat, keys in CATEGORY_WIN.items():
        if w in keys:
            return cat
    if w.startswith("to_") or w.startswith("write"):
        return "写入/覆盖"
    if w.startswith("rm") or w.startswith("delete") or w.startswith("remove"):
        return "删除"
    if w.startswith("move") or w.startswith("copy"):
        return "移动/重命名"
    return ""
