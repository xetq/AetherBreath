# 接入 GitHub MCP（官方 server）—— 从零到能调

> 目标：让 AB 用上 GitHub 官方 MCP server（仓库 / Issue / PR / 代码搜索）。
> 现状（2026-09-15 本机实测）：**二进制已装、station 已建、握手已通过**；
> 唯一差的是**你的 token**（没有 token 也能列工具，但一个工具都调不动）。

---

## 一、你要准备的东西

| 东西 | 在哪 | 说明 |
|---|---|---|
| 官方 server | **自己装**：`github/github-mcp-server` | Go 实现。编译或下载二进制后放进 PATH；也可改 `STATION.md` 的 `command:` 直接写绝对路径 |
| station | `agent_MCP/github/` | `STATION.md`（手写）+ `tools.yaml`（机器生成） |
| 工具清单 | `agent_MCP/github/tools.yaml` | **89 个工具**，schema 共 **128 KB**（约 3.2 万 token）——**这份东西不进上下文** |
| 注册表 | `agent_MCP/MCP_REGISTRY.md` | 只有一行：`\| github \| … \| 89 \| on \|` |

实测证据（真握手，非源码推测）：`github-mcp-server.exe stdio --toolsets all` →
`tools/list` 返回 89 个工具，`server_info.version = 1.12.1`，
客户端同时报出 `missing_env: ['GITHUB_PERSONAL_ACCESS_TOKEN']`。
（数字来自 v1.12.1；你装的版本可能不同 —— `mcp_manage test github` 会重新握手并刷新 `tools.yaml`。）

---

## 二、申请 token（Fine-grained，5 分钟）

1. 打开 **https://github.com/settings/personal-access-tokens/new**
   （GitHub → 右上头像 → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token）
2. **Token name**：随便填个能认出来的，例如 `aetherbreath-mcp`；**Expiration**：建议 90 天（到期再换，别用 No expiration）
3. **Resource owner**：选你自己；**Repository access**：
   - 只想让它干活的那几个仓库 → `Only select repositories`
   - 想全都能用 → `All repositories`
4. **Permissions**（按最小够用给；只读够用就别给写）：

   | 权限 | 级别 | 干什么用 |
   |---|---|---|
   | Metadata | Read（**必选**，自动勾上） | 基础信息 |
   | Contents | Read（要让它改文件/建分支再给 Read and write） | 读代码、搜索文件 |
   | Issues | Read（要让它开/关 issue 再给 write） | Issue |
   | Pull requests | Read（要让它发 PR 再给 write） | PR |
   | Actions / Commit statuses | Read | 看 CI 状态 |
   | Code scanning alerts / Dependabot | Read（可选） | 安全告警 |

   > 想更省事：先在权限页只勾 `Metadata: Read` + `Contents: Read`，用起来发现不够再加。
5. 点 **Generate token** → **页面只显示这一次**，立刻复制（`github_pat_...`）。

---

## 三、配置（两行，别把 token 写进仓库）

1. 打开项目根目录的 `.env`（**已被 `.gitignore` 忽略**，不会提交），加一行：

   ```
   GITHUB_PERSONAL_ACCESS_TOKEN=github_pat_你刚复制的那串
   ```

   ⚠️ 别写进 `agent_MCP/github/STATION.md` —— 那里只写 `${GITHUB_PERSONAL_ACCESS_TOKEN}` 占位，
   真值从 `.env` / 环境变量展开（这样 station 可以进 git，token 不会）。
2. 让运行中的进程看到它：
   - **命令行 / CLI**：重新起 agent 即可；
   - **WebUI**：`运行时` 面板 → 重新开机（重启网关也行，见第五节）。

---

## 四、验证它真能用

```bash
# 1) 注册表与目录一致吗（只读，不起进程）
venv/Scripts/python agent/mcp_station.py --check

# 2) 真握手 + 拉工具清单（这条不需要 token；会刷新 agent_MCP/github/tools.yaml）
venv/Scripts/python agent_MCP/mcp_sync.py --sync github

# 3) 真调一个工具（这条**需要** token）：在对话里让 AB 来
#    「用 mcp_manage 测一下 github，调 get_me」
#    或直接：「mcp_call(station="github", tool="get_me", arguments={})」
```

或者让 AB 自己来（在对话里说就行）：

```
用 mcp_search 看一下 github 有哪些工具，然后 mcp_call 调 get_me
```

AB 的动作顺序永远是：`mcp_search(station="github")` 拿 schema → `mcp_call(station="github", tool="…", arguments={…})`。

**看得到调用了什么**：「运行时」面板的 MCP 服务站区会显示 `在线 / 调用次数 / 最近 <工具名> <时间>`；
左侧工具时间线里那一步会写成 `mcp_call → github / get_me`。

---

## 五、开关与停止

| 想干什么 | 怎么做 |
|---|---|
| 临时关掉 github（不删） | 改 `agent_MCP/github/STATION.md` 的 `enabled: false`，再跑 `venv/Scripts/python agent/mcp_station.py` |
| 关掉整个 MCP（连 `mcp_search`/`mcp_call` 都不给模型） | `config.yaml` 的 `mcp.enabled: false` |
| 停掉跑着的 server 进程 | 随 agent 退出统一 `terminate`；手动清：`tasklist \| findstr /i github-mcp-server` |
| 只读模式（更保险） | `STATION.md` 的 `args` 加 `--read-only`，然后 `venv/Scripts/python agent_MCP/mcp_sync.py --sync github` 刷新工具清单 |
| 换 token / 撤销 | GitHub 的 token 页面 Revoke，重新生成，改 `.env`，重启 |

---

## 六、出问题怎么查

| 现象 | 原因 / 怎么办 |
|---|---|
| `❌ station 'github' 缺环境变量：GITHUB_PERSONAL_ACCESS_TOKEN` | token 没进 `.env`，或进程没重启（这是**快速失败**：不会再白等超时） |
| `❌ …401 Bad credentials` | token 贴错了 / 已过期 / 被 revoke |
| `❌ …403 Resource not accessible` | 权限没给够（回第二节第 4 步加权限），或仓库不在 `Only select repositories` 列表里 |
| 调用卡到超时（60s）后被 kill | server 起不来（二进制被删/不在 PATH）/ 网络不通；看 `agent_logs/*.jsonl` 里 `mcp:` 的 stderr 尾部 |
| 工具数不是 89 | `--toolsets` 变了：不加 `all` 是默认子集；改完记得 `mcp_sync --sync github` |

---

## 七、为什么值得这么接（v2 的设计账）

89 个工具、128 KB schema。若按老办法（把每个 MCP 工具注册成一等工具），这份 schema
**每一轮请求都要塞进上下文**（≈3.2 万 token / 次）。v2 改成：

- 系统提示词里只有**一行**：`| github | 官方 GitHub MCP server… | 89 | on |`
- 模型要用哪个工具，先 `mcp_search(station="github")` **现取**（89 个自动分两页）
- 真调走 `mcp_call`，每次都会问你要不要批准（`mcp.spawn` 卡，卡上有真实命令行）

实测这台的账：注册表全文 688 字节 / 12 行，工具表里 MCP 工具 **0 个**（只有 `mcp_search` /
`mcp_call` / `mcp_manage` 三个元/运维工具）。
