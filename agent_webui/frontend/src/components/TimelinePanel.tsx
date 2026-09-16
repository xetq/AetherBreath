// 「工具状态机」面板：自上而下 = 概况卡片 → 编排器管道条状卡片 → 工具时间线（最新在上方）。
import { useRef, useState } from 'react'
import { useApp } from '../store/appStore'
import OrchStrip from './OrchStrip'
import OverviewCard from './OverviewCard'
import type { TimelineEntry } from '../types'

/** MCP 调用在时间线里显示成 `mcp_call → station / tool`。
 *  v2 的 MCP 都从两个元工具进来，光看工具名只知道"调了 MCP"，看不出**调了哪个 server 的哪个工具**
 *  （主人 Q26 的要求：调用时在 WebUI 里能看见）。参数不是 JSON 时就不显示，不猜。 */
function mcpTarget(t: TimelineEntry): string {
  if (t.tool !== 'mcp_call') return ''
  try {
    const a = JSON.parse(t.args || '{}') as { station?: string; tool?: string }
    if (a && a.station) return ` → ${a.station}${a.tool ? ' / ' + a.tool : ''}`
  } catch { /* 参数不是 JSON：不显示 */ }
  return ''
}

function ToolRow({ t }: { t: TimelineEntry }) {
  const [open, setOpen] = useState(false)
  const state = t.status === 'running' ? 'run' : t.ok ? 'ok' : 'err'
  const target = mcpTarget(t)
  return (
    <div className={`tl-item ${state}`}>
      <div className="tl-head" onClick={() => setOpen((v) => !v)}>
        <span className="tl-ico">{t.status === 'running' ? '⏳' : t.ok ? '🔧' : '💥'}</span>
        <span className="tl-tool">{t.tool}{target && <span className="tl-mcp">{target}</span>}</span>
        {t.elapsed != null && <span className="tl-time">{t.elapsed.toFixed(2)}s</span>}
        {t.thread && <span className="tl-thread" title="执行线程（并行批会看到不同 worker）">{t.thread.replace(/^orch-/, '')}</span>}
        {t.source === 'history' && <span className="tl-src" title="来自会话文件还原">历史</span>}
        <span className="tl-caret">{open ? '▾' : '▸'}</span>
      </div>
      <div className="tl-args" title={t.args}>{t.args}</div>
      {open && (
        <div className="tl-detail">
          <div className="tl-lbl">参数</div>
          <pre>{t.args || '（无）'}</pre>
          {t.error
            ? <><div className="tl-lbl err">错误</div><pre className="err">{t.error}</pre></>
            : <><div className="tl-lbl">返回</div><pre>{t.result ?? (t.status === 'running' ? '执行中…' : '（空）')}</pre></>}
        </div>
      )}
    </div>
  )
}

export default function TimelinePanel() {
  const { state } = useApp()
  const listRef = useRef<HTMLDivElement>(null)
  const items = state.timeline
  const running = items.filter((i) => i.status === 'running').length
  const failed = items.filter((i) => i.status === 'done' && !i.ok).length
  const newestFirst = [...items].reverse()          // 最近使用的工具刷新在窗口上方

  return (
    <div className="panel-body tl-root">
      <div className="orch-wrap"><OverviewCard /></div>
      <div className="orch-wrap"><OrchStrip /></div>

      <div className="tl-wrap">
        <div className="panel-sub">
          <span>工具时间线 · {items.length} 次调用</span>
          {running > 0 && <span className="run">⏳ {running} 进行中</span>}
          {failed > 0 && <span className="err">💥 {failed} 失败</span>}
          {items.length > 0 && (
            <button className="btn tiny ghost right" onClick={() => listRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
                    title="切换会话或刷新页面后，时间线会按会话文件重新还原">
              回到最新
            </button>
          )}
        </div>

        {items.length === 0 && (
          <div className="empty">还没有工具调用。让 AB 干点活（比如「用 calculator 算 12*13」）就会实时流出。</div>
        )}

        {/* 中期进度不在这里显示：它是 stdout 流，会一行行把时间线顶出可视区。
            全量留档在 logs/bridge_<开机时间>.log 的 [ab] [中期进度] 行，「运行时」面板也能看尾部。 */}
        <div className="tl-list" ref={listRef}>
          {newestFirst.map((t) => <ToolRow key={t.key} t={t} />)}
        </div>

      </div>
    </div>
  )
}
