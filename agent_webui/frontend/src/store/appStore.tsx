// 全局状态：Context + useReducer（够用，不引额外状态库）
// SSE 事件是唯一的状态来源，reducer 必须对乱序/重复帧幂等。
import React, { createContext, Dispatch, useContext, useEffect, useMemo, useReducer } from 'react'
import { loadUsage, saveUsage } from '../lib/persist'
import type {
  AgentStatus, ApprovalItem, ApprovalRequest, ApprovalScope, ClarifyRequest, HistoryMessage, PipeView, SessionMeta, TimelineEntry, UsageNode,
  UsageSnapshot, WsEvent,
} from '../types'

export interface Toast { id: number; kind: 'info' | 'ok' | 'warn' | 'err'; msg: string }

export interface State {
  agent: AgentStatus
  sessions: SessionMeta[]
  current: string | null
  messages: HistoryMessage[]
  timeline: TimelineEntry[]
  progress: { run_id?: string; text: string; at: number; sid?: string | null }[]
  streaming: string | null
  /** 正在显示的这个回合属于哪个会话。
   *  busy/turn_phase 是【进程级】事实，而界面是【会话级】视图 —— 不记归属
   *  就会出现"切到别的会话，上一会话的思考中卡片还挂在下面"的串台。 */
  turnOwner: string | null
  turnError: string | null
  /** 提问队列：**同批并行提问会同时挂多张卡**（ask_user 允许一条消息里问多个独立问题）。
   *  单槽时代三张卡会互相覆盖 —— 屏幕上只看得见最后一张，答完即消失，其余的还在
   *  服务端干等（这正是"问三个问题却只能答一个"的成因）。 */
  clarifies: ClarifyRequest[]
  /** 提问窗口的**共享剩余秒数** + 它到达本地的时刻。多卡共享一条 deadline，有人作答
   *  会续窗 —— 倒计时必须统一读它，否则答完一张后其余卡还按各自的老出生时刻倒数，
   *  屏幕上会显示一个比真实值小的数字（主人以为来不及了，其实时间才刚续上）。 */
  clarifyWindow: { left: number; at: number } | null
  /** 审批队列：当前主循环串行审批，同时最多 1 条挂起；用队列是为将来的
   *  执行层第二道闸门预留——那时并发请求若挤在单槽里会互相覆盖。 */
  approvals: ApprovalRequest[]
  sse: 'connecting' | 'open' | 'error' | 'closed'
  /** 编排器管道实时表（tc_id → 状态）；done 后统一转空闲灰 */
  pipes: Record<string, PipeView>
  pipeSid: string | null
  toasts: Toast[]
  lastSeq: number
  /** token 概况：按会话分桶常驻（bridge 从 API 返回值读；只覆盖 WebUI 回合，不含 CLI） */
  usageBySid: Record<string, UsageNode>
  /** 每个会话最近一次上报的模型名（切会话时要跟着换，不能沿用上一个是哪个模型） */
  modelBySid: Record<string, string>
  usage: UsageSnapshot | null
}

export const initialAgent: AgentStatus = {
  phase: 'OFF', turn_phase: 'IDLE', pid: null, bridge_url: null, session_id: null,
  run_id: null, last_error: null, health_at: null, can_send: false, busy: false,
}

export const initialState: State = {
  agent: initialAgent,
  sessions: [],
  current: null,
  messages: [],
  timeline: [],
  progress: [],
  streaming: null,
  turnOwner: null,
  turnError: null,
  clarifies: [],
  clarifyWindow: null,
  approvals: [],
  sse: 'connecting',
  pipes: {},
  pipeSid: null,
  toasts: [],
  lastSeq: 0,
  usageBySid: {},
  modelBySid: {},
  usage: null,
}

