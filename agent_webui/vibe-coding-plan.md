# AetherBreath WebUI — Vibe Coding 提示词方案 v0.3（前后端分离版）

> 本文件由外部 agent 分析 AetherBreath 项目后撰写。
> 用途：**给 AetherBreath 的自举开发任务书**——主人把 §4 的提示词按顺序复制进 VSCode 终端会话，AetherBreath 作为主角自己开发自己的 WebUI。
> 原则：只出方案与提示词，不含实现代码（实现由 AetherBreath 在 vibe coding 中完成）。
>
> v0.2 → v0.3 变更（主人拍板）：
> - **前后端分离**：前端 = 独立 npm 工程（Vite + React + TS）；后端 = FastAPI 网关（Python）。
> - 保留 v0.2 的**子进程生命周期模型**（AB 本体是独立 bridge 子进程，可强制关闭防卡死、可重开续聊）。
> - 分离理由：好维护——改 UI 不碰后端、改 API 不碰页面，前后端可独立开发/测试/演进。

---

## 0. 背景速览（写给 AetherBreath 自己看）

检测到的现状（2026-09-07 实查）：

- **本体**：`python agent/agent.py` → CLI `input()` 循环。核心函数：
  - `call_agent_with_tools(messages, session_id, log, ...)` —— **一次调用 = 一个完整回合**（内部最多 `MAX_ITERATIONS` 轮：LLM 请求 → 工具编排 → 再请求），结束时返回 `{content, conversation, iterations}`。它是整个 WebUI 的能力内核。
  - `load_session(session_id, log)` / `save_session(session_id, messages, log, status)` —— 会话持久化，文件在 `agent_memory/working_memory/{session_id}.json`。**每轮工具执行后都会自动 save_session** → 进程被杀最多丢"当前半轮"，重开可续。
  - `load_system_prompt(log)` —— 组装系统提示（long_memory 下 SOUL/AGENTS/MEMORY/USER + 技能注册表），**新会话构建后冻结**（快照），续聊无条件复用。
- **工具**：`agent_tools/__init__.py` 注册 12 个工具，执行统一走 `task_orchestrator`（单工具串行 / 多工具并行 + 去重 + 中断标志 `is_interrupted`）。
- **配置**：`config.yaml` 管路径；`.env` 管 LLM 三件套；主对话 `stream=False`（LLM 无 token 流——WebUI 的"流"是**事件流**：阶段变化/工具调用/文本块，不是逐 token）。
- **已有 Web 参考**：`subagents/多agent系统/orchestrator.py` —— `http.server` 零依赖 Web UI 先例。
- **环境**：Python venv 在项目根 `venv/`（含 openai/dotenv/yaml 等，bridge 必须用它才能 import agent）；Node v22 + npm 10 可用（前端工程用）；LLM 走 OpenAI 兼容端点。
- **愿景**：`建设说明.txt` —— agent 将来是"通用壳 + 可切换垂直模式"。WebUI 要为其预留扩展位。

## 1. v1 功能范围（主人确认）

1. **聊天主界面 + 多会话管理**：新建 / 续聊 / 历史列表 / 切换 —— 替代 VSCode 终端调试对话。
2. **实时展示工具调用过程**：时间线（工具名 → 参数 → 返回摘要 → 前后顺序）。
3. **工作区状态展示**：`agent_workspace/` 任务子目录、最近活动文件、会话落盘情况（只读）。
4. **状态机**：会话生命周期状态可视 + 后端上报 + 前端渲染（§3.4）。
5. **clarify 审批机制**：agent 不确定时暂停提问（单选/多选/自由文本），主人答复后继续。
6. **AB 进程生命周期控制（"开机/关机"按钮）**：打开前端时 AB 未运行 → 点「🟢 开机」拉起 bridge 子进程 → 运行中可「⏻ 优雅关机」或「🛑 强制关闭」（kill 防卡死）→ 随时重开，同会话可续聊（最多丢被 kill 的半轮）。

