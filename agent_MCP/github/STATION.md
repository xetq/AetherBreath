---
name: github
description: 官方 GitHub MCP server：仓库/Issue/PR/代码搜索（**需要 token**，见 docs/MCP-GitHub接入.md）
command: github-mcp-server.exe
args: ["stdio", "--toolsets", "all"]
env:
  GITHUB_PERSONAL_ACCESS_TOKEN: "${GITHUB_PERSONAL_ACCESS_TOKEN}"
enabled: true
timeout: 60
never_parallel: false
origin: hand
---

# github —— 官方 GitHub MCP server

GitHub 官方 Go 实现（[github/github-mcp-server](https://github.com/github/github-mcp-server)）。
把编译好的 `github-mcp-server`（或 `.exe`）放进 PATH，或在 `command:` 里写绝对路径。

- 启动：`github-mcp-server.exe stdio --toolsets all`（`--toolsets` 决定暴露哪些工具；
  不加就是默认子集。想只读可以加 `--read-only`）
- **token**：`GITHUB_PERSONAL_ACCESS_TOKEN`，从 `.env` / 环境变量展开。
  申请与配置步骤见 `docs/MCP-GitHub接入.md`（**没 token 也能握手列工具，只是调不动**）
- 工具很多（全量上百个）—— 这正是不把 schema 注入上下文的原因：要用哪个工具，
  先 `mcp_search(station="github")` 取它的 schema，再 `mcp_call` 调。
