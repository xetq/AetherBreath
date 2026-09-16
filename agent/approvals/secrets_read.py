# -*- coding: utf-8 -*-
"""规范：读取本机凭据文件需要人工确认。

补的缺口（2026-09-11 安全审计）：审批对「读」整体不问。`cdrive.files` 把取向
写得很清楚 ——「读操作仍然不问：venv、site-packages 全在系统盘，一次 import
上百次读，读也弹会把人训练成闭眼点允许」。那条取向对**普通读**是对的，
但它把「读凭据」一并放过：实测 `read_file` 读 `C:/Users/<user>/.ssh/id_rsa`
判 pass，读任意路径也 pass。

而这个 agent **有出网能力**：读到的内容一旦进了上下文，就可能随下一次请求
离开本机（audit 实测：读 pass + 出网 pass，整条链原先零审批）。
所以凭据读取要单独成一道闸：**读它们极少是任务必需，误读一次却不可撤回。**

取向：
  · 只管**读**；写/删/移交给 cdrive.files 与 outzone.write（同一件事不弹两张卡）；
  · 只认**指纹明确的路径**，不做「secret」泛词匹配 —— 泛匹配的误报会把闸门变噪音；
  · 项目自己的 `.env` 也在列：那正是注入最想拿的东西。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import _impact as I

KIND = "secrets.read"
TITLE = "读取本机凭据文件"
RISK = 1

# 工具层「读文件」工具：其参数里的路径即读取目标
_READ_TOOL = "read_file"

# 凭据指纹（小写匹配）。只收「看一眼就知道是凭据」的形态，不收泛词。
_FINGERPRINTS: tuple = (
    # SSH / 密钥材料
    "/.ssh/", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".pem", ".ppk", ".pfx", ".p12", ".jks", ".keystore", ".key",
    # 云 / 容器 / 包管理器凭据
    "/.aws/credentials", "/.aws/config", "/.kube/config", "/.docker/config.json",
    ".netrc", "_netrc", ".git-credentials", "/.npmrc", "/.pgpass", ".my.cnf",
    # 浏览器数据
    "login data", "/cookies", "web data", "logins.json", "key4.db",
    "signons.sqlite", "/google/chrome/user data/", "/mozilla/firefox/profiles/",
    # 系统凭据库
    "/microsoft/credentials/", "/microsoft/vault/",
    # 项目/环境密钥文件
    ".env", ".env.local", ".env.production", ".env.development",
    "credentials.json", "credentials.yaml", "credentials.yml",
    "secrets.json", "secrets.yaml", "secrets.yml", "secret.json",
    # 通用私钥命名
    "service-account", "private_key", "privatekey",
)


# 宿主 agent profile 的记忆/身份文件：不是「凭据」，但同属不可外发的隐私面
_PROFILE_HINTS = ("/profiles/",)
_PROFILE_PARTS = ("/memories/", "/memory/", "soul.md", "user.md", "agents.md",
                  "memory.md", "config.yaml", "config.yml")


def _which(path: str) -> str:
    low = path.lower()
    for f in _FINGERPRINTS:
        if f in low:
            return f
    if any(h in low for h in _PROFILE_HINTS) and any(p in low for p in _PROFILE_PARTS):
        return "另一个 agent 的 profile 记忆/身份文件"
    return ""


_TOKEN_RE = re.compile(r"[^\s\"'`|;&()<>]+")


def _tokens_of(ctx: Dict[str, Any]) -> List[str]:
    """指令层与动作点实参里的 token —— 用来捞相对路径形态的凭据名。

    为什么需要：`.env`、`id_rsa` 这类相对路径不会被路径正则收进操作数层
    （实测 `cat .env` 的 operands 是空的），但词法层已经把它们绑在动作点上
    （`{"word": "cat", "args": [".env"]}`）—— 那是可用的判据。
    """
    texts = [str(ctx.get("control") or "")]
    for a in (ctx.get("actions") or []):
        texts += [str(x) for x in (a.get("args") or [])]
    out: List[str] = []
    for t in texts:
        for tok in _TOKEN_RE.findall(t):
            if _which(tok) and tok not in out:
                out.append(tok)
    return out


def _has_write_action(ctx: Dict[str, Any]) -> bool:
    """本次调用里是否存在写/删/移动作点（有则交给写类规范，避免重复弹卡）。"""
    for a in (ctx.get("actions") or []):
        if I.category_of(str(a.get("word") or "")):
            return True
        if a.get("redirect") or a.get("mode"):
            return True
    return False


def applies(ctx: Dict[str, Any]) -> bool:
    tool = str(ctx.get("tool") or "")
    if tool == _READ_TOOL or ctx.get("operands"):
        return True
    # 相对路径形态的凭据名（`.env`）不会进操作数层，但会留在指令层 ——
    # 初筛漏掉它们，等于「加个 ./ 就绕过凭据闸」（实测 `cat .env` 曾整条 pass）。
    return bool(_tokens_of(ctx))


def finding(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool = str(ctx.get("tool") or "")
    if _has_write_action(ctx):
        return None
    cands: List[str] = [str(p) for p in (ctx.get("operands") or [])]
    if tool == _READ_TOOL and not cands:
        cands = [str(p) for p in (ctx.get("paths") or [])]
    cands += _tokens_of(ctx)
    if not cands:
        return None

    hits: List[str] = []
    which = ""
    for p in cands:
        w = _which(p)
        if w and p not in hits:
            hits.append(p)
            which = which or w
    if not hits:
        return None

    names = "、".join((p.rsplit("/", 1)[-1] or p) for p in hits[:3])
    return {
        "quiet": False,
        "action": "读取",
        "targets": hits,
        "intent": "读取本机凭据文件：%s" % names,
        "reason": "目标路径命中凭据指纹「%s」" % which,
        "notes": ["凭据一旦进入对话上下文，就可能随下一次请求离开本机（本 agent 可出网）",
                  "确需查看时建议只读必要的字段，而不是整份文件"],
        "critical": False,
    }