## 2. 进程拓扑与技术选型（主人确认）

```
┌─ 前端工程 agent_webui/frontend/（Vite + React + TS，独立 npm 工程）
│    开发期: npm run dev（vite dev server，proxy /api → 127.0.0.1:8900，免 CORS）
│    日常/部署: npm run build → dist/ 由后端托管（同源，一条命令起）
│
└─ 后端工程 agent_webui/backend/（FastAPI + uvicorn，Python，装进项目 venv）
    │    main.py：FastAPI 应用（REST + SSE + 静态托管 dist，/docs 自动 API 文档）
    │    spawn → bridge.py（Python 子进程 = AB 本体，随机端口本地 HTTP）
    └── AB 进程链路：后端 AgentManager → bridge(import agent 模块，真跑 LLM/工具)  ← 可 kill / 重启
```

**技术选型与理由：**
- 网关后端 **FastAPI**：与 bridge/AB 本体同语言 → spawn/kill 子进程、日志、类型最顺；自带 OpenAPI `/docs`，以后加 API 好维护。新增依赖仅 `fastapi` + `uvicorn`（装进项目根 `venv/`，README 声明；**这是主人已批准的两个新依赖**，其它新依赖仍需先请示）。
- 前端 **Vite + React + TS**：组件化利于"加面板"扩展；TS 类型与 FastAPI 的 OpenAPI schema 可对齐（增强项，v1 手写类型即可）；独立 npm 工程，`frontend/package.json` 自持依赖。
- **保留子进程模型**：AB 本体 = bridge 子进程（Python，import agent 模块），因为 Python 杀不死卡死线程，只有进程边界能提供"强制关闭"。
- **bridge 与后端通信**：本地 HTTP JSON（`127.0.0.1` 随机端口 + 启动 token），理由：避开 Windows 管道编码坑、`import agent` 早期 print 不污染协议、可 curl 调试、kill 进程即全灭。
- `stream=False`：SSE 事件是"阶段/工具/文本块"粒度，**不要模拟 token 流**。

**两种运行模式（README 必须写清）：**
| 模式 | 启动 | 适合 |
|---|---|---|
| 开发模式 | 终端A：`uvicorn` 起后端 8900；终端B：`frontend/` 下 `npm run dev`（5173，proxy `/api`） | 改代码期间 |
| 日常模式 | 先 `npm run build`（产物 `frontend/dist/`），再一条命令起后端（FastAPI 托管 dist）→ 浏览器开 8900 | 日常使用 |

## 3. 架构蓝图（施工图）

### 3.1 目录结构（全部新增，落在 `agent_webui/`）

```
agent_webui/
├── backend/                     # FastAPI 网关（Python）
│   ├── main.py                  # FastAPI 实例、路由装配、CORS(仅 dev 直连时)、静态托管 dist
│   ├── api.py                   # REST 路由（sessions/chat/agent/workspace/clarify）
│   ├── sse.py                   # SSE hub（统一出口：bridge 事件 + 后端自身事件 → 前端）
│   ├── agent_proc.py            # AgentManager：spawn/kill/health bridge 子进程生命周期
│   ├── agent_client.py          # bridge 本地 HTTP 客户端（/chat /stop /clarify/answer /health）
│   ├── state.py                 # 两层状态机（进程级 + 回合级）
│   ├── sessions.py              # 会话列表/历史 API（读 working_memory/*.json）
│   ├── bridge.py                # ⚠️ AB 子进程入口：import agent 模块 → 本地 HTTP 随机端口
│   └── requirements.txt         # fastapi、uvicorn（装进项目根 venv）
├── frontend/                    # Vite + React + TS（独立 npm 工程）
│   ├── src/
│   │   ├── main.tsx / App.tsx
│   │   ├── types.ts             # API/事件类型（与后端约定，单一来源写注释）
│   │   ├── api.ts               # fetch 封装 + SSE hook
│   │   ├── store/               # 状态：agentPhase / sessions / messages / timeline（Context + reducer，够用再引库）
│   │   ├── components/          # ChatView / MessageList / TimelinePanel / Sidebar /
│   │   │                        # WorkspacePanel / PowerControl / ClarifyCard / StatusBadge
│   │   └── panels/registry.ts   # 可插拔面板注册表（加面板 = 新模块 + 注册一行）
│   ├── vite.config.ts           # dev proxy: /api,/api/events → 127.0.0.1:8900
│   ├── package.json / tsconfig.json
│   └── dist/                    # build 产物（.gitignore）
├── README.md                    # 总说明：两种模式启动/停止、架构、扩展指南、FAQ
└── webui设计说明.txt             # 便签（人工决策记录，可留空）
```

