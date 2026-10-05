import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { hydrateHistory } from './historyHydration'
import type { HistoryHydrationState } from './historyHydration'

function initialHistory(count: number): HistoryHydrationState {
  return { status: count ? 'loading' : 'complete', summaries: [], missingIds: [],
    failedChunks: [], rememberedCount: count }
}

export interface SharedHistoryHydration {
  history: HistoryHydrationState
  retry: () => void
  reload: () => void
  discard: () => void
}

// One memory-only owner serves both read views. Membership and persisted-write
// generations invalidate it; navigating between views does not.
export function useHistoryHydration(sessionIds: string[], version: number, enabled: boolean): SharedHistoryHydration {
  const membership = sessionIds.join('\n')
  const ids = useMemo(() => membership ? membership.split('\n') : [], [membership])
  const key = JSON.stringify([membership, version])
  const [snapshot, setSnapshot] = useState(() => ({ key, history: initialHistory(ids.length) }))
  const [request, setRequest] = useState(0)
  const cache = useRef<{ key: string; history: HistoryHydrationState } | null>(null)
  const retryState = useRef<{ key: string; history: HistoryHydrationState } | null>(null)
  const controller = useRef<AbortController | null>(null)
  const generation = useRef(0)

  useEffect(() => {
    if (!enabled || cache.current?.key === key) return
    const pending = new AbortController()
    controller.current = pending
    const read = ++generation.current
    const previous = retryState.current?.key === key ? retryState.current.history : undefined
    retryState.current = null
    void hydrateHistory(ids, {
      signal: pending.signal,
      ...(previous?.failedChunks.length ? { previous, retryChunks: previous.failedChunks } : {}),
    }).then((history) => {
      if (pending.signal.aborted || generation.current !== read) return
      cache.current = { key, history }
      setSnapshot({ key, history })
    }).catch(() => {
      if (pending.signal.aborted || generation.current !== read) return
      const history = { ...(previous ?? initialHistory(ids.length)), status: 'error' as const }
      cache.current = { key, history }
      setSnapshot({ key, history })
    })
    return () => { pending.abort(); generation.current += 1 }
  }, [enabled, ids, key, request])

  const readAgain = useCallback((failedOnly: boolean) => {
    controller.current?.abort()
    generation.current += 1
    const previous = cache.current?.key === key ? cache.current.history : undefined
    retryState.current = failedOnly && previous ? { key, history: previous } : null
    cache.current = null
    setSnapshot({ key, history: { ...(previous ?? initialHistory(ids.length)), status: 'loading' } })
    setRequest((value) => value + 1)
  }, [ids.length, key])
  const retry = useCallback(() => readAgain(true), [readAgain])
  const reload = useCallback(() => readAgain(false), [readAgain])
  const discard = useCallback(() => {
    controller.current?.abort()
    generation.current += 1
    retryState.current = null
    const empty = { key, history: initialHistory(0) }
    cache.current = empty
    setSnapshot(empty)
  }, [key])

  return { history: snapshot.key === key ? snapshot.history : initialHistory(ids.length), retry, reload, discard }
}
