export const HISTORY_PREFIX = 'rehearse.history.v1:'
export const HISTORY_CAPACITY = 500
const STORAGE_ERROR = 'History could not be saved in this browser.'

export type HistoryStorage = Pick<Storage, 'length' | 'key' | 'getItem' | 'setItem' | 'removeItem'>
export interface HistoryStorageResult { ok: boolean; reason: 'invalid' | 'capacity' | 'storage' | null }

export function normalizeSessionId(value: unknown): string | null {
  return typeof value === 'string' && /^[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$/i.test(value)
    ? value.toLowerCase() : null
}

function localStorageOrThrow(storage?: HistoryStorage): HistoryStorage {
  return storage ?? window.localStorage
}

export function listRememberedSessions(storage?: HistoryStorage): { ids: string[]; error: string | null } {
  try {
    const target = localStorageOrThrow(storage)
    const ids = new Set<string>()
    for (let index = 0; index < target.length; index += 1) {
      const key = target.key(index)
      if (!key?.startsWith(HISTORY_PREFIX)) continue
      const id = normalizeSessionId(key.slice(HISTORY_PREFIX.length))
      if (id !== null && target.getItem(key) === '1') ids.add(id)
    }
    return { ids: [...ids].sort(), error: null }
  } catch {
    return { ids: [], error: STORAGE_ERROR }
  }
}

export function addRememberedSession(value: unknown, storage?: HistoryStorage): HistoryStorageResult {
  const id = normalizeSessionId(value)
  if (id === null) return { ok: false, reason: 'invalid' }
  try {
    const target = localStorageOrThrow(storage)
    const remembered = listRememberedSessions(target)
    if (remembered.error) return { ok: false, reason: 'storage' }
    if (remembered.ids.includes(id)) return { ok: true, reason: null }
    if (remembered.ids.length >= HISTORY_CAPACITY) return { ok: false, reason: 'capacity' }
    target.setItem(`${HISTORY_PREFIX}${id}`, '1')
    return { ok: true, reason: null }
  } catch {
    return { ok: false, reason: 'storage' }
  }
}

export function removeRememberedSession(value: unknown, storage?: HistoryStorage): HistoryStorageResult {
  const id = normalizeSessionId(value)
  if (id === null) return { ok: false, reason: 'invalid' }
  try {
    const target = localStorageOrThrow(storage)
    const matching: string[] = []
    for (let index = 0; index < target.length; index += 1) {
      const key = target.key(index)
      if (key?.startsWith(HISTORY_PREFIX) && normalizeSessionId(key.slice(HISTORY_PREFIX.length)) === id) matching.push(key)
    }
    for (const key of matching) target.removeItem(key)
    return { ok: true, reason: null }
  } catch {
    return { ok: false, reason: 'storage' }
  }
}

export function clearRememberedHistory(storage?: HistoryStorage): HistoryStorageResult {
  try {
    const target = localStorageOrThrow(storage)
    const keys: string[] = []
    for (let index = 0; index < target.length; index += 1) {
      const key = target.key(index)
      if (key?.startsWith(HISTORY_PREFIX)) keys.push(key)
    }
    for (const key of keys) target.removeItem(key)
    return { ok: true, reason: null }
  } catch {
    return { ok: false, reason: 'storage' }
  }
}

export function isHistoryStorageEvent(event: StorageEvent): boolean {
  try {
    return event.storageArea === window.localStorage && (event.key === null || event.key.startsWith(HISTORY_PREFIX))
  } catch {
    return false
  }
}