### 3.2 API 草案（FastAPI，自动文档在 `/docs`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 会话列表 |
| POST | `/api/sessions` | 新建或续聊（body `{session_id?}`）→ `{session_id}` |
| POST | `/api/chat` | 跑回合（`{session_id, message}`）→ 立即返回 `{run_id}`（异步） |
| POST | `/api/chat/stop` | 请求中断当前回合（`{run_id}`） |
| GET | `/api/events` | SSE 事件流（按 run_id/session_id 过滤，含心跳） |
| GET | `/api/workspace/status` | 工作区状态快照 |
| POST | `/api/clarify/answer` | 回答 clarify（`{ask_id, choice}`） |
| POST | `/api/agent/start` | 开机：spawn bridge → 读端口 → health → ON |
| POST | `/api/agent/stop` | 优雅关机：置中断 → 回合保存退出 → 超时自动 kill |
| POST | `/api/agent/kill` | 强制关闭：直接 kill 子进程（防卡死） |
| GET | `/api/agent/status` | `{phase: OFF/STARTING/ON/STOPPING, pid?, bridge_url?, health_at?}` |

SSE 挂载：后端 `GET /api/events` 是**唯一事件出口**；bridge 事件经 `sse.py` 转发，后端自身事件（进程级 phase）同源推送。

### 3.3 进程生命周期（AgentManager 设计）

| 动作 | 行为 |
|---|---|
| 开机 | spawn `python backend/bridge.py`（cwd=项目根，env 注入 `AETHER_BRIDGE_TOKEN` 随机串）；bridge 自选随机端口，把 `{port}` 经 stdout **首行 JSON** 回传；后端 `/health`（+token）探测通过 → ON |
| 优雅关机 | 空闲直接退；回合中 → 置中断标志，等回合在工具边界保存退出；超时（SPEC 定，如 10s）自动转 kill |
| 强制关闭 | 直接 `proc.kill()`（Windows TerminateProcess）；前端提示"已强制终止，最多丢半轮，可重开续聊" |
| 状态查询 | 轮询 + 状态机事件双通道 |

要点：
- bridge 崩溃（未收到关机指令就退出）→ 状态回 OFF，前端提示"AB 异常退出，点开机重启"。v1 不自动拉起。
- 后端退出前尽量先优雅停 bridge；README 写明"关后端前先点关机"。孤儿进程回收留扩展（零依赖，无 psutil）。
- 端口：后端 8900 固定（可配）；bridge 随机（bind 0），不撞 8899。

### 3.4 bridge.py（AB 子进程内部）

- import agent 模块前**先把 stdout 重定向到 stderr**（防 import 期 print 污染首行协议；首行 JSON 后 stdout 不再用）。
- 本地 `http.server`（ThreadingHTTPServer，bind `127.0.0.1:0`）：
  - `POST /start` → `{ok}`
  - `POST /chat {session_id, message}` → 立即返回 `{run_id}`，回合线程跑 `call_agent_with_tools`
  - `GET /events` → SSE：stage/tool_begin/tool_end/progress/clarify/done/error（token 校验）
  - `POST /stop {run_id}` → 置中断标志
  - `POST /clarify/answer {ask_id, choice}` → 唤醒等待中的 ask_user
  - `GET /health` → `{ok, phase}`
