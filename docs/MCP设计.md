# MCP 系统设计 v2 —— station 化 + 元工具（渐进式披露）

> 状态：**已定稿**（2026-09-15 逐条拷问确认，26 题）。**取代 v1**（import 期注册成一等工具，见文末「历史」）。
> 实施分阶段见 §十一；动手前先删清单见 §九。三个**待确认口径**在 §三/§五 里标了 ⚠️，默认值已写明。

---

## 一、为什么要重构（动机全部来自实测）

| # | 问题 | 证据 |
|---|---|---|
| 1 | **不能热启动**（主因） | v1 的工具必须在 **import 期**注册（桥接层启动时逐个包装 `AVAILABLE_TOOLS`；编排器 `__init__` 复制工具快照）。于是加/改一个 server 要重启 agent，注册表永远追不上运行时 |
| 2 | **上下文成本**（本次要解决的第二个问题） | "几个 MCP server 一开，几百个工具涌入上下文，我不愿为不常用的工具买单"。实测官方 **GitHub MCP server = 112 个工具**（名字+描述 12KB ≈ 4k tokens；**完整 schema 只会更大**）；生产环境还要装 WPS 等更多 |
| 3 | **维护不现代** | 加 server = 手写 YAML + 跑同步脚本 + 重启；工具清单散在 `servers.yaml` 与 `tools/` 两处；WebUI 看不到 MCP 运行状态 |

**核心 KPI**：注册表注入体积 **只与 station 数线性、与工具数无关**；新增 station **不重启、不新开会话**即可用。

（另有附带必修项：`MCP/` → `agent_MCP/` 改名曾让路径失联、MCP 静默失效 —— 已在提交 `e703aba` 修好。）

---

## 二、设计总览（一句话）

**一个 server = 一个 station 文件夹；注册表只告诉模型"有哪些 station"；工具 schema 按需现取；
调用只走两个元工具。** 于是：不常用的工具描述永不进上下文，而"新增 station 立刻可用"由
`mcp_search` 读文件夹保证（不靠改系统提示词）。

```
系统提示词（冻结）          运行时（实时）
┌────────────────────┐     ┌──────────────────────────────┐
│ MCP_REGISTRY.md 快照 │     │ mcp_search(station) → 全量 schema │
│  · time    时间查询 2 │ ──▶ │ mcp_call(station, tool, args)  │
│  · github  仓库操作 112│     │        └→ mcp_client → stdio server │
└────────────────────┘     └──────────────────────────────┘
      一行 / station              按需、只取在用的那一个
```

---

## 三、station（服务站）

### 文件夹布局：`agent_MCP/<station>/`

| 文件 | 谁写 | 内容 |
|---|---|---|
| `STATION.md` | **手写** | frontmatter：`name` / `command` / `args` / `env` / `enabled` / `timeout` / `never_parallel` / `description` / `origin`；正文 = 给人看的说明（渐进式披露的深层资料） |
| `tools.yaml` | **机器生成**（`mcp_sync`） | 该 station **全部工具的完整 schema**（MCP 原样 `inputSchema`） |
| `NOTES.md` | 手写（可选） | 使用备注、坑、token 怎么配 |

**纪律**：手写文件机器**永不改写**（PyYAML 写回会抹掉注释）；机器只改 `tools.yaml`
和 `origin: auto` 的 station 自己的文件。

> ⚠️ **待确认口径 1（默认值）**：工具 schema **一个 station 一个文件**（`<station>/tools.yaml`），
> 不搞"每个工具一个文件"。理由：读一个 station 只需读它自己那一个文件，已满足"按需索取"；
> 112 个工具 = 112 个小文件的收益（更细的按需）抵不过文件数与 `.gitignore`/备份复杂度。
> 要"每工具一文件"（`<station>/tools/<tool>.yaml`）就说一声，改的是 `mcp_sync` 一处写法。

> ⚠️ **待确认口径 2（默认值）**：AB 自己集成的 station 与主人的 station **同放 `agent_MCP/`**，
> 靠 `STATION.md` 的 `origin: hand|auto` 区分；`mcp_manage` 只动 `origin: auto` 的（手写的拒绝改写）。
> 理由：station 是"一个文件夹 = 一个单位"，混放才符合"文件夹存在即注册"；再开一个 `.auto/` 目录
> 会让"同一个 server 可能在两处"这种状态出现。

### 注册语义

