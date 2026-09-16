# -*- coding: utf-8 -*-
"""用网关解释器逐个 import backend 模块，找出缺失的第三方依赖。

用法（必须在 agent_webui/backend 目录下）：
    <repo>/agent_webui/venv-gateway/Scripts/python.exe check_gateway_deps.py
"""
import importlib
import os
import sys
import traceback

BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)
# 网关进程同时会把仓库根与 agent/ 放进 sys.path（见 backend/config.py 的推导逻辑）
ROOT = os.path.dirname(os.path.dirname(BACKEND))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "agent"))

MODULES = [
    "config", "state", "events", "sse", "sessions", "workspace",
    "meta", "procguard", "agent_client", "agent_proc",
    "approval_adapter", "ask_user_tool", "clarify_batch",
    "mcp_view", "api", "main", "bridge",
]

missing = []
for name in MODULES:
    try:
        importlib.import_module(name)
        print("OK    %s" % name)
    except ModuleNotFoundError as e:
        missing.append((name, e.name))
        print("MISS  %s -> 缺包: %s" % (name, e.name))
    except Exception as e:
        # 非依赖问题的异常（如缺少运行期文件）单独标注，不算依赖缺失
        print("WARN  %s -> %s: %s" % (name, type(e).__name__, e))

print()
if missing:
    pkgs = sorted({m[1] for m in missing if m[1]})
    print("需要补的第三方包: %s" % ", ".join(pkgs))
    print("模块 -> 缺包: %s" % "; ".join("%s->%s" % (m, p) for m, p in missing))
else:
    print("✅ 所有 backend 模块均可 import，无缺失依赖")
