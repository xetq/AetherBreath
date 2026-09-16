import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import { useApp } from '../store/appStore'
import { useLoadSession } from '../lib/useLoadSession'
import { debounce, loadText, saveText } from '../lib/persist'
import MessageList from './MessageList'
import ClarifyCard from './ClarifyCard'
import ApprovalCard from './ApprovalCard'
import TurnBadge from './StatusBadge'

const draftKey = (sid: string | null) => `draft.${sid || '_none'}`

export default function ChatView() {
  const { state, dispatch } = useApp()
  const load = useLoadSession()
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)
  // 草稿：400ms 防抖落盘（刷新/误关页面回来还在），且不会每敲一键写一次 localStorage。
  // 单一真相源 draftRef —— 切会话时先立即落盘上一份，避免防抖尾巴写错槽位。
  const draftRef = useRef<{ sid: string | null; text: string }>({ sid: null, text: '' })
  const saveDraft = useMemo(() => debounce(() => {
    saveText(draftKey(draftRef.current.sid), draftRef.current.text)
  }, 400), [])
  useEffect(() => {
    saveText(draftKey(draftRef.current.sid), draftRef.current.text)      // 先把手上这份结清
    const mine = loadText(draftKey(state.current)) || ''
    draftRef.current = { sid: state.current, text: mine }
    setInput(mine)
  }, [state.current])

  const on = state.agent.phase === 'ON'
  const busy = state.agent.busy
  const canSend = on && !busy && !state.clarifies.length
  // 中期交互（「用户交代」）只在一种情形下可用：**AB 正在跑的正是本会话**。
  // 它在跑别的会话时这里没有可投递的目标 —— 按钮保持红色「停止本回合」，
  // 回车也不做任何事：绝不把话投进别人的语境，更不误触停止。
  const busyHere = on && busy && state.turnOwner === state.current
  const hasText = !!input.trim()
  const midMode = busyHere && hasText

  // 首屏：优先接上"上次停留的会话"（刷新延续），否则退回最近一个
  useEffect(() => {
    if (state.current || state.sessions.length === 0) return
    const saved = loadText('current')
    const hit = !!saved && state.sessions.some((x) => x.session_id === saved)
    void load(hit ? (saved as string) : state.sessions[0].session_id)
  }, [state.current, state.sessions, load])

  const send = async (e?: FormEvent) => {
    e?.preventDefault()
    const text = input.trim()
    if (!text || !canSend) return
    let sid = state.current
    if (!sid) {
      try {
        const r = await api.createSession(undefined, 'session')
        sid = r.session_id
        await load(sid)
      } catch (err) {
        dispatch({ type: 'toast', kind: 'err', msg: `新建会话失败：${(err as Error).message}` })
        return
      }
    }
    setSending(true)
    dispatch({ type: 'append_user', text })
    setInput('')
    draftRef.current = { sid, text: '' }
    saveText(draftKey(sid), '')        // 已发出，草稿清掉
    try {
      await api.chat(sid, text)
    } catch (err) {
      dispatch({ type: 'toast', kind: 'err', msg: `发送失败：${(err as Error).message}` })
    } finally {
      setSending(false)
      setTimeout(() => taRef.current?.focus(), 50)
    }
  }

  // 中期交互：把输入框里的字作为「用户交代」投递给正在跑的这个回合。
  // 它不起新回合、不打断工具执行 —— AB 会在下一批工具返回时收到。
  const sendMid = async () => {
    const text = input.trim()
    const sid = state.current
    if (!text || !sid || !midMode || sending) return
    setSending(true)
    try {
      const r = await api.midTurn(sid, text, state.agent.run_id)
      // 先拿到投递回执再清输入框：失败时字还留着，主人重发即可（不丢话）
      setInput('')
      draftRef.current = { sid, text: '' }
      saveText(draftKey(sid), '')
      dispatch({ type: 'append_mid_turn', text, itemId: r.item_id || '' })
    } catch (err) {
      dispatch({ type: 'toast', kind: 'err', msg: `「用户交代」未送达：${(err as Error).message}` })
    } finally {
      setSending(false)
      setTimeout(() => taRef.current?.focus(), 50)
    }
  }

  const stop = async () => {
    try {
      const r = await api.stopChat(state.agent.run_id)
      // stopped=false 表示压根没有活动回合（v1 的 bug：这种情况会让 bridge 自死锁）
      if (r.ok !== false && r.stopped === false) {
        // 后端说没得停 = 前端状态过期，立刻以网关为准校正，别再骗用户「还在忙」
        try { dispatch({ type: 'set_agent', agent: await api.agentStatus() }) } catch { /* 忽略 */ }
        dispatch({ type: 'toast', kind: 'info', msg: r.reason === 'no-active-run' ? '当前没有活动回合，无需中断（界面状态已校正）' : `未执行中断：${r.reason || '原因未知'}` })
      } else {
        dispatch({ type: 'toast', kind: 'warn', msg: r.note ? `中断请求已发出：${r.note}` : '已请求中断，将在下一个边界保存' })
      }
    } catch (e) {
      dispatch({ type: 'toast', kind: 'err', msg: `中断失败：${(e as Error).message}` })
    }
  }

  return (
    <main className="chat">
      <div className="chat-head">
        <div className="chat-title">
          <b>{state.current || '未选择会话'}</b>
          {state.agent.session_id && state.agent.session_id !== state.current && (
            <span className="hint" title="AB 进程当前正在处理的会话">AB 正在跑：{state.agent.session_id}</span>
          )}
        </div>
        <div className="chat-state">
          <TurnBadge phase={state.agent.turn_phase} />
          {state.agent.pid ? <span className="mini">pid {state.agent.pid}</span> : null}
          {state.agent.run_id && busy ? <span className="mini">run {state.agent.run_id}</span> : null}
        </div>
      </div>

      {state.agent.busy && state.turnOwner && state.turnOwner !== state.current ? (
        <div className="elsewhere">
          <span>⏳ 会话 <b>{state.turnOwner}</b> 的回合正在执行，本会话只是旁观位</span>
          <button className="btn tiny ghost" onClick={() => void load(state.turnOwner as string)}>切过去看</button>
        </div>
      ) : null}

      <MessageList />
      <ApprovalCard />
      <ClarifyCard />

      <form className="composer" onSubmit={send}>
        <textarea
          ref={taRef}
          value={input}
          rows={input.split('\n').length > 6 ? 8 : Math.max(2, input.split('\n').length)}
          placeholder={!on ? 'AB 未开机：先点右上角「🟢 开机」'
            : busyHere ? '回合进行中：输入后点紫色「发送」= 用户交代（AB 在下一批工具返回时收到，不打断本回合）'
            : busy ? `AB 正在跑会话 ${state.turnOwner || '?'}，本会话只能旁观`
            : '消息…（Enter 发送 / Shift+Enter 换行）'}
          disabled={!on}
          onChange={(e) => {
            const v = e.target.value
            setInput(v); draftRef.current = { sid: state.current, text: v }; saveDraft()
          }}
          onKeyDown={(e) => {
            if (e.key !== 'Enter' || e.shiftKey) return
            e.preventDefault()
            // 回合运行中：有内容 → 发交代；输入框是空的 → 什么都不做。
            // 这一条是刻意的：空回车绝不能穿透到下面的「⏹ 停止本回合」上去。
            // AB 在跑别的会话时同理什么都不做 —— 本会话没有可投递的目标。
            if (busyHere) { void sendMid(); return }
            if (!busy) { void send() }
          }}
        />
        <div className="composer-acts">
          {busy ? (
            midMode ? (
              /* 紫色 = 中期交互：这一步不打断回合，只是把话塞进模型的下一步动作里。
                 输入框空着时它就退回红色「停止本回合」—— 停止是危险动作，不该常驻在
                 「我刚打完字要发送」的那个位置上。 */
              <button type="button" className="btn mid" disabled={sending}
                      onClick={() => void sendMid()}
                      title="作为「用户交代」送出：不打断当前回合，AB 在下一批工具返回时收到">
                ⤴ 发送（用户交代）
              </button>
            ) : (
              <button type="button" className="btn danger" onClick={() => void stop()}>⏹ 停止本回合</button>
            )
          ) : (
            <button type="submit" className="btn primary" disabled={!canSend || !input.trim() || sending}>
              {on ? '发送 ↵' : '需先开机'}
            </button>
          )}
        </div>
      </form>
    </main>
  )
}