- **文件夹存在即注册**（与技能系统同构）；扫描时按 `name` 去重、重名记账不静默。
- `enabled: false` 的 station 不进注册表，也不允许 `mcp_call`（fail-closed，报错写清原因）。
- 全局总闸 `config.yaml` 的 `mcp.enabled`（默认 `true`）：`false` = 连两个元工具都不注入、
  注册表不注入（"MCP 常开"由这里决定）。

---

## 四、注册表与注入（冻结，但不影响"热"）

- **文件**：`agent_MCP/MCP_REGISTRY.md`，**机器生成**（对标 `SKILL_REGISTRY.md`）。
  每个 station 一行：`name | 一句话用途 | 工具数 | 开关`。人工可读、可 diff。
- **注入时机与位置**：会话启动时同步一次，随后随系统提示词**冻结注入**
  —— 接在 `agent/agent.py` 现有技能注册表那条路上（`load_system_prompt()` 里加一块），
  同一套"Freeze on Start"语义。**`agent.py` 仍只做路由**：渲染逻辑全在 `agent/mcp_station.py`。
- **为什么冻结**：① 命中 provider 的 **prompt cache**（前缀不变才缓存得住，直接省钱）；
  ② 系统提示词每轮变会污染上下文管理器与调试。
- **冻结 ≠ 不能热**：`mcp_search` **直接读文件夹**（实时真相）。同一会话内新加的 station
  不在冻结的注册表里，但 search 能找到 —— 这就是本次要的"热启动"。

---

## 五、两个元工具

### `mcp_search(query)` —— 取某 station 的工具清单

- 用 `query` 在 station **名字 / 用途 / 工具名 / 工具描述**里匹配：
  - **唯一命中** → 返回该 station **全部工具的完整 schema**（Q13-C）；
  - **多个命中** → 返回候选清单（名字 + 用途 + 工具数），让模型用精确名字再查一次。
- 为什么给全量而不是"只回命中的那几个"（Q13-C 的理由，原话大意）：*"如果选 A 没命中需要的工具怎么办？
  选 B 的话 LLM 一开始不知道这个 server 有什么工具和 schema，而且维护'精简版'还要额外成本；
  与其让它猜，不如让它完整知晓 —— 单一 server 上不是最优，但背景是多 server 常开，优势很明显。"*
- 只读，不启动任何 server 进程（读的是 `tools.yaml`）。

> ⚠️ **待确认口径 3（默认值）**：单个 station 工具数**超过 60** 时自动分页
> （返回"第 1/N 页"+ 提示 `page=2`），避免一次把 112 个 schema（几十 KB）灌进上下文。
> 60 以下不分页（一次给全，符合 Q13-C）。要"永远一次给全"就把这个上限设成 0（关掉分页）。

### `mcp_call(station, tool, arguments)` —— 调用

- `arguments` 是结构化对象；也接受 JSON 字符串（模型常写成串，Q14-A：**不让 LLM 手搓 JSON**）。
- 落在 `agent_tools/mcp_gateway.py` 一个文件里导出两个工具（Q20-A：3 个文件，比 v1 的 5 个少）。
- 都走任务编排器 ⇒ 自动获得：依赖分层/并行、`logger` 签名注入、`❌` 业务失败语义、真超时 kill。

---

## 六、上下文与压缩（明确"不做"）

- 注册表：一行/station；工具 schema：只在 `mcp_search` 结果里出现。
- **不做**上下文管理器的"重点/不压缩"标识（Q25-A，YAGNI）：现有 `keep_recent_rounds=10`
  已经保护最近 10 轮，而 `search → call` 通常隔 1–2 轮。真被压掉过再回来按最小方案加
  （`shrink_message` 里加一条"含 X 标记则不折叠"）。

---

## 七、审批与安全

- **`mcp.spawn` 重写为认 `mcp_call`**（Q17-A）：首次调用某 station 弹一次卡
  （卡上：**真实命令行** + 内部工具名 + 参数摘要），该 station 进程起来过之后不再问；
  `mcp_search` 只读，**不问**。
- **新增安全增益（v1 没有的能力，Q18-A）**：把 `mcp_call.arguments` **展开给词法/规范层** ——
  于是 `cdrive.files` / `outzone.write` / `secrets.read` 能兜住"用 MCP 去写系统盘 / 读凭据"。
  新设计把 MCP 调用**收窄到一个工具名**，正是做这件事的最佳时机。
- **集成（`mcp_manage add`）不设专门闸门**（主人 2026-09-14 决定，退役原文存
  `agent_workspace/agent_self_maintenance/retired_mcp_install.py.txt`）：AB 自己的纪律是
  动手前说清包名/来源；试跑后**撤销"已批准"**，所以第一次真用仍有一张启动卡。

