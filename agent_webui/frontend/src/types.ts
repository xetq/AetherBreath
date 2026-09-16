// 与 backend 对齐的类型（单一来源：agent_webui/backend/{events,api,sessions,workspace}.py）
// 改后端字段时同步这里；增强的类型生成方案见 README「扩展指南」。

export type AgentPhase = 'OFF' | 'STARTING' | 'ON' | 'STOPPING'

export type TurnPhase =
  | 'IDLE' | 'THINKING' | 'TOOL_RUNNING' | 'RESPONDING'
  | 'CLARIFY_WAIT' | 'AUDIT_WAIT' | 'INTERRUPTED' | 'ERROR'

export interface AgentStatus {
  phase: AgentPhase
  turn_phase: TurnPhase
  pid: number | null
  bridge_url: string | null
  session_id: string | null
  run_id: string | null
  last_error: string | null
  health_at: string | null
  can_send: boolean
  busy: boolean
  alive?: boolean
  hello?: PoolHello
  mode?: string
  injected?: string[]
  subscribers?: number
  graceful_timeout?: number
  clarify_timeout?: number
  gateway?: { host: string; port: number; pid: number }
}

export interface SessionMeta {
  session_id: string
  /** 上下文视图元数据（agent/context_manager.py 落盘；未压缩过 = undefined/{}） */
  ctx?: CtxMeta
  title?: string
  display?: string
  file?: string
  message_count: number
  user_turns?: number
  tool_calls?: number
  status: 'active' | 'complete' | 'interrupted' | 'unknown' | string
  status_label?: string
  has_snapshot?: boolean
  summary: string
  last_activity?: string
  saved_at?: string
  size?: number
  unreadable?: boolean
}

/** 上下文管理器：视图元数据（backend/sessions.py:_ctx_meta 读出） */
export interface CtxMeta {
  version?: number
  source_len?: number
  rounds?: number
  saved_ratio?: number
  est_before?: number
  est_after?: number
  kept_rounds?: number
  compactions?: number
  last_compact_at?: string
  /** 原文占用估算（本地口径 1 字符≈0.5 token）：没压缩过、也没发过消息时的圆圈兜底 */
  est_original?: number
}

// ---------- 会话搜索 ----------
export interface SearchHit {
  field: 'title' | 'content'
  role?: string
  at?: number
  snippet: string
}

export interface SearchMatch {
  session_id: string
  title: string
  display?: string
  summary?: string
  status?: string
  status_label?: string
  message_count?: number
  last_activity?: string
  title_matched: boolean
  hit_count: number
  hits: SearchHit[]
}

export interface SearchResp {
  ok: boolean
  query: string
  scope?: string
  status?: string
  scanned: number
  matched: number
  matches: SearchMatch[]
}

export interface SessionsResp {
  sessions: SessionMeta[]
  total: number
  dir: string
}

/** 删除会话的返回：血缘三件套（原文 + 视图 + 事件流）一起移入 .trash */
export interface DeleteSessionResp {
  ok: boolean
  session_id?: string
  moved_to: string
  moved?: { kind: 'session' | 'view' | 'view_events' | string; to: string }[]
  warnings?: string[]
}

export interface ToolCallView {
  id: string
  name: string
  args: string
  result: string | null
  failed: boolean
  pending: boolean
}

export interface HistoryMessage {
  role: 'user' | 'assistant'
  content: string
  tool_calls?: ToolCallView[]
  timeline?: TimelineEntry[]
  /** 仅前端视图态：这条是「用户交代」（历史靠前缀识别，实时靠 mid_turn 事件）。
   *  pending=已投递待注入 / delivered=已随工具返回注入 / dropped=回合结束未送达 */
  mid?: 'pending' | 'delivered' | 'dropped'
  /** 投递回执 id：injected/dropped 事件按它精确更新状态（乱序到达也不会错配） */
  mid_id?: string
}

export interface HistoryResp {
  session_id: string
  exists: boolean
  status?: string
  status_label?: string
  message_count?: number
  saved_at?: string
  has_snapshot?: boolean
  snapshot_head?: string | null
  messages: HistoryMessage[]
}

// ---------- 时间线 ----------
export interface TimelineEntry {
  key: string
  tool: string
  args: string
  result: string | null
  ok: boolean
  error: string | null
  elapsed: number | null
  call_id: string
  thread?: string
  run_id?: string
  session_id?: string | null
  source: 'live' | 'history'
  at?: number
  status: 'running' | 'done'
}