export type Action =
  | { type: 'set_agent'; agent: Partial<AgentStatus> }
  | { type: 'set_sessions'; sessions: SessionMeta[] }
  | { type: 'set_current'; sid: string | null }
  | { type: 'set_messages'; messages: HistoryMessage[]; timeline: TimelineEntry[] }
  | { type: 'append_user'; text: string }
  | { type: 'append_mid_turn'; text: string; itemId: string }
  | { type: 'set_sse'; sse: State['sse'] }
  | { type: 'toast'; kind: Toast['kind']; msg: string }
  | { type: 'drop_toast'; id: number }
  | { type: 'resolve_clarify'; ask_id: string; remaining?: number }
  | { type: 'resolve_approval'; ask_id: string }
  | { type: 'clear_approvals' }
  | { type: 'prune_gates'; ids: string[]; before: number;
      approvals?: boolean; clarify?: boolean }
  | { type: 'reset_timeline' }
  | { type: 'reset_usage'; seq?: number }
  | { type: 'event'; evt: WsEvent }

const DEFAULT_SCOPES: ApprovalScope[] = [
  { key: 'once', label: '仅批准本次', emoji: '🟢' },
  { key: 'session', label: '本会话允许这些路径', emoji: '🟡' },
  { key: 'persistent', label: '永久允许这些路径', emoji: '⚪' },
]

let toastSeq = 1
const now = () => Date.now() / 1000

function toast(state: State, kind: Toast['kind'], msg: string): State {
  const t: Toast = { id: toastSeq++, kind, msg }
  return { ...state, toasts: [...state.toasts.slice(-4), t] }
}

/** 历史消息 -> 时间线条目（与实时事件复用同一渲染器） */
export function timelineFromHistory(messages: HistoryMessage[]): TimelineEntry[] {
  const out: TimelineEntry[] = []
  messages.forEach((m, mi) => {
    ;(m.timeline || []).forEach((t, ti) => {
      out.push({ ...t, key: `h${mi}-${t.call_id || ti}`, status: 'done', source: 'history' })
    })
  })
  return out
}

/** 回合收尾后所有管道转空闲：满足"关闭或运行完成后呈灰色空闲态"的语义 */
function idleAll(pipes: Record<string, PipeView>): Record<string, PipeView> {
  const out: Record<string, PipeView> = {}
  for (const [k, v] of Object.entries(pipes)) {
    out[k] = v.status === 'failed' || v.status === 'cancelled' ? v : { ...v, status: 'idle' }
  }
  return out
}

