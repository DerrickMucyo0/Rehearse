import { useCallback, useEffect, useRef, useState } from 'react'
import { hydrateHistory } from './historyHydration'
import type { HistoryHydrationState } from './historyHydration'
import { getAuthState, isAuthWorkspaceCurrent } from './auth'

function initialHistory(): HistoryHydrationState {
  return { status: 'loading', summaries: [], nextCursor: null, pageError: false }
}

export interface SharedHistoryHydration {
  history: HistoryHydrationState
  retry: () => void
  reload: () => void
  loadMore: () => void
  canLoadMore: boolean
}

// The authenticated workspace owns this memory-only cache. Persisted writes
// invalidate it; History/Progress navigation reuses it without extra requests.
export function useHistoryHydration(version: number, enabled: boolean): SharedHistoryHydration {
  const auth = getAuthState()
  const workspaceGeneration = auth.status === 'authenticated' ? auth.generation : null
  const key = JSON.stringify([version, workspaceGeneration])
  const [snapshot, setSnapshot] = useState(() => ({ key, history: initialHistory() }))
  const [request, setRequest] = useState(0)
  const cache = useRef<{ key: string; history: HistoryHydrationState } | null>(null)
  const continuation = useRef<{ key: string; history: HistoryHydrationState } | null>(null)
  const controller = useRef<AbortController | null>(null)
  const generation = useRef(0)

  useEffect(() => {
    if (!enabled || cache.current?.key === key) return
    const pending = new AbortController()
    controller.current = pending
    const read = ++generation.current
    const previous = continuation.current?.key === key ? continuation.current.history : undefined
    continuation.current = null
    void hydrateHistory({ signal: pending.signal, ...(previous ? { previous } : {}) }).then((history) => {
      if (pending.signal.aborted || generation.current !== read || workspaceGeneration === null || !isAuthWorkspaceCurrent(workspaceGeneration)) return
      cache.current = { key, history }
      setSnapshot({ key, history })
    }).catch(() => {
      if (pending.signal.aborted || generation.current !== read || workspaceGeneration === null || !isAuthWorkspaceCurrent(workspaceGeneration)) return
      const history: HistoryHydrationState = { ...(previous ?? initialHistory()), status: previous?.summaries.length ? 'partial' : 'error', pageError: true }
      cache.current = { key, history }
      setSnapshot({ key, history })
    })
    return () => { pending.abort(); generation.current += 1 }
  }, [enabled, key, request, workspaceGeneration])

  const readAgain = useCallback((append: boolean) => {
    if (controller.current && !controller.current.signal.aborted && cache.current?.key !== key) return
    controller.current?.abort()
    generation.current += 1
    const previous = cache.current?.key === key ? cache.current.history : undefined
    continuation.current = append && previous ? { key, history: previous } : null
    cache.current = null
    setSnapshot({ key, history: { ...(append && previous ? previous : initialHistory()), status: 'loading' } })
    setRequest((value) => value + 1)
  }, [key])
  const retry = useCallback(() => readAgain(true), [readAgain])
  const reload = useCallback(() => readAgain(false), [readAgain])
  const history = snapshot.key === key ? snapshot.history : initialHistory()
  const canLoadMore = history.status !== 'loading' && history.nextCursor !== null && !history.pageError
  const loadMore = useCallback(() => {
    if (cache.current?.key === key && cache.current.history.nextCursor !== null && !cache.current.history.pageError) readAgain(true)
  }, [readAgain, key])

  return { history, retry, reload, loadMore, canLoadMore }
}
