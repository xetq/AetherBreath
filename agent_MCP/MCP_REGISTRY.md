# MCP_REGISTRY.md（MCP 服务站注册表）

> 本文件由 `agent/mcp_station.py` **自动生成**（机器所有，别手改；改 station 文件夹即可）。
> 一个 station = 一个 MCP server = `agent_MCP/<station>/` 一个文件夹，**文件夹存在即注册**。
> 注册表只说明「有什么」，**不含工具参数 schema** —— 要用时先 `mcp_search` 取，再 `mcp_call` 调。

| station | 用途 | 工具数 | 状态 |
|---|---|---|---|
| github | 官方 GitHub MCP server：仓库/Issue/PR/代码搜索（**需要 token**，见 docs/MC | 89 | on |
| time | 官方 time server：查某时区当前时间、时区间换算 | 2 | on |

（共 2 个 station）