function onEvent(state: State, evt: WsEvent): State {
  if (typeof evt.hub_seq === 'number' && evt.hub_seq <= state.lastSeq) return state
  const lastSeq = typeof evt.hub_seq === 'number' ? evt.hub_seq : state.lastSeq
  // 带 session_id 的事件会更新归属；不带的（agent_phase 等进程级事件）沿用上一次
  const owner = typeof evt.session_id === 'string' && evt.session_id ? evt.session_id : state.turnOwner
  const base: State = { ...state, lastSeq, turnOwner: owner }

  switch (evt.type) {
    case 'agent_phase': {
      const agent: AgentStatus = {
        ...base.agent,
        phase: (evt.phase as AgentStatus['phase']) || base.agent.phase,
        turn_phase: (evt.turn_phase as AgentStatus['turn_phase']) || base.agent.turn_phase,
        pid: evt.pid !== undefined ? evt.pid : base.agent.pid,
        bridge_url: evt.bridge_url !== undefined ? evt.bridge_url : base.agent.bridge_url,
        run_id: evt.run_id !== undefined ? evt.run_id : base.agent.run_id,
        can_send: evt.can_send ?? base.agent.can_send,
        busy: evt.busy ?? base.agent.busy,
      }
      let next: State = { ...base, agent }
      if (evt.reason && evt.reason !== 'snapshot' && evt.error) {
        next = toast(next, 'err', `进程状态：${evt.error}`.slice(0, 240))
      }
      return next
    }
    case 'turn_phase': {
      const agent = {
        ...base.agent,
        turn_phase: evt.phase as AgentStatus['turn_phase'],
        can_send: evt.can_send ?? base.agent.can_send,
        busy: evt.busy ?? base.agent.busy,
      }
      return { ...base, agent }
    }
    case 'stage': {
      const agent = { ...base.agent, turn_phase: 'THINKING' as const }
      return { ...base, agent }
    }
    case 'tool_begin': {
      const e: TimelineEntry = {
        key: `${evt.run_id || 'r'}-${evt.call_id || now()}`,
        tool: evt.tool || '?', args: evt.args || '', result: null, ok: true,
        error: null, elapsed: null, call_id: evt.call_id || '', thread: evt.thread,
        run_id: evt.run_id || undefined, session_id: evt.session_id ?? null,
        source: 'live', at: evt.ts as number, status: 'running',
      }
      const agent = { ...base.agent, busy: true, can_send: false, turn_phase: 'TOOL_RUNNING' as const }
      return { ...base, agent, timeline: [...base.timeline, e] }
    }
    case 'tool_end': {
      const idx = base.timeline.findIndex((t) => t.call_id === evt.call_id && t.status === 'running')
      const merged: TimelineEntry = {
        key: idx >= 0 ? base.timeline[idx].key : `${evt.run_id || 'r'}-${evt.call_id || now()}`,
        tool: evt.tool || '?', args: base.timeline[idx]?.args || evt.args || '',
        result: evt.result ?? null, ok: evt.ok !== false, error: evt.error || null,
        elapsed: evt.elapsed ?? null, call_id: evt.call_id || '', thread: evt.thread,
        run_id: evt.run_id || undefined, session_id: evt.session_id ?? base.timeline[idx]?.session_id ?? null,
        source: 'live', at: base.timeline[idx]?.at ?? (evt.ts as number), status: 'done',
      }
      const timeline = idx >= 0
        ? base.timeline.map((t, i) => (i === idx ? merged : t))
        : [...base.timeline, merged]
      const agent = { ...base.agent, turn_phase: 'THINKING' as const }
      return { ...base, timeline, agent }
    }
    case 'progress': {
      if (!evt.text) return base
      const p = { run_id: evt.run_id || undefined, text: evt.text, at: now(), sid: evt.session_id ?? null }
      return { ...base, progress: [...base.progress.slice(-59), p] }
    }
    case 'text':
      return { ...base, streaming: evt.text || '' }
    case 'approval_request':
    case 'approval_batch': {
      // 回放帧不建卡：卡片"此刻在不在等人"的真相只在 REST pending 通道里
      // （App.tsx 的 syncPending）。靠环形回放复活卡片，会把早已裁决/超时的
      // 卡重新显示出来，比看不见更糟 —— 主人会对着一个不存在的 ask_id 点允许。
      if (evt.replayed) return base
      // 单条与合并卡归一成一个容器：单条 = 只有 1 项的批，渲染同一套代码。
      const isBatch = evt.type === 'approval_batch'
      const raw = (isBatch ? evt.items : [evt]) as unknown as Partial<ApprovalItem>[]
      const id = String((isBatch ? evt.batch_id : evt.ask_id) || '')
      if (!id || !raw.length) return base
      const a: ApprovalRequest = {
        ask_id: id, batch: isBatch || raw.length > 1,
        items: raw.map((it) => ({
          ask_id: String(it.ask_id || id), kind: it.kind || '',
          risk: Number(it.risk ?? 2), title: it.title || '', intent: it.intent || '',
          reason: it.reason || '', paths: it.paths || [],
          critical: !!it.critical, source_code: it.source_code || '',
          // 恢复卡要在卡上说明白：主人可能以为这是刚弹的，其实已经等掉了一半窗口
          notes: evt.restored
            ? ['🔄 本卡在你刷新／切走之后由服务端找回（后端仍在等你裁决）', ...(it.notes || [])]
            : (it.notes || []),
        })),
        scopes: (evt.scopes as unknown as ApprovalScope[]) || DEFAULT_SCOPES,
        total: Number(evt.total ?? raw.length), timeout: evt.timeout,
        session_id: evt.session_id,
        // 恢复的卡不许把倒计时重置成满格，那是在骗主人：服务端给了剩余秒数，
        // 就折算回一个「过去的出生时刻」，让秒数继续往下走。
        born: Date.now() - Math.max(0,
          (Number(evt.timeout ?? 300) - Number(evt.remaining ?? evt.timeout ?? 300)) * 1000),
        restored: !!evt.restored,
        user_request: evt.user_request || '',
        // 这两个字段必须在这里显式搬一遍：本 reducer 是逐字段手工白名单构造，
        // types.ts 里加了可选字段而这里漏抄，值就会静默变 undefined ——
        // tsc 不报、构建不报，只有真机看一眼界面才发现输入框根本没渲染。
        accepts_note: !!evt.accepts_note,
        note_hint: String(evt.note_hint || ''),
      }
      // 同 id 重复到达（SSE 重连补发）时替换而非堆叠
      const rest = base.approvals.filter((x) => x.ask_id !== a.ask_id)
      const agent = { ...base.agent, turn_phase: 'AUDIT_WAIT' as const, busy: true, can_send: false }
      return { ...base, approvals: [...rest, a], agent }
    }
    case 'approval_resolved':
    case 'approval_batch_resolved':
    case 'approval_expired': {
      // 门禁结束（批准 / 拒绝 / 超时 / 回合终止）：摘卡并把徽标交还主循环。
      // 只处理仍在队列里的 id —— 晚到的帧不许改写已经 done 的回合状态。
      const gone = String(evt.ask_id || evt.batch_id || '')
      const hit = base.approvals.find((x) => x.ask_id === gone)
      const next: State = { ...base, approvals: base.approvals.filter((x) => x.ask_id !== gone) }
      if (!hit) return next
      next.agent = { ...base.agent, turn_phase: 'THINKING' as const }
      if (evt.type === 'approval_expired') {
        return toast(next, 'warn', '审批超时：该操作已按「拒绝」处理，未被执行')
      }
      return next
    }
    case 'mid_turn': {
      // 中期交互（用户交代）：三个阶段都走事件 —— accepted 落座、injected 送到、
      // dropped 作废。回放帧一律不动消息列表：它与卡片恢复同一条哲学 ——
      // 重连/刷新时的环形回放不是"此刻正在发生什么"的证据，照它写会把早已
      // 注入落盘的旧交代再显示一遍（历史里本来就有一条）。
      if (evt.replayed) return base
      const sid = typeof evt.session_id === 'string' ? evt.session_id : null
      if (!sid) return base
      const here = sid === state.current
      if (evt.mid === 'accepted') {
        if (!here) return base
        const id = evt.item_id || ''
        if (id && base.messages.some((m) => m.mid_id === id)) return base   // 本地已落座 → 不堆叠
        return { ...base, messages: [...base.messages, {
          role: 'user' as const, content: evt.text || '',
          mid: 'pending' as const, mid_id: id,
        }] }
      }
      const ids = new Set((evt.ids || []).map(String))
      if (!ids.size) return base
      const status: HistoryMessage['mid'] = evt.mid === 'dropped' ? 'dropped' : 'delivered'
      let hit = false
      const messages = base.messages.map((m) => {
        if (m.mid_id && ids.has(m.mid_id) && m.mid !== status) {
          hit = true
          return { ...m, mid: status }
        }
        return m
      })
      if (!hit) return base
      const next: State = { ...base, messages }
      // 没送到就必须当面说清：那条交代不会生效，别让人以为 AB 看到了
      if (evt.mid === 'dropped' && here) {
        const first = (evt.texts && evt.texts[0]) || ''
        const brief = first.length > 24 ? first.slice(0, 24) + '…' : first
        return toast(next, 'warn',
          `「用户交代」未送达（回合已结束）：${brief || (evt.count || 1) + ' 条'} —— 需要就重发一次`)
      }
      return next
    }
    case 'clarify_request': {
      if (evt.replayed) return base          // 同审批：回放不建卡，真相在 REST pending
      const c: ClarifyRequest = {
        ask_id: evt.ask_id || '', question: evt.question || '',
        options: evt.options || [], kind: (evt.mode as ClarifyRequest['kind']) || 'choice',
        run_id: evt.run_id, session_id: evt.session_id, timeout: evt.timeout,
        remaining: typeof evt.remaining === 'number' ? evt.remaining : undefined,
        restored: !!evt.restored,
        // 批次信息（新增）：同批一共几问、此刻还剩几个待答 —— 卡片上要如实显示
        batch_size: typeof evt.batch_size === 'number' ? evt.batch_size : undefined,
        batch_live: typeof evt.batch_live === 'number' ? evt.batch_live : undefined,
        born: Date.now() - Math.max(0, ((Number(evt.timeout ?? 120))
          - Number(evt.remaining ?? evt.timeout ?? 120)) * 1000),
      }
      const agent = { ...base.agent, turn_phase: 'CLARIFY_WAIT' as const, busy: true, can_send: false }
      // 同 ask_id 重复到达（SSE 重连补发）→ 替换；不同 ask_id = 同批的另一个问题 → 追加。
      // 这里**绝不能**覆盖整个槽位：一个回合里可以同时挂着好几张卡。
      const rest = base.clarifies.filter((x) => x.ask_id !== c.ask_id)
      return { ...base, clarifies: [...rest, c],
               clarifyWindow: { left: Number(evt.remaining ?? evt.timeout ?? 120), at: Date.now() },
               agent }
    }
    case 'clarify_resolved': {
      // 挂着的提问整批作废（回合被终止等）→ 卡立即从屏幕上摘掉，不留僵尸
      if (evt.replayed) return base
      if (!base.clarifies.length) return base
      return { ...base, clarifies: [], clarifyWindow: null }
    }
    case 'done': {
      // 归属判定必须覆盖"写数据"的路径：否则 A 会话的回复会被 append 进你正看着的
      // B 会话消息列表（切回去才发现串了台）。
      const sid = typeof evt.session_id === 'string' ? evt.session_id : null
      const here = sid === null || sid === state.current
      const content = evt.content || base.streaming || ''
      const messages = here && content
        ? [...base.messages, { role: 'assistant' as const, content }]
        : base.messages
      // 回放帧只补时间线 —— 它不是"此刻正在发生什么"的证据。少了这一条，初次打开
      // （since=0，整环回放）时上一回合的 done 会把正挂着的审批卡一起清掉，
      // 表现是"刷新后卡片消失，且要刷几次才复现"（取决于回放与恢复请求谁先到）。
      if (evt.replayed) return { ...base, messages }
      const agent = { ...base.agent, turn_phase: 'IDLE' as const, busy: false, can_send: true }
      const next: State = {
        ...base, agent, messages, clarifies: [], clarifyWindow: null, approvals: [],
        turnOwner: null, pipes: idleAll(base.pipes),
        streaming: here ? null : base.streaming,
        progress: here ? [] : base.progress,
        turnError: here ? null : base.turnError,
      }
      // [Fix] 刷新会全量回放 hub 历史：其它/旧会话的 interrupted done 若无条件弹 toast，
      // 用户会误以为当前对话被刷新中断。仅当事件归属当前正在看的会话才提示。
      if (evt.interrupted && here) return toast(next, 'warn', '回合已中断（进度已保存到会话）')
      return next
    }
    case 'error': {
      const sid2 = typeof evt.session_id === 'string' ? evt.session_id : null
      const here2 = sid2 === null || sid2 === state.current
      const msg = evt.message || evt.error || '未知错误'
      if (evt.replayed) return base          // 回放不写"此刻状态"，也不弹告警
      const agent = { ...base.agent, turn_phase: 'IDLE' as const, busy: false, can_send: true }
      const nextErr: State = { ...base, agent, turnOwner: null, clarifies: [], clarifyWindow: null,
        approvals: [], pipes: idleAll(base.pipes),
                               turnError: here2 ? msg : base.turnError,
                               streaming: here2 ? null : base.streaming }
      // 同理：回放来的非当前会话 error 事件只校正状态，不弹 toast
      return here2 ? toast(nextErr, 'err', msg) : nextErr
    }
    case 'pipeline': {
      if (!evt.tc_id) return base
      const prev = base.pipes[evt.tc_id]
      const pipes: Record<string, PipeView> = { ...base.pipes, [evt.tc_id]: {
        tool: evt.tool || prev?.tool || '?',
        status: evt.status || 'idle',
        layer: evt.layer ?? prev?.layer ?? null,
        at: now(),
        thread: evt.thread ?? prev?.thread ?? null,
        elapsed: evt.elapsed ?? prev?.elapsed ?? null,
      } }
      return { ...base, pipes, pipeSid: evt.session_id ?? base.pipeSid }
    }
    case 'usage': {
      const esid = typeof evt.session_id === 'string' && evt.session_id ? evt.session_id : null
      const sess = evt.session as UsageNode | undefined
      if (!esid || !sess) return base            // 拿不到归属就不记，避免串会话
      // 按会话分桶常驻：切走再切回沿用同一桶，不重置（用户要求"来回看本会话消耗"）。
      const bySid = { ...base.usageBySid, [esid]: sess }
      const mdl = evt.model ? String(evt.model) : null
      const models = mdl ? { ...base.modelBySid, [esid]: mdl } : base.modelBySid
      if (esid !== state.current) return { ...base, usageBySid: bySid, modelBySid: models }
      const snap: UsageSnapshot = {
        scope: 'session',
        node: sess,
        session: sess,
        global: (evt.global as UsageNode) || sess,
        model: mdl ?? base.modelBySid[esid] ?? null,
        at: now(),
      }
      return { ...base, usageBySid: bySid, modelBySid: models, usage: snap }
    }
    case 'heartbeat':
    default:
      return base
  }
}