- **回合中断实现（P5 难点）**：`call_agent_with_tools` 内部靠 `KeyboardInterrupt` 保存退出；bridge 的 `/stop` 来自另一线程，**无法直接抛 KeyboardInterrupt**。先读 `task_orchestrator` 源码确认中断通道（源码有 `is_interrupted` 线索），在 bridge 层实现"中断标志 → 工具边界检查"。任何对 agent/ 或 agent_tools/ 现有文件的改动必须先书面请示主人。
- 事件 type 枚举集中一处定义（backend 与 bridge 共享约定，注释写扩展位）：
  `agent_phase` / `stage` / `tool_begin` / `tool_end` / `progress` / `text` / `clarify_request` / `done` / `error` / `heartbeat`

### 3.5 状态机（两层合成展示）

```
进程级：
OFF ──start──> STARTING ──health ok──> ON ──优雅stop──> STOPPING ──> OFF
  ▲              │(启动失败/超时)             │
  └──────────────┴──────── kill / 崩溃检测 <──┘   （任意运行态 → OFF）

回合级（仅 ON 态有意义）：
IDLE ──(用户消息)──> THINKING ──(工具调用)──> TOOL_RUNNING ──> THINKING ──(...)──> RESPONDING ──> IDLE
                       │                        │
                       └──(需确认)──> CLARIFY_WAIT ──(答复)──> 回到原状态（可取消 → IDLE）
任意回合态 ──(stop/强杀)──> INTERRUPTED ──> IDLE      任意回合态 ──(异常)──> ERROR ──> IDLE
```

- 前端合成：电源区大徽标（进程级）+ 侧栏/聊天区状态点（回合级）；非 ON+IDLE 禁用发送。
- 扩展位：将来"模式切换" = 进程级动作（优雅停 → 换 mode 参数重启 bridge）。

### 3.6 clarify 审批机制（唯一触碰 agent 侧的改动）

- **agent 工具网关新增第 13 个工具 `ask_user`**（唯一改动点）：
  - 新增 `agent_tools/ask_user.py` + `agent_tools/__init__.py` 追加 import 与 schema 注册（两行小 diff）。
  - schema：`question`（必填）、`options`（可选）、`type`（choice/multi/freeform，默认 choice）。
  - 行为：阻塞等待答复（threading.Event）。
    - bridge/WebUI 模式：问题 → bridge 事件流 → 后端 SSE → 前端 ClarifyCard → 答复 → `/api/clarify/answer` → 后端转 bridge → ask_user 返回。
    - CLI 兜底：无 bridge 回调时降级 `print + input()`（VSCode 终端照常可用）。
- **需主人批准后 AetherBreath 才能动手**（P5 先交书面改动说明：改哪些文件、如何回滚、CLI 兼容性）。
- 注册进 `TOOLS_SCHEMA` 后旧 AB 进程看不到新工具，需重启——与"开机/关机"按钮天然契合。

### 3.7 扩展点设计（面向"模式切换 / 垂直方向"愿景）

1. **后端**：bridge 启动参数预留 `mode`（v1 空 → 现有 `load_system_prompt`）；将来模式 = {skills 目录、memory 子集、工具白名单、persona 覆盖}，切换 = 带新 mode 重启 bridge。
2. **前端**：`panels/registry.ts` 面板注册表 + 组件化；加面板 = 新组件 + 注册一行。
3. **类型对齐（增强项）**：FastAPI OpenAPI → `openapi-typescript` 生成前端类型；v1 手写 `types.ts` 并注释"与 backend API 对齐"。
4. **SSE 事件**：type 枚举集中管理，预留扩展位。
5. **会话文件**：将来按模式分区（`working_memory/{mode}/`），v1 不改格式。

