import { memo, useEffect, useRef, useState } from 'react'
import { renderMarkdown } from '../lib/md'
import { MID_BADGE, MID_MARK } from '../lib/midTurn'
import { useApp } from '../store/appStore'

export default memo(function MessageList() {
  const { state } = useApp()
  const endRef = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const bottomRef = useRef<HTMLDivElement>(null)

  // 回合归属门控：busy/turn_phase 是进程级事实，只有当这个回合确实属于
  // 当前会话时才在本视图显示状态卡，否则切会话会串台（旧回合卡片挂在空会话下面）。
  const owns = state.turnOwner !== null && state.turnOwner === state.current
  const busy = state.agent.busy && owns
  const live = state.streaming
  const myProgress = state.progress.filter((x) => !x.sid || x.sid === state.current)
  // 进度默认只给最后 3 条单行摘要（长回合里全量会把对话顶出屏幕），可一键展开全部。
  // store 里环形保留最近 60 条，更早的全量在 logs/bridge_*.log。
  const [openProg, setOpenProg] = useState(false)
  const lastSid = useRef(state.current)
  useEffect(() => {                     // 展开态属于"这个会话的视图"，切走就归位
    if (lastSid.current !== state.current) { lastSid.current = state.current; setOpenProg(false) }
  }, [state.current])

  useEffect(() => {
    const onScroll = () => {
      const el = bottomRef.current?.parentElement
      if (!el) return
      stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120
    }
    const el = bottomRef.current?.parentElement
    el?.addEventListener('scroll', onScroll)
    return () => el?.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (stick.current) endRef.current?.scrollIntoView({ block: 'end', behavior: 'smooth' })
  }, [state.messages.length, live, myProgress.length])

  return (
    <div className="msgs" ref={bottomRef}>
      {state.messages.length === 0 && !busy && (
        <div className="hero">
          <div className="hero-mark">☲</div>
          <h3>AetherBreath WebUI</h3>
          <p>左侧选会话或新建 → 顶部「🟢 开机」拉起 AB 子进程 → 直接开聊。<br />
             右栏实时看工具调用；卡住了点「🛑 强制关闭」，重开可续聊（最多丢当前半轮）。</p>
          <p className="hero-tip">⚠️ 同一会话请勿同时在 CLI 与 WebUI 操作：两端是独立进程，只共享磁盘。</p>
        </div>
      )}
      {state.messages.map((m, i) => (
        <div key={i} className={`msg ${m.role}${m.mid ? ' mid' : ''}${m.mid === 'dropped' ? ' dropped' : ''}`}>
          <div className="who">
            {m.role === 'user'
              ? (m.mid
                  ? <>👤 你 · <b>{MID_MARK}</b>
                      <span className={`mid-badge ${m.mid}`}>{MID_BADGE[m.mid]}</span></>
                  : '👤 你')
              : '☲ AB'}
          </div>
          {m.content ? (
            m.role === 'user'
              ? <div className="body-text plain">{m.content}</div>
              : <div className="body-text md" dangerouslySetInnerHTML={{ __html: renderMarkdown(m.content) }} />
          ) : null}
          {m.tool_calls && m.tool_calls.length > 0 && (
            <div className="mini-tools">
              {m.tool_calls.map((t, ti) => (
                <span key={t.id || ti} className={`mini-tool ${t.failed ? 'err' : ''} ${t.pending ? 'wait' : ''}`}>
                  🔧 {t.name}{t.pending ? '（历史中未见返回）' : ''}
                </span>
              ))}
            </div>
          )}
        </div>
      ))}

      {busy && (
        <div className="msg assistant live">
          <div className="who">☲ AB <span className="typing">●●●</span></div>
          {live ? (
            <div className="body-text md" dangerouslySetInnerHTML={{ __html: renderMarkdown(live) }} />
          ) : (
            <div className="body-text waittext">
              {state.agent.turn_phase === 'CLARIFY_WAIT' ? '等待你在下方回答…'
                : state.agent.turn_phase === 'TOOL_RUNNING' ? '工具执行中…'
                : '思考中…'}
            </div>
          )}
          {myProgress.length > 0 && (
            <div className="progress-box">
              <button className="prog-toggle" onClick={() => setOpenProg((v) => !v)}
                      title="折叠时只看最后 3 条；展开可看全部（内存里保留最近 60 条，更早的全量见 bridge 日志）">
                {openProg ? '▾ 收起进度' : `▸ 展开全部 ${myProgress.length} 条进度`}
                {!openProg && myProgress.length > 3 ? <em>（仅显示最后 3 条）</em> : null}
              </button>
              <div className={`prog-list ${openProg ? 'open' : ''}`}>
                {(openProg ? myProgress : myProgress.slice(-3)).map((x, i) => (
                  <div key={`${x.at}-${i}`} className="progress-line">› {x.text}</div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {state.turnError && (
        <div className="msg error">
          <div className="who">❌ 错误</div>
          <div className="body-text plain">{state.turnError}</div>
        </div>
      )}
      <div ref={endRef} />
    </div>
  )
})
