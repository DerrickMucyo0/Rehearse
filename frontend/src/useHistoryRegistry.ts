import { useCallback, useEffect, useState } from 'react'
import {
  addRememberedSession, clearRememberedHistory, isHistoryStorageEvent,
  listRememberedSessions, removeRememberedSession,
} from './historyStorage'

const STORAGE_WARNING = 'History could not be saved in this browser.'
const CAPACITY_WARNING = 'History storage is full. Remove a remembered session to save another.'

// Only opaque IDs enter persistent storage. Display facts belong to read APIs.
export function useHistoryRegistry() {
  const [registry, setRegistry] = useState(() => listRememberedSessions())
  const [notice, setNotice] = useState<string | null>(null)
  const [cacheGeneration, setCacheGeneration] = useState(0)
  const refresh = useCallback(() => { setRegistry(listRememberedSessions()) }, [])

  useEffect(() => {
    function changed(event: StorageEvent) {
      if (!isHistoryStorageEvent(event)) return
      refresh()
      // Even unchanged membership can accompany newly persisted facts.
      setCacheGeneration((value) => value + 1)
    }
    window.addEventListener('storage', changed)
    return () => { window.removeEventListener('storage', changed) }
  }, [refresh])

  const remember = useCallback((sessionId: string) => {
    const result = addRememberedSession(sessionId)
    setNotice(result.ok ? null : result.reason === 'capacity' ? CAPACITY_WARNING : STORAGE_WARNING)
    setCacheGeneration((value) => value + 1)
    refresh() // The writing tab does not receive its own storage event.
  }, [refresh])

  const remove = useCallback((sessionId: string) => {
    const result = removeRememberedSession(sessionId)
    setNotice(result.ok ? null : STORAGE_WARNING)
    setCacheGeneration((value) => value + 1)
    refresh()
  }, [refresh])

  const clear = useCallback(() => {
    const result = clearRememberedHistory()
    setNotice(result.ok ? null : STORAGE_WARNING)
    setCacheGeneration((value) => value + 1)
    refresh()
  }, [refresh])

  return {
    sessionIds: registry.ids, storageError: notice ?? registry.error,
    cacheGeneration, remember, remove, clear,
  }
}