### 3.8 已知坑（写给 AetherBreath）

- bridge 必须跑在**项目根 venv**（含 openai 依赖），后端 fastapi/uvicorn 也装进同一 venv（一份依赖最简）；`import agent` 顶层有副作用（建 OpenAI client、打印）——import 安全，bridge 提前重定向 stdout。
- `stream=False`：LLM 响应一次性返回，SSE 事件是"阶段/工具/文本块"，不要模拟 token 流。
- 工具执行可能数分钟：SSE 心跳；回合异步；前端"等待中"视觉。
- 强杀后会话恢复：靠自动 `save_session`；重开续聊若历史里 assistant tool_calls 缺 tool 回复，参考 `ensure_tool_responses` 的补全思路（先读源码确认自动恢复路径，不凭空设计）。
- **CLI 与 WebUI 是两个独立 AB 进程**共享磁盘会话文件：同一时间只让一端操作同一会话（README 写明）。
- Windows：kill 用 `proc.kill()` 即可；输出编码统一 UTF-8；`npm run build` 路径注意（MSYS 无关，前端工程内操作）。
- **开发模式双进程**：vite proxy 已处理 SSE 转发，别在直连模式下忘了 CORS（后端 main.py 配 dev 白名单或统一走 proxy）。
- FastAPI 托管 SPA：静态目录挂载 + 未知路径 fallback `index.html`（刷新不 404）。
- 快照冻结：改 SOUL/AGENTS/MEMORY/技能需新会话；ask_user 新工具需重启 AB 进程（开机按钮天然支持）。

## 4. Vibe Coding 提示词剧本（核心交付）

【用法】按顺序逐段复制进 AetherBreath 会话（VSCode 集成终端 `python agent/agent.py`）。
每段完成 → 主人浏览器/终端实际验收通过 → 才发下一段。
每段通用结尾模板（可拼接到任意阶段末尾）：

```
【通用要求】
- 先 read_file 读 agent_webui/vibe-coding-plan.md 的对应章节和现有相关源码，再动手。
- 每步真实运行验证，禁止伪报"已完成"：起服务后用 curl 实际请求 API 并把输出贴给我看。
- 中期进度每步前后汇报（📥 / ✅ / ⚠️）。
- 代码按你的修改铁律执行（整文件生成优先、区间替换、不逐行推断）；execute_python 被拦截的操作走 execute_shell heredoc。
- 发现方案与现状冲突 → 停下报告，等指示，不擅自绕路。
- 阶段完成标准：给出【验收清单】并逐条自证（真实输出），主人实际操作通过后才算完成。
```

### P0 —— 规格与决策（只读侦察 + SPEC，不写实现）

```
【任务：为我自己开发 WebUI —— 阶段 P0：规格与决策】
你是 AetherBreath（Python CLI agent）。现在要给自己开发一个【前后端分离】的 WebUI：后端 FastAPI（Python，装进项目 venv），前端 Vite + React + TS（独立 npm 工程），采用【子进程生命周期模型】：后端是常驻网关，我（AB 本体）作为独立子进程 bridge 由后端拉起，主人可随时强制关闭我（防卡死）再重开。

完整需求与架构蓝图：先 read_file 读 agent_webui/vibe-coding-plan.md（§0-§3），再读 agent/agent.py 的 call_agent_with_tools / load_session / save_session / load_system_prompt / main()、task_orchestrator 的中断机制、agent_tools/__init__.py，以及 subagents/多agent系统/orchestrator.py 的 Web UI 部分作为参考。

本阶段目标（只做规划，不写任何 WebUI 代码）：
1. 在 agent_webui/ 下新建 SPEC.md：进程拓扑、backend/frontend 模块清单、API 草案、两层状态机、事件 type 枚举、bridge 内部流程（import 前重定向、首行端口上报、回合/中断/事件推流）、前端组件树与面板注册表设计、实施顺序——全部基于真实源码，以源码为准修正本方案推测。
2. 检查并汇报：项目 venv 是否已有 fastapi/uvicorn（无则列入安装计划，装进项目根 venv）；node/npm 版本可用性。
3. 汇报实现计划（≤200 字）。
4. 列出 3-5 个需主人拍板的决策点（例如：后端端口、/chat 同步 vs 异步+SSE、优雅停止超时秒数、ask_user 是否允许改 agent_tools/__init__.py、中断实现的最小侵入方案）。

红线：不改 agent/、agent_tools/、agent_memory/ 任何现有文件（本阶段纯新增 SPEC.md）；不装任何依赖；产出必须真实。

交付：agent_webui/SPEC.md + 实现计划 + 决策点清单。
```