// ---------- SSE 事件 ----------
export type WsEventType =
  | 'agent_phase' | 'turn_phase' | 'stage' | 'tool_begin' | 'tool_end'
  | 'progress' | 'text' | 'clarify_request' | 'clarify_resolved' | 'approval_request' | 'approval_expired'
  | 'approval_resolved' | 'approval_batch' | 'approval_batch_resolved'
  | 'mid_turn'
  | 'done' | 'error' | 'heartbeat'
  | 'pipeline' | 'usage'

export interface WsEvent {
  type: WsEventType
  hub_seq?: number
  /** SSE 回放帧（刷新/重连时补历史）。只补时间线，不许改写"此刻谁在等人"。 */
  replayed?: boolean
  ts?: number | string
  seq?: number
  run_id?: string | null
  session_id?: string | null
  phase?: TurnPhase | AgentPhase
  turn_phase?: TurnPhase | AgentPhase
  reason?: string
  ok?: boolean
  timeout?: number
  // 审批卡：引擎决定是否收集主人的补充说明（逐字段搬运见 appStore reducer）
  accepts_note?: boolean
  note_hint?: string
  already?: boolean
  killed?: boolean
  stage?: string
  tool?: string
  args?: string
  result?: string | null
  error?: string | null
  elapsed?: number
  call_id?: string
  thread?: string
  index?: number
  text?: string
  content?: string
  message?: string
  iterations?: number
  tool_calls?: number
  interrupted?: boolean
  can_send?: boolean
  busy?: boolean
  pid?: number | null
  bridge_url?: string | null
  // clarify
  ask_id?: string
  question?: string
  title?: string
  kind?: string
  flag?: string
  risk?: number
  paths?: string[]
  lines?: string[]
  source_code?: string
  channel?: string
  batch_id?: string
  batch_size?: number
  batch_live?: number
  items?: unknown[]
  scopes?: unknown[]
  intent?: string
  notes?: string[]
  total?: number
  critical?: boolean
  // 恢复通道：服务端算出的剩余秒数 + 「这是找回来的卡」标记。
  // WsEvent 没有索引签名，新字段必须在此声明 —— 否则又是静默丢值那一类病。
  remaining?: number
  restored?: boolean
  user_request?: string
  options?: string[]
  mode?: string
  model?: string
  history_count?: number
  snapshot?: string
  tools?: string[] | number
  injected?: string[]
  error_detail?: string
  // 编排器管道状态机
  tc_id?: string
  status?: string
  live?: number
  layer?: number | null
  size?: number
  pool?: string
  // usage（token 计量，bridge 从 API 返回值直接读）
  call?: UsageNode
  session?: UsageNode
  global?: UsageNode
  // 中期交互（mid_turn 事件）：
  //   mid = accepted（已投递）| injected（已随工具返回注入）| dropped（回合结束未送达）
  mid?: string
  item_id?: string
  ids?: string[]
  texts?: string[]
  count?: number
  pending?: number
}

// ---------- token 计量 ----------
export interface UsageNode {
  calls: number; prompt: number; completion: number; reasoning: number; total: number
  /** 命中前缀缓存的输入 token（厂商不返回则恒为 0） */
  cached?: number
  /** 最近一次请求的输入 token = 当前上下文占用（压缩后会回落） */
  last_prompt?: number
}
export interface UsageSnapshot {
  /** 只表示本会话累计；本会话尚无计量时为 null（不再用全局数冒充） */
  scope: 'session'
  node: UsageNode
  session: UsageNode | null
  global: UsageNode
  model?: string | null
  at: number
}

// ---------- 编排器管道 ----------
export interface PipeView {
  tool: string
  status: 'pending' | 'running' | 'idle' | 'failed' | 'cancelled' | string
  layer: number | null
  at: number
  thread?: string | null
  elapsed?: number | null
}

// bridge 首行协议在网关 status.hello 里整包回显
export interface PoolHello {
  pools?: PoolCaps
  port?: number
  pid?: number
  mode?: string
  model?: string
  tools?: number
  injected?: string[]
  usage_watch?: string
  /** 上下文管理器配置（窗口/阈值），前端画占比圆圈用；由 bridge 从 CM_PARAMS 原样下发 */
  ctx?: CtxConfig
  /** 计量账本：上次退出前的每会话用量（bridge 落盘，重启后原样带回来） */
  usage_ledger?: { total?: UsageNode; by_sid?: Record<string, UsageNode> }
}

export interface CtxConfig {
  enabled?: boolean
  window?: number
  threshold?: number
  keep_recent_rounds?: number
}

