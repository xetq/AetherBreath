// 后端 REST 封装。同源 /api（开发模式由 vite proxy 转发到网关）。
import type {
  AgentStatus, ApprovalRule, DeleteSessionResp, HistoryResp, McpStationsResp, SearchResp, SessionsResp,
  ToolsResp, WsEvent, WorkspaceStatus,
} from './types'

const BASE = '/api'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch (e) {
    throw new ApiError(0, `网关不可达：${(e as Error).message}（后端是否已启动？）`)
  }
  const text = await res.text()
  let data: unknown = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = { raw: text } }
  }
  if (!res.ok) {
    const d = data as { detail?: string; error?: string } | null
    throw new ApiError(res.status, d?.detail || d?.error || `HTTP ${res.status}`)
  }
  return data as T
}

const post = <T,>(path: string, body?: unknown) =>
  call<T>(path, { method: 'POST', body: body === undefined ? '{}' : JSON.stringify(body) })

/** 电源类接口的返回：状态快照 + 动作结果 */
export type PowerResult = Partial<AgentStatus> & {
  ok?: boolean; error?: string; reason?: string; killed?: boolean
  already?: boolean; already_off?: boolean; model?: string; tools?: number
  injected?: string[]; mode?: string
}

export const api = {
  // ---- 会话 ----
  sessions: () => call<SessionsResp>('/sessions'),
  history: (sid: string) => call<HistoryResp>(`/sessions/${encodeURIComponent(sid)}/history`),
  createSession: (session_id?: string, prefix?: string, title?: string) =>
    post<{ session_id: string; created: boolean; resumed: boolean; title?: string }>(
      '/sessions', { session_id, prefix, title }),
  renameSession: (sid: string, title: string) =>
    post<{ ok: boolean; session_id: string; title: string }>(
      `/sessions/${encodeURIComponent(sid)}/title`, { title }),
  searchSessions: (q: string, scope = 'all', status = '') =>
    call<SearchResp>(`/sessions/search?q=${encodeURIComponent(q)}&scope=${scope}&status=${status}`),
  deleteSession: (sid: string) => call<DeleteSessionResp>(
    `/sessions/${encodeURIComponent(sid)}`, { method: 'DELETE' }),

  // ---- 对话 ----
  chat: (session_id: string, message: string) =>
    post<{ ok: boolean; run_id: string; session_id: string }>('/chat', { session_id, message }),
  /** 中期交互：回合运行中追加「用户交代」。**不起新回合**，投给正在跑的那个，
   *  由 AB 在下一批工具返回时注入。没有活动回合 / 回合或会话不符 → 409，回合已结束 → 404。 */
  midTurn: (session_id: string, text: string, run_id?: string | null) =>
    post<{ ok: boolean; item_id?: string; pending?: number; run_id?: string;
           session_id?: string; error?: string }>('/chat/mid_turn', { session_id, text, run_id }),
  stopChat: (run_id?: string | null) => post<{ ok: boolean; stopped?: boolean; note?: string | null; reason?: string }>(
    '/chat/stop', { run_id }),
  clarifyAnswer: (ask_id: string, answer: string) =>
    post<{ ok: boolean; batch_live?: number; remaining?: number }>('/clarify/answer', { ask_id, answer }),
  // 审批裁决：统一提交「勾选项 + 批准范围」。未勾的即视为拒绝（整批不放行）。
  // 审批已结束（超时/回合终止）时后端返 410，前端必须如实告知答复未被采纳。
  approvalAnswer: (ask_id: string, approved: string[], scope: string, stop = false,
                   note = '') =>
    post<{ ok: boolean; approved?: number }>(
      '/approval/answer', { ask_id, approved, scope, stop, note }),

  // ---- 电源 ----
  // 免审规则：查询与撤销都是整包 body，网关不做字段白名单
  /** 服务端当前真挂着的审批 / 提问：刷新、切会话、重开页面后靠它找回卡片。
   *  这两个端点是 GET —— 曾经这里误用 post()，于是每次恢复请求都被后端 405 掉，
   *  而调用点的 catch 把它吞了。结果是"恢复通道"自始至终一次都没生效过，
   *  四个"卡片会消失"的现象当然一个也没修好。方法写错就整条通道失效，
   *  所以网关侧现在同时收 GET/POST，两边都不许再靠单点正确性。 */
  pendingApprovals: () => call<{ ok: boolean; cards: WsEvent[]; error?: string }>('/approval/pending'),
  pendingClarify: () => call<{ ok: boolean; cards: WsEvent[]; error?: string }>('/clarify/pending'),
  approvalRules: (session_id = '') =>
    post<{ ok: boolean; rules?: ApprovalRule[]; error?: string }>(
      '/approval/rules', { session_id }),
  approvalRevoke: (path = '', scope = '', session_id = '') =>
    post<{ ok: boolean; removed?: number; rules?: ApprovalRule[]; error?: string }>(
      '/approval/rules/revoke', { path, scope, session_id }),

  agentStart: (mode = '') => post<PowerResult>('/agent/start', { mode }),
  agentStop: () => post<PowerResult>('/agent/stop'),
  agentKill: () => post<PowerResult>('/agent/kill'),
  agentStatus: () => call<AgentStatus>('/agent/status'),
  agentTools: () => call<ToolsResp>('/agent/tools'),
  agentLog: (lines = 80) => call<{ ok: boolean; tail: string }>(`/agent/log?lines=${lines}`),

  // ---- MCP 服务站（v2）----
  // 常驻快照：只读 station 文件夹 + 运行时表，不起进程，**不需要先发消息**。
  mcpStations: () => call<McpStationsResp>('/mcp/stations'),

  // ---- 工作区 ----
  workspace: () => call<WorkspaceStatus>('/workspace/status'),
  workspaceDir: (p: string) => call<Record<string, unknown>>(`/workspace/dir?path=${encodeURIComponent(p)}`),

  health: () => call<Record<string, unknown>>('/health'),
}

export const EVENTS = [
  'agent_phase', 'turn_phase', 'stage', 'tool_begin', 'tool_end',
  'progress', 'text', 'clarify_request', 'clarify_resolved', 'approval_request', 'approval_expired',
  'approval_resolved', 'approval_batch', 'approval_batch_resolved',
  'approval_resolved',
  'mid_turn',
  'done', 'error', 'heartbeat',
  'pipeline', 'usage',
] as const

/** 打开 SSE；返回 close()。onEvent 收到解析后的事件。 */
export function openEvents(
  since: number,
  onEvent: (e: WsEvent) => void,
  onState: (s: 'open' | 'error' | 'closed') => void,
  sessionId?: string | null,
): () => void {
  const q = new URLSearchParams({ since: String(since) })
  if (sessionId) q.set('session_id', sessionId)
  const es = new EventSource(`${BASE}/events?${q.toString()}`)
  es.onopen = () => onState('open')
  es.onerror = () => onState(es.readyState === EventSource.CLOSED ? 'closed' : 'error')
  for (const t of EVENTS) {
    es.addEventListener(t, (ev: MessageEvent) => {
      try { onEvent(JSON.parse(ev.data) as WsEvent) } catch { /* 忽略坏帧 */ }
    })
  }
  return () => es.close()
}