### P1 —— 后端骨架（FastAPI）+ 电源链路雏形

```
【阶段 P1：FastAPI 网关骨架 + 电源开关雏形】
按 P0 的 SPEC（先重读确认没跑偏）实现：
1. 安装 fastapi + uvicorn 进项目根 venv（若已装则跳过，汇报版本）。
2. backend/main.py：FastAPI 实例 + /api/sessions（真实扫描 agent_memory/working_memory/*.json）+ 电源区 API 占位 + /api/events 空 SSE（先只推后端心跳）。打开 /docs 确认自动文档可用。
3. backend/agent_proc.py AgentManager 雏形：start() spawn【假子进程】（如 python -c 循环打印的小脚本，仅验证生命周期链路）；kill() 能杀；status() 返回 phase。
4. 静态托管暂不做（P2 前端工程起来后接）。

完成标准（真实验证）：
- uvicorn 起后端（python -m uvicorn backend.main:app --port 8900 或等价方式）；curl /api/sessions 返回真实会话 JSON；浏览器开 /docs 看到 API 列表。
- 用 curl 走一遍电源链路：POST /api/agent/start → status 变 ON（假进程）→ POST /api/agent/kill → status 回 OFF；贴出 status 变化与假进程被杀证据。
- 汇报停止服务方式。
```

### P2 —— 前端工程初始化 + 电源按钮 + proxy

```
【阶段 P2：React 前端工程 + 电源控制】
1. 在 agent_webui/frontend/ 初始化 Vite + React + TS 工程（npm create vite@latest frontend -- --template react-ts 或手动搭建）。
2. vite.config.ts：dev proxy 把 /api 与 /api/events 代理到 http://127.0.0.1:8900（SSE 走 proxy 需确认不缓冲——用 http-proxy 默认即可，验证时重点看事件是否实时到达）。
3. 页面骨架：App 布局（左 Sidebar / 中 Chat / 右下 Timeline 占位）+ 顶部 PowerControl（🟢开机 / ⏻优雅关机 / 🛑强杀按钮 + phase 徽标）+ StatusBadge 组件。
4. api.ts：封装 /api/agent/* 与 /api/sessions；SSE hook（EventSource 连 /api/events，处理断线重连）。
5. 会话列表：真实渲染 working_memory 会话（只读）。

完成标准（真实验证）：
- npm run dev 起 5173 → 浏览器操作：点开机（后端拉假进程）→ 徽标 ON → 强杀 → OFF；点开机后能列出真实会话。
- 贴出：vite 起的日志、一次 SSE 收到的真实事件、电源链路操作结果。
- 停止方法：Ctrl+C 两端（前端 npm 进程 + 后端 uvicorn）。
```

### P3 —— bridge 真机 + 真实回合 + SSE 对话

