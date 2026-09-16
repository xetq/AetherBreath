# -*- coding: utf-8 -*-
"""URL 安全校验：疑似凭据 / 内网地址（SSRF）。

**规则只有一处。** `web_extract` 与 `fetch_url` 都 import 本模块。

为什么抽出来：本项目已经出过这个事故 —— 同一类工具里 `web_extract` 有
「凭据 + SSRF」两道闸，`fetch_url` 一道都没有，而它的 schema 还劝模型
「有更优秀的 web_extract, 优先用它」。**选择哪个工具不该决定安全性**：
模型换个工具名，闸门就整体消失。

取向（对齐 Hermes `tools/url_safety.py` 的思路，简版）：
  · 只挡**显式**内网地址，不做 DNS 解析 —— 解析到内网的域名挡不住
    （要更严需解析后再判，见 approvals/README.md 已知限制）；
  · 命中即拒绝，不给「仍要访问」的授权入口 —— 换个写法就能绕过的东西，
    留个按钮只会在卡片上制造噪音；
  · 拦截措辞由本模块统一给出，两个工具回报同一句话，便于测试断言。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

# 疑似密钥模式（对齐 Hermes tools/url_safety.py + agent/redact.py 的思路）：
#   A. 前缀式凭据，全串搜索（sk- / ghp_ / github_pat_ / xox / AKIA / rk-live-）
#   B. 敏感 query 参数名（对齐 Hermes _SENSITIVE_QUERY_PARAM_NAMES 的子集）
_PREFIX_SECRET_RE = re.compile(
    r"(?i)(?:sk-[a-z0-9]{12,}|ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|"
    r"xox[baprs]-[a-z0-9-]{10,}|AKIA[0-9a-z]{16}|rk-live-[a-z0-9]{16,})"
)
_SENSITIVE_PARAM_NAMES = frozenset({
    "access_token", "api_key", "apikey", "auth_token", "authorization",
    "awsaccesskeyid", "client_secret", "credential", "credentials", "jwt",
    "password", "passwd", "secret", "session_id", "signature", "token",
    "x_amz_security_token", "x_amz_signature", "x_api_key", "x_auth_token",
})
# 显式内网/本机地址（简版 SSRF 拦截）
_PRIVATE_HOST_RE = re.compile(
    r"(?i)^(localhost|0\.0\.0\.0|127(\.\d{1,3}){3}|10(\.\d{1,3}){3}|"
    r"192\.168(\.\d{1,3}){2}|172\.(1[6-9]|2\d|3[01])(\.\d{1,3}){2}|"
    r"169\.254(\.\d{1,3}){2}|\[::1\]|\[::\]|\[fc|\[fd)"
)


def url_has_secret(url: str) -> bool:
    """URL 里是否疑似带着凭据（明文或百分号编码）。"""
    decoded = urllib.parse.unquote(url)
    if _PREFIX_SECRET_RE.search(url) or _PREFIX_SECRET_RE.search(decoded):
        return True
    try:
        parsed = urllib.parse.urlsplit(decoded)
    except ValueError:
        return False
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if value and urllib.parse.unquote(key).lower() in _SENSITIVE_PARAM_NAMES:
            return True
    return False


def url_is_private(url: str) -> bool:
    """URL 是否显式指向本机/内网地址。解析不出主机名时按「是」处理（保守）。"""
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return True
    return bool(_PRIVATE_HOST_RE.match(host.strip("[]")))


def check_url(url: str) -> Optional[str]:
    """返回拦截原因（给模型看的短语）；可放行时返回 None。

    只回答「这个 URL 能不能发出去」，不关心能不能取回内容 ——
    scheme 校验、可用性错误仍由各自工具负责。
    """
    if url_has_secret(url):
        return "URL 疑似包含 API key/令牌, 禁止发送"
    if url_is_private(url):
        return "URL 指向内网/本机地址(SSRF 防护)"
    return None
