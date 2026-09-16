// 会话切换的唯一入口：加载历史 + 从 tool_calls 配对还原时间线。
import { useCallback } from 'react'
import { api } from '../api'
import { parseMidTurn } from '../lib/midTurn'
import { useApp, timelineFromHistory } from '../store/appStore'

export function useLoadSession() {
  const { dispatch } = useApp()
  return useCallback(async (sid: string) => {
    dispatch({ type: 'set_current', sid })
    try {
      const h = await api.history(sid)
      // 历史里注入过的「用户交代」只有 role/content，靠那行标识前缀认出来 ——
      // 标成 delivered（已注入必然已送达）并剥掉标识/引导行，界面才是它本来的样子。
      const msgs = (h.exists ? h.messages : []).map((m) => {
        if (m.role !== 'user') return m
        const p = parseMidTurn(m.content || '')
        return p.isMid ? { ...m, content: p.text, mid: 'delivered' as const } : m
      })
      dispatch({ type: 'set_messages', messages: msgs, timeline: timelineFromHistory(msgs) })
    } catch (e) {
      dispatch({ type: 'set_messages', messages: [], timeline: [] })
      dispatch({ type: 'toast', kind: 'err', msg: `读取会话失败：${(e as Error).message}` })
    }
  }, [dispatch])
}