```
【阶段 P3：bridge 开机 + 真实聊天】
把假子进程换成真实 bridge（仍是独立进程，可杀）：
1. backend/bridge.py：import 项目根 agent 模块（import 前重定向 stdout→stderr），起本地 HTTP（127.0.0.1 随机端口），首行 JSON 上报端口；提供 /start /chat /events(SSE) /health（token 校验，token 经 env AETHER_BRIDGE_TOKEN 注入）。
2. agent_proc.py：spawn bridge → 读首行拿端口 → /health → ON。agent_client.py：/chat /stop /clarify/answer /health 封装。
3. /api/chat 异步化：{run_id} 立即返回；bridge 回合事件（agent_phase/stage/progress/text/done/error）经 sse.py 转发前端 SSE。
4. 前端：ON+IDLE 才能发送；发消息 → 显示最终回复；回合中"处理中"；历史消息从 working_memory JSON 还原（只渲染 user/assistant，工具消息 P4 处理）。

完成标准（真实验证）：
- 浏览器开机 → 发"你好" → 收到真实回复；贴该回合 SSE 原始事件序列（至少含 done）。
- 用 CLI 开过的同一 session_id 在 WebUI 续聊，确认上下文接得上。
- 浏览器强杀 → kill 成功、徽标 OFF、页面正常；再开机 → 同会话续聊正常。
```

### P4 —— 工具时间线 + 工作区面板

```
【阶段 P4：工具调用实时可视化 + 工作区状态】
1. bridge 侧：每次工具调用（名称/参数摘要/返回摘要/耗时/是否并行批）作为 tool_begin/tool_end 事件推送（参照 agent.py 中 orchestrator.execute 前后的真实数据点；只新增上报，不改 agent 现有逻辑，必须改则先请示）。
2. 前端 TimelinePanel：🔧 工具名 → 参数（截断）→ ↩️ 返回（截断/错误标红）→ 耗时；并行批次分组。历史恢复：assistant tool_calls + 后续 tool 消息配对渲染（参考 ensure_tool_responses 理解配对）。
3. 后端 GET /api/workspace/status：agent_workspace/ 子目录（名称/修改时间/文件数/最近 N 文件）+ 会话统计。
4. 前端 WorkspacePanel（Sidebar 或独立 tab）：目录卡片展开看文件，定时刷新。

完成标准（真实验证）：
- 发"用 python 算 17×23 并把结果写到 agent_workspace/webui_timeline_test.txt" → 浏览器看到时间线实时流出；贴 tool_begin/tool_end 真实事件 JSON。
- /api/workspace/status 真实 JSON 与磁盘一致。
```

### P5 —— 两层状态机 + 优雅停止/强杀完善 + clarify（唯一触碰 agent 侧的阶段）

```
【阶段 P5：状态机 + 生命周期完善 + clarify 审批机制】
本阶段含 agent 侧唯一改动点（agent_tools/）—— 先提交书面改动说明（改哪些文件、加什么、如何回滚、CLI 兼容性），经主人批准后再动手。禁止未经批准直接改 agent_tools/__init__.py。

1. backend/state.py + 前端：按 vibe-coding-plan.md §3.5 两层状态机实现；非 ON+IDLE 禁用发送；电源徽标实时反映；状态迁移事件驱动 UI。
2. /api/agent/stop（优雅）与 kill（强制）对齐 §3.3：stop 先置中断 → 回合保存退出 → 超时(SPEC 秒数)自动 kill。
3. 中断机制：读 task_orchestrator 源码确认中断通道，实现"stop → 回合在工具边界保存退出"，禁止生造并行中断；需最小侵入改 agent 现有文件则先书面请示。
4. clarify：新增 agent_tools/ask_user.py（schema: question/options/type=choice|multi|freeform）注册进 __init__.py；bridge 内 ask_user 挂起 → 问题推前端 ClarifyCard → 答复 → 后端转 bridge → ask_user 返回；CLI 无回调降级 input()。
5. 前端 ClarifyCard：选项按钮 / 多选 / 自由输入框。

完成标准（真实验证）：
- 优雅停/强杀各测一次：长工具任务中点停止 → 会话 interrupted 保存、状态回 OFF/IDLE；再开机同会话续聊上下文连续。
- WebUI 发"你先问我一个问题再继续" → 问题卡片 → 回答 → agent 继续完成回合。
- CLI 兜底：VSCode 终端重启 agent 后触发 ask_user → 走 input() 提问，原 CLI 不受影响。
```