// 编排器池容量（bridge 在 hello 里上报；懒起线程推不出来，必须显式给）
export interface PoolCap { workers: number; prefix?: string; started?: number }
export interface PoolCaps { parallel?: PoolCap; serial?: PoolCap }

// ---------- 审批（系统盘/高危门禁；与 clarify 提问是不同语义，故不同通道）----------
export interface ApprovalOption {
  key: string            // A~F
  label: string
  scope: 'once' | 'session' | 'persistent' | string
  emoji: string          // emoji 表达"授权范围有多宽"
  confirm?: boolean      // 需二次确认（永久允许）
  stop?: boolean         // 会中止整个回合
}

export interface ApprovalItem {
  ask_id: string
  kind: string
  risk: number
  title?: string
  intent?: string
  reason?: string
  paths?: string[]
  notes?: string[]
  critical?: boolean
  source_code?: string
}

export interface ApprovalScope { key: string; label: string; emoji: string }

/** 一条已签发的免审规则。撤销必须同时改内存与磁盘，见 ContextPanel 的提示。 */
export interface ApprovalRule {
  path: string
  kind?: string
  scope: string          // persistent | session
  session_id?: string
  ask_id?: string
  ts?: number
  drive_root?: string
}

export interface ApprovalRequest {
  ask_id: string         // 回投时的 ask_id / batch_id
  batch: boolean         // true=合并卡；单条按 1 项的批处理，共用一套渲染
  items: ApprovalItem[]
  scopes: ApprovalScope[]
  total: number
  timeout?: number
  session_id?: string | null
  born?: number
  user_request?: string
  // 引擎决定是否收集主人的补充说明；缺字段（旧 bridge）就不显示输入框
  accepts_note?: boolean
  note_hint?: string
  /** 从服务端恢复回来的卡：倒计时按 remaining 续算，不许重置成满格 */
  remaining?: number
  restored?: boolean
}

export interface ClarifyRequest {
  ask_id: string
  question: string
  options: string[]
  kind: 'choice' | 'multi' | 'freeform'
  run_id?: string | null
  session_id?: string | null
  timeout?: number
  answered?: string | null
  remaining?: number
  restored?: boolean
  /** 到达时刻（恢复的卡会被折算成"过去的时刻"，倒计时才连得上） */
  born?: number
  /** 同批一共几问 / 此刻还剩几个待答 —— ask_user 允许一条消息里并行问多个独立问题，
   *  这些卡共享一条等待窗口（有人作答就往后续）。 */
  batch_size?: number
  batch_live?: number
}

// ---------- 工作区 ----------
export interface WorkspaceFile { name: string; mtime: string; size: number }
export interface WorkspaceDir {
  name: string
  mtime: string
  file_count: number
  size: number
  files: WorkspaceFile[]
}
export interface WorkspaceStatus {
  workspace: string
  exists: boolean
  task_dirs: WorkspaceDir[]
  task_dir_count: number
  recent_files: { path: string; mtime: string; size: number }[]
  sessions: {
    dir: string; count: number; by_status: Record<string, number>
    latest: { session_id: string; mtime: string } | null; tmp_files?: number
  }
  skills?: { dir: string; count: number; registry: string | null }
  loose_files?: { name: string; mtime: string }[]
  generated_at: string
}

export interface ToolsResp {
  ok: boolean
  tools?: string[]
  injected?: string[]
  error?: string
}

/** MCP 服务站（v2）：一个 station = 一个 server 文件夹（agent_MCP/<name>/） */
export interface McpStation {
  name: string
  description: string
  /** hand = 主人手写（机器不改）；auto = AB 自己集成的 */
  origin: 'hand' | 'auto' | string
  enabled: boolean
  usable: boolean
  tools: number
  problems: string[]
  alive: boolean          // 进程是否在跑（懒启动：没起过 = false，属正常）
  calls: number           // 累计调用次数（来自 agent 侧快照；agent 没跑过 = 0）
  missing_env: string[]
  last_tool: string | null
  last_call_at: string | null
  last_call_ok: boolean | null
  server_version: string
}

export interface McpStationsResp {
  ok: boolean
  enabled?: boolean       // 总闸（config.yaml 的 mcp.enabled）
  stations_dir?: string
  registry?: string
  count?: number
  online?: number
  on_switch?: number
  total_tools?: number
  issues?: string[]
  stations?: McpStation[]
  /** agent 侧运行时快照的时间；null/缺省 = agent 还没跑过 → 运行态未知 */
  snapshot_at?: string | null
  /** 写快照的那个 agent 进程 PID */
  runner_pid?: number | null
  error?: string
}
