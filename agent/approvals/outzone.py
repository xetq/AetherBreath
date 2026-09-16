# -*- coding: utf-8 -*-
"""规范：改动「项目范围之外」的文件需要人工确认。

补的缺口（2026-09-11 安全审计）：审批原先只有两份覆盖面 ——
  · `cdrive.files`：**系统盘（C:）**的写/删/移
  · `engine.selfmodify`：**项目自留地**
两者之间的**补集没有任何规范**。项目放在 D 盘时，写 D 盘任意位置全部静默放行。
实测全 pass：

    <other-app>/profiles/<name>/skills/…    ← 另一个 agent 的技能库
    <other-app>/profiles/<name>/SOUL.md     ← 那个 agent 的身份文件
    <other-app>/profiles/<name>/memories/…  ← 那个 agent 的长期记忆
    <other-app>/config.yaml                 ← 那个应用的配置

「被注入的 agent 去改宿主 agent 的技能/记忆」是一条跨进程持久化链：
宿主下次启动时，被写进去的内容就进了宿主的系统提示。

取向：
  · **读不问**（与 cdrive.files 一致：读在真实任务里是常态）；
  · **项目内不问** —— 那是它自己的活儿（work 区）；
  · 项目外的临时/缓存目录免打扰（记账放行）；
  · 路径形似「另一个 agent 的 profile」时标 critical —— 那种地方本不该被碰。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import _impact as I

KIND = "outzone.write"
TITLE = "在项目范围外改动文件"
RISK = 2

# 免打扰：项目外的临时/缓存位置
QUIET: tuple = (
    "/appdata/local/temp/", "/temp/", "/tmp/", "/pip/cache/", "/uv/",
    "/npm-cache/", "/node_modules/", "/__pycache__/", "/crashdumps/",
    "/.cache/",
)

# 「另一个 agent 的 profile」的布局特征：命中则卡片措辞更重
_PROFILE_HINTS = ("/profiles/", "/profile/")
_PROFILE_PARTS = ("/skills/", "/memories/", "/memory/", "soul.md", "agents.md",
                  "config.yaml", "config.yml", "user.md")


def _is_profile_like(path: str) -> bool:
    low = path.lower()
    return any(h in low for h in _PROFILE_HINTS) and \
        any(p in low for p in _PROFILE_PARTS)


def applies(ctx: Dict[str, Any]) -> bool:
    """廉价初筛：有动作点才可能构成「改动」。区判定留在 finding()。"""
    return bool(ctx.get("actions"))


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    root = (ctx.get("root") or "").lower().rstrip("/")
    droot = (ctx.get("drive") or "").lower()
    if not root:
        return None

    hits: List[Dict[str, Any]] = []
    for a in (ctx.get("actions") or []):
        if a.get("opaque"):
            continue
        word = str(a.get("word") or "")
        cat = I.category_of(word)
        if not cat and (a.get("redirect") or a.get("mode")):
            cat = "写入/覆盖"
        if not cat or not word:
            continue                 # 认不出类别、又没有写信号 = 不算改动（既有取向）
        paths, how = I.targets_of(a)
        for p in paths:
            low = p.lower()
            if low.startswith(droot) or low == root or low.startswith(root + "/"):
                continue             # 系统盘 / 项目内：各有专门规范，不在这里重复问
            hits.append({"verb": word, "cat": cat, "path": p,
                         "how": how or "全部实参"})
    if not hits:
        return None

    targets: List[str] = []
    for h in hits:
        if h["path"] not in targets:
            targets.append(h["path"])
    cats = "/".join(sorted({h["cat"] for h in hits}))

    if all(any(q in t.lower() + "/" for q in QUIET) for t in targets):
        return {"quiet": True, "action": cats, "targets": targets,
                "reason": "项目外临时/缓存目录内的%s，记账放行" % cats}

    crit = [t for t in targets if _is_profile_like(t)]
    notes = ["依据：%s" % "；".join(
        "%s 的目标取自%s" % (I.verb_last(h["verb"]), h["how"]) for h in hits[:3])]
    if crit:
        notes.insert(0, "⚠️ 该路径形似另一个 agent 的 profile（技能/记忆/身份/配置）——"
                        "改动会影响那个 agent 的后续行为，且不易察觉")
    return {
        "quiet": False,
        "action": cats,
        "targets": targets,
        "intent": "%s项目范围外的文件：%s" % (
            cats, "、".join((t.rsplit("/", 1)[-1] or t) for t in targets[:3])),
        "reason": "目标既不在项目根内，也不在系统盘 —— 跨出工作区的改动原先没有覆盖面",
        "notes": notes,
        "critical": bool(crit),
    }