### P6 —— 多会话完善 + 刷新恢复 + 日常模式

```
【阶段 P6：多会话完善与两种运行模式】
1. 会话列表：新建/切换/续聊/删除（删除前 confirm；删除 = 移备份，遵循 trash 优先）。
2. 会话信息：首条摘要、消息数、最后活动、状态。
3. 互通验证：CLI 聊两句 exit → WebUI 同 session_id 续聊连续；反向亦然。
4. 刷新不丢状态：SSE 断线重连；刷新后当前会话、消息、电源状态正确还原（含后端仍 ON 时前端重连直接显示 ON）。
5. 日常模式：npm run build → backend 静态托管 frontend/dist（SPA fallback index.html）→ 单命令起后端 → 浏览器 8900 完整可用（电源/聊天/时间线全通）。

完成标准：浏览器完整生命周期走一遍（开机→新建→聊→切走→回来→强杀→重开续聊→删除）；日常模式与开发模式都验证通过；贴关键操作结果。
```

### P7 —— 加固、文档与 GitHub 友好自检

```
【阶段 P7：加固与交付】
1. agent_webui/README.md：启动/停止（开发模式双命令、日常模式 build+单命令）、架构图（三进程拓扑）、API 一览（附 /docs）、扩展指南（加面板/事件类型/未来 mode）、CLI 共存注意、强杀恢复说明、后端依赖安装命令（venv + pip install -r backend/requirements.txt）、前端依赖安装命令（npm install）。
2. GitHub 友好自检（逐条自查汇报）：
   - 无硬编码绝对路径；新增环境变量 AETHER_* 前缀；无"Hermes"品牌字样；
   - backend/requirements.txt 只含 fastapi/uvicorn（及必要的 pydantic 传递依赖不手写）；
   - frontend/package.json 依赖合理；node_modules/、dist/ 已 gitignore；后端 __pycache__ 已忽略；
   - 中文 UTF-8 无乱码；无密钥/token 泄露（bridge token 仅内存与启动环境，不落盘不落日志）。
3. 边界与错误处理：非法 session_id、空消息、bridge 启动失败/超时、SSE 断线、端口占用提示、强杀后立刻重开竞态（确认端口已释放）。
4. 全流程回归：日常模式下完整走一遍（多工具任务 + clarify + 优雅停 + 强杀重启续聊），贴最终验收记录。

交付：README.md + 自检报告 + 回归验收记录。完成后汇报"WebUI 已可日常使用"，给出一句话启动命令。
```

## 5. 给主人的使用流程

1. 先跑 **P0**，与 AetherBreath 过一遍 SPEC 与决策点（含依赖安装批准、ask_user 改动批准、中断方案）。
2. 逐阶段 P1→P7，**每阶段实际验收**后再发下一段。
3. 全部通过后日常使用：`npm run build`（改动前端后）→ 一条命令起后端 → 浏览器 `http://127.0.0.1:8900` → 点「🟢 开机」→ 聊天；卡死点「🛑 强制关闭」→ 再开机续聊。停止：先点关机/强杀 → Ctrl+C 停后端。
4. 开发新功能：后端开 uvicorn + 前端 `npm run dev`（热更新）；按 §3.7 扩展点加面板/API/事件。
5. 将来大特性（模式切换）可再开一轮 vibe coding，复用 P0→P7 流程（模式 = 带 mode 参数重启 bridge）。

## 6. 备注

- 本方案中的函数名/行号来自 2026-09-07 实查；若 AetherBreath 读到源码与方案有出入，以源码为准并在 SPEC 中标注差异。
- v0.3 已获主人批准的新依赖：后端 `fastapi` + `uvicorn`（装进项目根 venv）；前端 Vite/React/TS 脚手架依赖。其它新增依赖仍需先请示。
