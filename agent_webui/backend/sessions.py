# -*- coding: utf-8 -*-
"""会话读取层（只读扫描 agent_memory/working_memory/*.json）。

格式由 agent.save_session 决定：
  {session_id, created_at(=最后保存时间), message_count, status, messages, system_prompt?}
删除走"移动到备份"而非物理删除（trash 优先原则）。
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

STATUS_LABEL = {"active": "进行中", "interrupted": "中断未完成", "complete": "正常结束"}


def validate_session_id(sid: str) -> str:
    sid = (sid or "").strip()
    if not sid or ".." in sid or not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,79}$", sid):
        raise ValueError(f"非法 session_id: {sid!r}（仅允许字母数字下划线点横线，≤80 字符）")
    return sid


def session_file(sid: str) -> Path:
    return config.WORKING_MEMORY_DIR / f"{validate_session_id(sid)}.json"


def _load_raw(sid: str) -> Optional[Dict[str, Any]]:
    p = session_file(sid)
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _brief(text: Any, n: int = 90) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:n] + ("…" if len(s) > n else "")


VIEW_DIR = config.PROJECT_ROOT / "agent_memory" / ".condensed_sessions"


def _ctx_meta(sid: str, msgs: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """上下文视图元数据（agent/context_manager.py 落盘，只读）。

    视图 = 原文的确定性函数，缺文件即未压缩过；压缩次数从事件流实算（不缓存、不猜）。
    msgs 用于给出「原文占用估算」，让侧栏圆圈在没压缩过的会话上也有数。
    """
    out: Dict[str, Any] = {}
    try:
        vf = VIEW_DIR / f"{sid}.json"
        if vf.exists():
            v = json.loads(vf.read_text(encoding="utf-8"))
            st = v.get("stats") or {}
            tk = v.get("tokens") or {}
            out = {"version": v.get("version"), "source_len": v.get("source_len"),
                   "rounds": st.get("rounds"), "saved_ratio": st.get("saved_ratio"),
                   "est_before": tk.get("est_before"), "est_after": tk.get("est_after"),
                   "kept_rounds": v.get("kept_rounds"),
                   "last_compact_at": v.get("created_at")}
        ev = VIEW_DIR / f"{sid}.events.jsonl"
        if ev.exists():
            n = 0
            with open(ev, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        if json.loads(line).get("event") == "compact":
                            n += 1
                    except Exception:
                        continue
            out["compactions"] = n
        if msgs is not None:
            # 口径与 agent/context_manager.est_tokens 一致（cl100k 对中文高估 ≈1.8 倍，
            # 故按 1 字符 ≈ 0.5 token 折算）；只是给圆圈的兜底量，真实值以 usage 为准
            out["est_original"] = len(json.dumps(msgs, ensure_ascii=False)) // 2
    except Exception:
        return out
    return out


def list_sessions() -> Dict[str, Any]:
    d = config.WORKING_MEMORY_DIR
    items: List[Dict[str, Any]] = []
    if not d.exists():
        return {"sessions": [], "dir": str(d), "total": 0}
    for p in sorted(d.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            items.append({"session_id": p.stem, "unreadable": True,
                          "message_count": 0, "status": "unknown",
                          "summary": "（文件无法解析）"})
            continue
        msgs = [m for m in (data.get("messages") or []) if m.get("role") != "system"]
        first_user = next((m for m in msgs if m.get("role") == "user"), None)
        tool_calls = sum(len(m.get("tool_calls") or []) for m in msgs
                         if m.get("role") == "assistant")
        st = p.stat()
        items.append({
            "session_id": data.get("session_id") or p.stem,
            "file": p.name,
            "message_count": data.get("message_count", len(msgs)),
            "user_turns": sum(1 for m in msgs if m.get("role") == "user"),
            "tool_calls": tool_calls,
            "status": data.get("status", "complete"),
            "status_label": STATUS_LABEL.get(data.get("status", "complete"), "未知"),
            "has_snapshot": bool(data.get("system_prompt")),
            "summary": _brief(first_user.get("content")) if first_user else "（空会话）",
            "last_activity": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "saved_at": data.get("created_at"),
            "size": st.st_size,
            "ctx": _ctx_meta(p.stem, msgs),   # 视图元数据 + 原文占用估算（未压缩过也有数）
        })
    return {"sessions": items, "total": len(items), "dir": str(d)}


def _args_brief(raw: Any, n: int = 260) -> str:
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
        parts = []
        for k, v in (obj or {}).items():
            sval = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            sval = re.sub(r"\s+", " ", str(sval)).strip()
            if len(sval) > 90:
                sval = sval[:90] + "…"
            parts.append(f"{k}={sval}")
        text = "  ".join(parts)
    except Exception:
        text = re.sub(r"\s+", " ", str(raw))[:n]
    return text[:n] + ("…" if len(text) > n else "")


def get_history(sid: str, limit: int = 400) -> Dict[str, Any]:
    """还原会话消息：user/assistant 正文 + 工具调用与返回配对。"""
    data = _load_raw(sid)
    if data is None:
        return {"session_id": sid, "exists": False, "messages": [], "turns": []}
    raw_msgs = [m for m in (data.get("messages") or []) if m.get("role") != "system"]

    tool_index: Dict[str, Dict[str, Any]] = {}
    for m in raw_msgs:
        if m.get("role") == "tool":
            tool_index[str(m.get("tool_call_id"))] = m

    out: List[Dict[str, Any]] = []
    for m in raw_msgs:
        role = m.get("role")
        if role == "tool":
            continue
        item: Dict[str, Any] = {"role": role, "content": m.get("content") or ""}
        calls = m.get("tool_calls") or []
        if calls:
            tcs = []
            for tc in calls:
                fn = (tc or {}).get("function") or {}
                cid = str(tc.get("id") or "")
                ret = tool_index.get(cid)
                ret_text = (ret or {}).get("content")
                tcs.append({
                    "id": cid,
                    "name": fn.get("name") or "?",
                    "args": _args_brief(fn.get("arguments")),
                    "result": _brief(ret_text, 320) if ret_text is not None else None,
                    "failed": bool(ret_text and str(ret_text).startswith("❌")),
                    "pending": ret_text is None,
                })
            item["tool_calls"] = tcs
            # 时间线兼容字段（前端复用同一渲染器）
            item["timeline"] = [{
                "tool": t["name"], "args": t["args"], "result": t["result"],
                "ok": not t["failed"] and not t["pending"], "error": None,
                "elapsed": None, "call_id": t["id"], "source": "history",
            } for t in tcs]
        if role == "assistant" and not (m.get("content") or "").strip() and not calls:
            continue
        out.append(item)

    if len(out) > limit:
        out = out[-limit:]
    return {
        "session_id": data.get("session_id") or sid,
        "exists": True,
        "status": data.get("status", "complete"),
        "status_label": STATUS_LABEL.get(data.get("status", "complete"), "未知"),
        "message_count": len(out),
        "saved_at": data.get("created_at"),
        "has_snapshot": bool(data.get("system_prompt")),
        "snapshot_head": _brief(data.get("system_prompt"), 160) if data.get("system_prompt") else None,
        "messages": out,
    }


def create_session_id(prefix: str = "session") -> str:
    base = f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"
    sid, n = base, 0
    while session_file(sid).exists():
        n += 1
        sid = f"{base}_{n}"
    return sid


def delete_session(sid: str) -> Dict[str, Any]:
    """删除 = 把该会话的**全部血缘**移入 agent_webui/.trash/（可恢复），绝不物理删除。

    血缘三件套（主人 2026-09-13 要求统一管上）：
      1. working_memory/{sid}.json              对话原文（唯一真相）
      2. .condensed_sessions/{sid}.json         上下文视图（原文的派生物）
      3. .condensed_sessions/{sid}.events.jsonl 压缩事件流
    视图与原文有血缘：只删原文、留下视图，就会冒出"没有会话却有视图"的孤儿——
    下次同名会话一出现，那份视图还可能被误认成它的历史。所以三件一起走。
    """
    src = session_file(sid)
    if not src.exists():
        return {"ok": False, "error": "会话不存在"}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    moved: List[Dict[str, str]] = []
    warn: List[str] = []

    # 1) 对话原文（主体，缺它就算删除失败）
    t1 = config.WEBUI_DIR / ".trash" / "working_memory"
    t1.mkdir(parents=True, exist_ok=True)
    d1 = t1 / f"{src.stem}.{stamp}.json"
    shutil.move(str(src), str(d1))
    moved.append({"kind": "session", "to": str(d1.relative_to(config.WEBUI_DIR))})

    # 2) 上下文视图 + 事件流（有则一起走；移不动只记警告，不影响会话已删的事实）
    t2 = config.WEBUI_DIR / ".trash" / "condensed_sessions"
    for p, kind in ((VIEW_DIR / f"{sid}.json", "view"),
                    (VIEW_DIR / f"{sid}.events.jsonl", "view_events")):
        if not p.exists():
            continue
        try:
            t2.mkdir(parents=True, exist_ok=True)
            dst = t2 / f"{p.stem}.{stamp}{p.suffix}"
            shutil.move(str(p), str(dst))
            moved.append({"kind": kind, "to": str(dst.relative_to(config.WEBUI_DIR))})
        except Exception as e:
            warn.append(f"{kind} 未随会话移走（{p.name}）: {e.__class__.__name__}: {e}")

    out: Dict[str, Any] = {"ok": True, "session_id": src.stem,
                           "moved_to": str(d1.relative_to(config.WEBUI_DIR)),
                           "moved": moved}
    if warn:
        out["warnings"] = warn
    return out