function reducer(state: State, a: Action): State {
  switch (a.type) {
    case 'set_agent': {
      const agent = { ...state.agent, ...a.agent }
      // 刷新后延续思考中：status 里 busy 且带 session_id，说明那会话有回合在跑
      const turnOwner = agent.busy ? (a.agent.session_id ?? state.turnOwner) : null
      // 计量账本校准：bridge 把上次退出的每会话用量落了盘，重启后随 hello 带回来。
      // 本地桶优先（更实时），账本只补本地没有的会话 —— 于是「没发过消息也有数」。
      const led = (a.agent.hello as { usage_ledger?: { by_sid?: Record<string, UsageNode> } } | undefined)
        ?.usage_ledger?.by_sid
      const bySid = led ? { ...led, ...state.usageBySid } : state.usageBySid
      const usage = state.usage ?? (state.current && bySid[state.current] ? {
        scope: 'session' as const, node: bySid[state.current],
        session: bySid[state.current], global: bySid[state.current],
        model: state.modelBySid[state.current] ?? null, at: now(),
      } : state.usage)
      return { ...state, agent, turnOwner, usageBySid: bySid, usage }
    }
    case 'set_sessions': return { ...state, sessions: a.sessions }
    case 'set_current': {
      // 切会话只换显示口径，不清 usageBySid：切回来还能看到该会话的累计
      const usage = a.sid && state.usageBySid[a.sid] ? {
        scope: 'session' as const, node: state.usageBySid[a.sid],
        session: state.usageBySid[a.sid], global: state.usageBySid[a.sid],
        model: state.modelBySid[a.sid] ?? null, at: now(),
      } : null
      // 待裁决卡片一律保留，不按会话过滤 —— 这一条是四个现象里"切会话/新建后
      // 卡片消失"的正面修法。审批与提问是【进程级】闸门（主循环串行，同一时刻
      // 最多一批），后端 Event 还在死等；卡片的归属只是它顺带带的标签。
      // 上一版写成"只保留属于目标会话的"：切走时把卡删了，切回来自然什么都没有，
      // 而后端仍在等 —— 于是整批工具等到超时、循环当场卡死。
      // 归属不同的卡由卡片自己显示"属于别的会话"的提示，不靠删卡来"理顺"视图。
      return { ...state, current: a.sid, streaming: null, turnError: null, usage }
    }
    case 'set_messages': return { ...state, messages: a.messages, timeline: a.timeline }
    case 'append_user':
      return {
        ...state,
        messages: [...state.messages, { role: 'user', content: a.text }],
        streaming: null, turnError: null, progress: [],
      }
    case 'append_mid_turn': {
      // 中期交互「用户交代」：投递回执（HTTP 200）一到就本地落座，不等 SSE。
      // 与 SSE 的 accepted 事件用 item_id 判重 —— 两条通道谁先到都不会堆两条。
      // 注意这里**不清** streaming/progress：交代不打断正在跑的回合视图。
      const id = a.itemId
      if (id && state.messages.some((m) => m.mid_id === id)) return state
      return { ...state, messages: [...state.messages, {
        role: 'user', content: a.text, mid: 'pending', mid_id: id }] }
    }
    case 'set_sse': return { ...state, sse: a.sse }
    case 'toast': return toast(state, a.kind, a.msg)
    case 'drop_toast': return { ...state, toasts: state.toasts.filter((t) => t.id !== a.id) }
    case 'resolve_clarify': {
      // 按 ask_id 摘掉这一张：同批的其它问题还在等，绝不清空整个队列
      const rest = state.clarifies.filter((x) => x.ask_id !== a.ask_id)
      // 有人作答 → 后端把本批共享窗口续期了。用回执里的真值续算，剩下的卡不会被误判超时。
      const win = typeof a.remaining === 'number' && a.remaining > 0
        ? { left: a.remaining, at: Date.now() }
        : state.clarifyWindow
      return { ...state, clarifies: rest, clarifyWindow: rest.length ? win : null }
    }
    case 'resolve_approval':
      return { ...state, approvals: state.approvals.filter((x) => x.ask_id !== a.ask_id) }
    case 'clear_approvals': return { ...state, approvals: [] }
    case 'prune_gates': {
      // 服务端说「这些才是真挂着的」。只剪掉在快照起点之前就已存在的卡：
      // 拉取期间新到达的卡 born 晚于起点，不在剪枝范围内（否则会把刚弹的摘掉）。
      // 这一条防的是 bridge 换代后屏幕上留着永远点不动的僵尸卡。
      //
      // approvals / clarify 可分别关闭：某条通道拉取失败时，我们对它的真实状态
      // 一无所知 —— 此时"没看见卡"绝不等于"服务端没有卡"。拿看不见当不存在去剪枝，
      // 会把屏幕上真挂着的卡删掉，正是要修的那类 bug 的镜像。默认开启以保持旧语义。
      const live = new Set(a.ids)
      const approvals = a.approvals === false ? state.approvals
        : state.approvals.filter((x) => live.has(x.ask_id) || (x.born ?? 0) >= a.before)
      // 提问是多卡的：逐张判定，留下"服务端确认还在"或"快照起点之后才出现的"那些
      const clarPrev = state.clarifies
      const clarifies = a.clarify === false ? clarPrev
        : clarPrev.filter((x) => live.has(x.ask_id) || (x.born ?? 0) >= a.before)
      if (approvals.length === state.approvals.length
          && clarifies.length === clarPrev.length) return state
      return { ...state, approvals, clarifies }
    }
    case 'reset_timeline': return { ...state, timeline: [], progress: [] }
    // 网关换代：**不再清桶**（主人 2026-09-13：任何情况下都要常驻）。
    // bridge 侧已把计量账本落盘并在新代载入，新代发来的桶是接着上次累计的，
    // 所以这里只需把 seq 打回本代起点，避免落盘用旧的高 lastSeq 覆盖。
    case 'reset_usage': return { ...state, lastSeq: a.seq ?? 0 }
    case 'event': return onEvent(state, a.evt)
    default: return state
  }
}

