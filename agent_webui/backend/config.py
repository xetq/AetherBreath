# -*- coding: utf-8 -*-
"""WebUI 网关配置：路径推导 + 环境变量。

GitHub 友好铁律：
- 零硬编码绝对路径，一切基于本文件位置向上推导 PROJECT_ROOT
- 所有可覆盖项使用 AETHER_* 前缀环境变量
"""
from __future__ import annotations

import os
from pathlib import Path

# backend/ -> agent_webui/ -> 项目根
BACKEND_DIR = Path(__file__).resolve().parent
WEBUI_DIR = BACKEND_DIR.parent
PROJECT_ROOT = WEBUI_DIR.parent


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


# ===== 网关监听 =====
GATEWAY_HOST = _env("AETHER_WEBUI_HOST", "127.0.0.1")
GATEWAY_PORT = _env_int("AETHER_WEBUI_PORT", 8900)

# ===== Python 解释器：两套环境各司其职，均落在预期位置 =====
# 网关（fastapi/uvicorn）：agent_webui/venv-gateway
GATEWAY_PYTHON = Path(_env("AETHER_GATEWAY_PYTHON", str(WEBUI_DIR / "venv-gateway" / "Scripts" / "python.exe")))
# bridge（AB 本体，需 openai 等已有依赖）：项目根 venv
AGENT_PYTHON = Path(_env("AETHER_AGENT_PYTHON", str(PROJECT_ROOT / "venv" / "Scripts" / "python.exe")))
# POSIX 兜底
if not AGENT_PYTHON.exists():
    _alt = PROJECT_ROOT / "venv" / "bin" / "python"
    if _alt.exists():
        AGENT_PYTHON = _alt
if not GATEWAY_PYTHON.exists():
    _alt = WEBUI_DIR / "venv-gateway" / "bin" / "python"
    if _alt.exists():
        GATEWAY_PYTHON = _alt

BRIDGE_SCRIPT = BACKEND_DIR / "bridge.py"

# ===== 生命周期 =====
# 开机：等待 bridge 首行端口 JSON + health 探测的总超时
START_TIMEOUT_SEC = _env_int("AETHER_START_TIMEOUT", 60)
# 优雅关机：等待活动回合在边界保存退出的超时，超时自动转 kill
GRACEFUL_STOP_TIMEOUT_SEC = _env_int("AETHER_GRACEFUL_TIMEOUT", 15)
# clarify 等待主人答复的最长时间
CLARIFY_TIMEOUT_SEC = _env_int("AETHER_CLARIFY_TIMEOUT", 120)
# 审批等待窗口：比 clarify 长，因为"人不在屏幕前"是常态；超时=拒绝
APPROVAL_TIMEOUT_SEC = _env_int("AETHER_APPROVAL_TIMEOUT", 300)

# ===== 前端产物（日常模式静态托管）=====
FRONTEND_DIST = WEBUI_DIR / "frontend" / "dist"

# ===== 项目内路径（读 config.yaml，失败降级默认值）=====
_DEFAULT_PATHS = {
    "working_memory": "agent_memory/working_memory",
    "workspace": "agent_workspace",
    "long_memory": "agent_memory/long_memory",
    "skills_dir": "agent_skills",
}


def load_project_paths() -> dict:
    """读取 config.yaml 的 paths 段。

    网关环境未必装了 pyyaml，因此解析失败时退回默认值——
    这些默认值与项目 config.yaml 当前内容一致。
    """
    paths = dict(_DEFAULT_PATHS)
    cfg = PROJECT_ROOT / "config.yaml"
    if not cfg.exists():
        return paths
    try:
        import yaml  # type: ignore

        with open(cfg, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        paths.update({k: v for k, v in (data.get("paths") or {}).items() if isinstance(v, str)})
    except Exception:
        # 无 pyyaml：平面扫描 paths: 段下的 "key: value"
        try:
            inside = False
            for line in cfg.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("paths:"):
                    inside = True
                    continue
                if inside:
                    if line and not line.startswith((" ", "\t", "#")):
                        break
                    if ":" in line and not line.strip().startswith("#"):
                        key, _, val = line.strip().partition(":")
                        val = val.strip().strip('"').strip("'").split("#")[0].strip()
                        if key and val:
                            paths[key] = val
        except Exception:
            pass
    return paths


PROJECT_PATHS = load_project_paths()
WORKING_MEMORY_DIR = PROJECT_ROOT / PROJECT_PATHS["working_memory"]
WORKSPACE_ROOT = PROJECT_ROOT / PROJECT_PATHS["workspace"]
SKILLS_DIR = PROJECT_ROOT / PROJECT_PATHS.get("skills_dir", "agent_skills")

# ===== 日志面板（只读展示，不落敏感信息）=====
WEBUI_LOG_DIR = WEBUI_DIR / "logs"


def gateway_python() -> Path:
    if not GATEWAY_PYTHON.exists():
        raise FileNotFoundError(
            f"网关 Python 不存在: {GATEWAY_PYTHON}\n"
            f"请先创建: {(PROJECT_ROOT / 'venv' / 'Scripts' / 'python.exe')} -m venv {WEBUI_DIR / 'venv-gateway'}\n"
            f"然后安装: pip install -r {BACKEND_DIR / 'requirements.txt'}"
        )
    return GATEWAY_PYTHON


def agent_python() -> Path:
    if not AGENT_PYTHON.exists():
        raise FileNotFoundError(f"AB 本体 Python 不存在: {AGENT_PYTHON}（项目根 venv 缺失？）")
    return AGENT_PYTHON