---

## 八、自维护（`mcp_manage` 适配，Q21-A）

六个动作（search / list / add / test / set_enabled / remove）全部保留，**目标从"auto 表"改成"station 文件夹"**：
用户给信息 → AB 自己注册 / 删除 / 开启 / 关闭 station。

- `add`：校验 → **固定版本**（`npx pkg@x.y.z` / `uvx pkg==x.y.z`，探不到就报错让人查）→
  建 `agent_MCP/<name>/`（写 `STATION.md`，`origin: auto`）+ 跑 `mcp_sync` 生成 `tools.yaml`
  → **真启动试跑** → 撤销"已批准"（保住首次真调用的那张卡）→ 有界自愈（失败 2 次停手 + 真回滚）。
- 手写 station 一律拒绝改写（与 v1 同纪律）。

---

## 九、文件地图与**删除清单**（v1 的孤儿代码）

| 文件 | 处置 |
|---|---|
| `agent/mcp_client.py` | **保留**：stdio JSON-RPC、连接锁、真超时 kill、stderr 背压、结果降维、日志 —— **transport 一行不改**（36 条用例在守它）。只把"注册表读取"迁去 `mcp_station`，改为接受 station spec 对象 |
| `agent/mcp_station.py` | **新建**：扫 station 文件夹 / frontmatter 解析 / 注册表渲染与同步 / 注入块 / 工具 schema 读取。**不碰 `skill_system.py`**，只借鉴它的形状（Q9-B） |
| `agent_tools/mcp_gateway.py` | **新建**：导出 `mcp_search` / `mcp_call` |
| `agent_tools/mcp_manage.py` | **保留**（自维护仍是 agent 的工具），适配 station 目标 |
| `agent_MCP/` | 数据 + `mcp_sync.py` + `README.md`；旧 `servers.yaml` / `tools/*.yaml` ✅ **已删净**（2026-09-15，`time`/`github` 都迁成 station 文件夹）；v1 真机探针 `probe_live.py` 也**已删**（2026-09-15：能力由 `mcp_station.py --check` + `mcp_sync.py --sync` + `mcp_manage action=test` 覆盖） |
| `agent_tools/mcp_tool_map.py` | ✅ **已删**（2026-09-15）（"把 station 工具变一等工具"整块废弃） |
| `agent_tools/__init__.py` 的 MCP 注册块 + 热注册 API | ✅ **已删**（2026-09-15）（`register_tools` / `unregister_tools` / `find_orchestrator` / `set_tool_wrapper` / `registry_status`） |
| `agent/task_orchestrator.py` 的 `register_tools` / `unregister_tools` / `registered_tools` | ✅ **已删**（2026-09-15） |
| `agent_webui/backend/bridge.py` 的 `_late_wrap` / `_install_late_wrap_hook` + 每回合兜底 | ✅ **已删**（2026-09-15）；**启动期** `_wrap_pending_tools()` 保留（界面事件通道靠它） |
| `tests/` 里"注册与 schema 导出"相关用例 | ✅ **已重写**（2026-09-15，删掉测已删机制的用例） |
| `agent/approvals/mcp_spawn.py` | 重写判定目标（认 `mcp_call`） |

---

## 十、WebUI（你新加的两条需求）

1. **「运行时」面板**（Q3-C）：列出各 station 的**在线状态**（进程活着/调用次数/工具数/开关），
   **开机自动刷新**。
   - 网关：新增 `/api/mcp/status`（FastAPI，`agent_webui/backend/`），数据从 agent 侧取
     `mcp_client` 的连接状态（`peek_server_info` 已有）。
   - 前端：`agent_webui/frontend/src/panels/` 下新增运行时面板（现有 `panels/` 体系里加一块）。
2. **调用展示**（Q26）：MCP 调用在界面上要能看出**哪个 station 的哪个工具** ——
   工具行显示成 `mcp_call → github / issue_write`（参数里已有 station/tool，展示层拼一下即可）。

---

## 十一、测试与验收

**KPI（Q24-A）**
1. 新增 station 后**不重启 agent、不新开会话**：同会话内 `mcp_search` 能找到 + `mcp_call` 调用成功；
2. 注入块体积只与 station 数线性（112 工具的大 station 在注册表里**只占一行**）；
3. 全量 `pytest tests` 绿；
4. 真机跑通 GitHub MCP —— ⚠️ **需要 GitHub token，交付时必须附"怎么申请并配置"的步骤**（Q24 明确要求教）。

