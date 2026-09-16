---
name: time
description: 官方 time server：查某时区当前时间、时区间换算
command: uvx
args: ["mcp-server-time"]
enabled: true
timeout: 20
never_parallel: false
origin: hand
---

# time —— 时区与时间

官方 Python 参考实现（`uvx mcp-server-time`），本机实测可用。

- `get_current_time(timezone)` —— 查某个 IANA 时区的当前时间
- `convert_time(source_timezone, time, target_timezone)` —— 时区之间换算

**运行前提**：PATH 上有 `uv`（提供 `uvx`）；首次运行会联网下载包。

**要改的东西**：开关/超时/参数都在上面那份 frontmatter 里。改完跑一次
`venv/Scripts/python agent/mcp_station.py` 同步注册表（重启 agent 后注入才生效）。
工具 schema 不在这里 —— 它在 `tools.yaml`（机器生成，`mcp_sync --sync time` 刷新）。

> 建议把 `args` 固定版本（`["mcp-server-time==2026.8.18"]`）：不锁版本等于上游塞什么就认什么。
> AB 自己集成 station 时会自动固定；这里是手写，靠你自己拧紧。
