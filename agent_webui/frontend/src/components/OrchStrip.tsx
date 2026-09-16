// 编排器槽位面板：串行 #0 + 并行 #0…#N 常驻平铺，谁在跑谁亮，空闲常灰 —— 控制台那种。
// 槽位定义取自 bridge 上报的池容量（hello.pools）：ThreadPoolExecutor 懒起线程，
// 没用过的槽位在任何事件里都不会出现，所以容量必须显式读，不能靠"见过哪些线程名"倒推。
import { useMemo } from 'react'
import { useApp } from '../store/appStore'
import type { PoolCaps } from '../types'

type Busy = { tool: string; status: string; layer: number | null; elapsed?: number | null; sid?: string | null }
type Slot = { key: string; pool: 'serial' | 'parallel'; no: number; busy: Busy | null; last: string; n: number; bad: number }

const FALLBACK: PoolCaps = { parallel: { workers: 8 }, serial: { workers: 1 } }

// 线程名形如 orch-parallel_3 / orch-serial_0（Python 的 prefix_序号）
function parse(thread?: string | null): { pool: 'serial' | 'parallel'; no: number } | null {
  if (!thread) return null
  const m = /^(?:orch-)?(parallel|serial)_(\d+)$/.exec(thread)
  if (!m) return null
  return { pool: m[1] === 'serial' ? 'serial' : 'parallel', no: Number(m[2]) }
}

export default function OrchStrip() {
  const { state } = useApp()
  const caps = state.agent.hello?.pools || FALLBACK
  const known = !!state.agent.hello?.pools
  const nPar = Math.max(1, Math.min(64, caps.parallel?.workers ?? 8))
  const nSer = Math.max(1, Math.min(8, caps.serial?.workers ?? 1))
  const real = Object.keys(state.pipes).length > 0

  const slots = useMemo<Slot[]>(() => {
    const map = new Map<string, Slot>()
    const key = (pool: string, no: number) => `${pool}#${no}`
    for (let i = 0; i < nSer; i++) map.set(key('serial', i), { key: key('serial', i), pool: 'serial', no: i, busy: null, last: '', n: 0, bad: 0 })
    for (let i = 0; i < nPar; i++) map.set(key('parallel', i), { key: key('parallel', i), pool: 'parallel', no: i, busy: null, last: '', n: 0, bad: 0 })
    const put = (thread: string | null | undefined, f: (s: Slot) => void) => {
      const p = parse(thread)
      if (!p) return
      const k = key(p.pool, p.no)
      let s = map.get(k)
      if (!s) {                                   // 槽位号比上报容量大：补一个，绝不丢信息
        s = { key: k, pool: p.pool, no: p.no, busy: null, last: '', n: 0, bad: 0 }
        map.set(k, s)
      }
      f(s)
    }
    if (real) {
      for (const tc of Object.keys(state.pipes)) {
        const v = state.pipes[tc]
        put(v.thread, (s) => {
          s.n += 1
          if (v.status === 'running' || v.status === 'pending') s.busy = { tool: v.tool, status: v.status, layer: v.layer ?? null, elapsed: v.elapsed ?? null }
          else { s.last = v.tool; if (v.status === 'failed' || v.status === 'cancelled') s.bad += 1 }
        })
      }
    } else {
      for (const t of state.timeline) {
        put(t.thread, (s) => {
          if (t.status === 'running') s.busy = { tool: t.tool, status: 'running', layer: null, elapsed: null }
          else { s.n += 1; s.last = t.tool; if (!t.ok) s.bad += 1 }
        })
      }
    }
    const busyFirst = [...map.values()]
    return busyFirst.sort((a, b) => {            // 固定顺序：串行在前、按槽号，跑着的不顶位（控制台式）
      if (a.pool !== b.pool) return a.pool === 'serial' ? -1 : 1
      return a.no - b.no
    })
  }, [state.pipes, state.timeline, real, nSer, nPar])

  const running = slots.filter((s) => s.busy?.status === 'running').length
  const waiting = slots.filter((s) => s.busy?.status === 'pending').length
  const started = (caps.parallel?.started ?? 0) + (caps.serial?.started ?? 0)
  const otherSid = real && state.pipeSid && state.pipeSid !== state.current

  return (
    <div className="orch">
      <div className="orch-head">
        <span className="orch-title">任务编排器</span>
        <span className={`orch-mode ${real ? 'real' : 'derived'}`}
              title={real ? 'bridge 上报的 ToolPipeline 真状态' : '未收到 pipeline 事件：按工具事件的线程名推导（重启 AB 可得真状态）'}>
          {real ? '真状态' : '推导'}
        </span>
        {otherSid ? <span className="orch-of" title="AB 正在跑的会话不是当前视图">· {state.pipeSid}</span> : null}
        <span className="orch-kv" title={known ? `池容量由 bridge 上报；已创建线程 ${started} 个（懒起）` : '未拿到池容量，按默认 8+1 画格子'}>
          并 {nPar} · 串 {nSer} · 跑 {running}{waiting ? ` · 排队 ${waiting}` : ''}
        </span>
      </div>

      <div className="orch-bars">
        {slots.map((s) => {
          const b = s.busy
          const cls = b ? (b.status === 'running' ? 'running' : 'pending') : s.bad ? 'failed' : 'idle'
          return (
            <div key={s.key} className={`orch-bar ${cls}`}
                 title={`${s.pool === 'serial' ? '串行池' : '并行池'} #${s.no} · 线程 ${'orch-' + s.pool + '_' + s.no}${b ? ` · ${b.status}` : s.last ? ` · 上次 ${s.last}` : ' · 从未使用'}`}>
              <span className="orch-dot" />
              <span className="orch-name">{s.pool === 'serial' ? '串' : '并'}#{s.no}</span>
              <span className="orch-cur">
                {b ? <><b>{b.status === 'running' ? '⏳' : '◦'} {b.tool}</b>
                       {b.layer != null ? <i>L{b.layer}</i> : null}</>
                   : <span className="orch-freetxt">空闲{s.last ? <i> · 上次 {s.last}</i> : null}</span>}
              </span>
              <span className="orch-stat">{s.n ? `${s.n} 次` : ''}{s.bad ? ` · 💥${s.bad}` : ''}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