**测试改造（Q23-A）**：保留 transport（分帧/对号入座/超时 kill/stderr 背压）与审批语义；
重写"注册与 schema 导出"为"station 扫描 / 注册表渲染 / 元工具"；新增
① 注入体积与工具数无关 ② 112 工具 station 只占一行 ③ 同会话热发现（刚建的 station 立刻能 search 到）。

**分阶段（每阶段可验收、可回滚）**

| 阶段 | 内容 | 验收 |
|---|---|---|
| P0 | 改名 + 路径修复 | ✅ 已完成（`e703aba`） |
| P1 | `mcp_station.py`（扫描/frontmatter/注册表/注入）+ 把 `time` 迁成 station | ✅ **2026-09-15 完成**：`tests/test_mcp_station.py` 25/25；注册表一行/station；注入块 1245 字节；station 模式下真调用 `time` 返回 `2026-09-15T18:51:03+08:00`；全量 237 passed / 2 skipped |
| P2 | `mcp_gateway.py`（`mcp_search`/`mcp_call`）+ 接线（`agent_tools`、系统提示词注入一行） | ✅ **2026-09-15 完成**：`tests/test_mcp_gateway.py` 18/18；端到端探针改走 v2（11 步全过）；工具表里 **0 个** MCP 工具（112 工具的 station 也一样）；真调用 `time` 成功；全量 258 passed / 3 skipped |
| P3 | 审批：`mcp.spawn` 认 `mcp_call` + `arguments` 路径审查 | ✅ **2026-09-15 完成**（与 P2 同批做：P2 一落地 `mcp__*` 工具就消失，旧规范认不出 `mcp_call` → 会有一段**无人看守**的窗口，所以合并实施）。卡上写清"哪个 station / 哪个内部工具 / 真实命令行"；批准过就静默；`approval.extract_paths` 能扫到 `arguments` 里的路径 → outzone/secrets 类规范对 MCP 仍够得着（有测试钉住） |
| P4 | `mcp_manage` 适配 station | ✅ **2026-09-15 完成**（面板/调用展示见 P4b）：集成 = 建文件夹、开关只改一行、移除进 `.trash/`；只动 `origin: auto`，手写文件字节不变；端到端探针重写为 v2（12 步全过）；全量 270 passed / 2 skipped。中途揪出真事故：`registry_path()` 曾优先读 config → 测试把注册表写进真实仓库（已修成"永远跟着 station 目录走"） |
| P4b | WebUI「运行时」面板 + 调用展示 `mcp_call → station/tool` | ✅ **2026-09-15 完成**（Q3-C / Q26）：`GET /api/mcp/stations`（组装拆到 `backend/mcp_view.py`，不依赖 fastapi → 可单测）；「运行时」面板新增 MCP 服务站区（**常驻**、5s 自刷、等高行、1.2k 缩写）；时间线里 `mcp_call` 显示成 `mcp_call → station / tool`；客户端记 `last_tool/last_call_at/last_call_ok`（含失败与抛异常）。前端 `npm run build` 通过（tsc + vite）。**要重启网关才生效**（网关同进程 import 了 AB 本体） |
| P5 | **删孤儿**（§九 清单）+ 文档收口 + 真机 GitHub 验证 | 🟡 **进行中 2026-09-15**：真机 GitHub 已完成（二进制 v1.12.1 装好，真握手 89 工具 / schema 128KB；注册表全文 688 字节 12 行、工具表 MCP 工具 **0** 个；token 教程 `docs/MCP-GitHub接入.md`）。孤儿删除由子 agent 执行中，完成后复核 + 全量回归 |

---

## 历史：v1（2026-09-14，已退役）

v1：MCP server 的工具在 **import 期**注册成一等工具（`mcp__<server>__<tool>`），配套自维护工具
`mcp_manage` 与"热注册三处"（编排器 `register_tools` / `agent_tools` 热注册 API / bridge late-wrap）。
代码见提交 `23c2408` 与 `5024474`；退役原因见 §一。

**v1 留下的两条硬约束仍然重要**（理解 agent 启动顺序的关键，v2 也要绕开它们）：
① 桥接层启动时逐个包装 `AVAILABLE_TOOLS`（晚注册的工具界面看不到）；
② `TaskOrchestrator.__init__` 会复制工具快照，而 bridge 在主线程预建常驻编排器（晚注册 = "未知工具"）。
→ v2 的答案是：**MCP 工具不再进工具表**，只有两个元工具（import 期注册，恒定），所以两条约束自动失效。