const Ctx = createContext<{ state: State; dispatch: Dispatch<Action> } | null>(null)

export function AppProvider({ children }: { children: React.ReactNode }) {
  // 初始态从本地装载：刷新不丢；网关换代时由 App.tsx 派发 reset_usage 作废
  const [state, dispatch] = useReducer(reducer, undefined, bootState)
  // 概况桶落盘：存的是每个会话最新累计值，体量很小
  useEffect(() => {
    saveUsage(state.usageBySid, state.modelBySid, state.lastSeq)
  }, [state.usageBySid, state.modelBySid, state.lastSeq])

  const value = useMemo(() => ({ state, dispatch }), [state])
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useApp() {
  const c = useContext(Ctx)
  if (!c) throw new Error('useApp 必须在 AppProvider 内使用')
  return c
}

/**
 * 初始态：把上一次的概况桶从 localStorage 捞回来。
 * 桶只在「本地快照的 hub_seq 落在已保存的订阅点之后」才有效，换代判定在 App.tsx 里
 * 用「收到的帧 hub_seq < 本地 usage_seq」触发 reset_usage（网关的 hub_seq 每代从 0 起）。
 */
function bootState(): State {
  const stored = loadUsage()
  return { ...initialState, usageBySid: stored.bySid as State['usageBySid'],
           modelBySid: stored.models as State['modelBySid'] }
}
