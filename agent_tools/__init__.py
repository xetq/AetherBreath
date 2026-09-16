# agent_tools/__init__.py
from typing import Any, Dict                    # 下面 station 自检报告的注解要用（别删）

from .file_read import read_file, read_file_schema
from .calculator import calculator, calculator_schema
from .search import search, search_schema
from .web_reader import fetch_url, fetch_url_schema
from .web_extract import web_extract, web_extract_schema
from .rag import rag_query, rag_query_schema
from .execute_python import execute_python, execute_python_schema
from .execute_shell import execute_shell, execute_shell_schema
from .execute_browser import execute_browser, execute_browser_schema
from .create_tool import create_tool, create_tool_schema
from .skillhub_download import skillhub_install, skillhub_install_schema

from .time_weather import time_weather, time_weather_schema
from .restore_context import restore_context, restore_context_schema
# 自维护：AB 自己集成/管理 MCP server（建 station 文件夹、固定版本、连通性测试）
# ⚠️ 小坑（写给将来的人）：这条 import 会让**包的属性名** `agent_tools.mcp_manage`
#    指向这个**函数**，而不是同名子模块 —— 模块名与导出函数同名就会这样。
#    要拿模块本体请用 importlib.import_module("agent_tools.mcp_manage")（测试里就是这么做的）。
from .mcp_manage import mcp_manage, mcp_manage_schema
# gateway：MCP 的**两个元工具**（v2 —— MCP 工具不再进常驻工具表，见 docs/MCP设计.md v2 §五）。
# 每个 MCP 工具都注册成一等工具是 v1 的做法；一个 station 可能有上百个工具（官方 GitHub MCP
# = 112 个），全量常驻上下文是纯浪费。现在改为：注册表注入（只报"有什么"）+ 按需 mcp_search 取 schema。
from .mcp_gateway import mcp_search, mcp_call, mcp_search_schema, mcp_call_schema


# ========== 2. 自动构建注册表（给 agent.py 直接用） ==========
AVAILABLE_TOOLS = {
    "read_file": read_file,
    "calculator": calculator,
    "search": search,
    "fetch_url": fetch_url,
    "web_extract": web_extract,
    "rag_query": rag_query,
    "execute_python": execute_python,
    "execute_shell": execute_shell,
    "execute_browser": execute_browser,
    "create_tool": create_tool,
    "skillhub_install": skillhub_install,
    "time_weather": time_weather,
    "restore_context": restore_context,
    "mcp_manage": mcp_manage,
}


def _mcp_gate_enabled() -> bool:
    """MCP 总闸（`config.yaml` 的 `mcp.enabled`）。口径：关掉 = 连两个元工具都不给。

    拿不到配置就默认**开**：这两个工具本身不做危险事（一个读文件夹，一个走 mcp_client 的
    审批+日志链路），不注册它们只会让模型 "没有 MCP 能力"，而不是安全增益。
    """
    try:
        import mcp_station                       # noqa: PLC0415（运行期扁平导入）
        return bool(mcp_station.mcp_enabled())
    except Exception:
        return True


MCP_GATE_ON = _mcp_gate_enabled()
if MCP_GATE_ON:
    AVAILABLE_TOOLS["mcp_search"] = mcp_search
    AVAILABLE_TOOLS["mcp_call"] = mcp_call

TOOLS_SCHEMA = [
    read_file_schema,
    calculator_schema,
    search_schema,
    fetch_url_schema,
    web_extract_schema,
    rag_query_schema,
    execute_python_schema,
    execute_shell_schema,
    execute_browser_schema,
    create_tool_schema,
    skillhub_install_schema,
    time_weather_schema,
    restore_context_schema,
    mcp_manage_schema,
]
if MCP_GATE_ON:
    TOOLS_SCHEMA += [mcp_search_schema, mcp_call_schema]

# ========== 3. MCP：**不再**把每个 MCP 工具注册成一等工具（v2）==========
# v1 在这里把 registry 里每个 server 的每个工具注册成 `mcp__<server>__<tool>`；v2 改成
# **两个元工具**（上面的 mcp_search / mcp_call）+ 注册表注入（docs/MCP设计.md v2 §五）。
# 为什么改：一个 station 可能上百个工具（官方 GitHub MCP = 112 个），全量常驻工具表 = 每轮
# 白吃几万 token；而且新增 station 在 v1 里要重启 agent（import 期注册的硬约束）。
# 这段只做一次**只读体检**（station 目录有几个、有没有坏 station）——不起进程、绝不抛，
# 因为它跑在 agent 的 import 路径上（这里炸掉 = agent 起不来）。
def _mcp_station_report() -> Dict[str, Any]:
    report: Dict[str, Any] = {"mode": "station", "enabled": False, "stations": 0,
                              "servers": 0, "registered": 0, "problems": [], "errors": []}
    try:
        import mcp_station                                        # noqa: PLC0415
        found, issues = mcp_station.scan_stations()
        report["enabled"] = bool(mcp_station.mcp_enabled())
        report["servers"] = len(found)
        report["stations"] = len([s for s in found if s.enabled and s.usable])
        report["problems"] = list(issues)
        report["errors"] = list(issues)        # 老字段名保留：谁读它都能看到"哪里不对"
    except Exception as e:
        report["errors"] = ["MCP station 自检失败：%s: %s" % (type(e).__name__, e)]
    return report


MCP_REGISTRATION = _mcp_station_report()

# ========== 4.（已删）运行期热注册 ==========
# v1 这里曾有一整套"运行期热注册"：把新集成的 MCP 工具原地写进 AVAILABLE_TOOLS / TOOLS_SCHEMA、
# 再灌进常驻编排器、并让宿主（bridge）补上界面事件包装 —— 为的是"装完就能用，不必重启"。
# v2 之后 MCP 工具**不再**进常驻工具表（常驻的只有 mcp_search / mcp_call 两个元工具），
# "装完就能用"改由**每次实时读 station 文件夹**天然拿到，不需要往工具表里写任何东西 ——
# 于是这套机制成了孤儿代码：把 MCP 工具编译成函数的那一整个模块、编排器的运行期注册方法、
# bridge 的 late-wrap 钩子，都在 2026-09-15 一并删除。设计依据：docs/MCP设计.md v2 §五。
# 别再顺手加回来：让上百个 station 工具重新常驻 = 每轮白烧几万 token，正是改 v2 的直接原因。

# 暴露所有工具和 Schema 供外部导入
__all__ = [
    "read_file", "read_file_schema",
    "calculator", "calculator_schema",
    "search", "search_schema",
    "fetch_url", "fetch_url_schema",
    "web_extract", "web_extract_schema",
    "rag_query", "rag_query_schema",
    "execute_python", "execute_python_schema",
    "execute_shell", "execute_shell_schema",
    "execute_browser", "execute_browser_schema",
    "create_tool","create_tool_schema",
    "skillhub_install","skillhub_install_schema",
    "time_weather","time_weather_schema",
    "restore_context","restore_context_schema",
    "MCP_REGISTRATION",
]
