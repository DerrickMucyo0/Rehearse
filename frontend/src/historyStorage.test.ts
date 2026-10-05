// @vitest-environment jsdom
import { afterEach, expect, test, vi } from 'vitest'
import { addRememberedSession, clearRememberedHistory, HISTORY_PREFIX, isHistoryStorageEvent, listRememberedSessions, normalizeSessionId, removeRememberedSession } from './historyStorage'
import type { HistoryStorage } from './historyStorage'

const first = 'aabbccdd-0011-2233-4455-66778899aabb'
const second = 'aabbccdd-0011-2233-4455-66778899aabc'
const id = (index: number) => `00000000-0000-0000-0000-${index.toString(16).padStart(12, '0')}`
class MemoryStorage implements HistoryStorage {
  readonly data = new Map<string, string>()
  fail: 'length' | 'key' | 'get' | 'set' | 'remove' | null = null
  get length() { if (this.fail === 'length') throw new Error('private browser policy'); return this.data.size }
  key(index: number) { if (this.fail === 'key') throw new Error('private browser policy'); return [...this.data.keys()][index] ?? null }
  getItem(key: string) { if (this.fail === 'get') throw new Error('private browser policy'); return this.data.get(key) ?? null }
  setItem(key: string, value: string) { if (this.fail === 'set') throw new Error('private quota'); this.data.set(key, value) }
  removeItem(key: string) { if (this.fail === 'remove') throw new Error('private browser policy'); this.data.delete(key) }
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

test('empty storage has no remembered sessions', () => {
  expect(listRememberedSessions(new MemoryStorage())).toEqual({ ids: [], error: null })
})
test('canonicalizes UUIDs without accepting malformed or non-string input', () => {
  expect(normalizeSessionId(first.toUpperCase())).toBe(first)
  for (const value of [null, 42, {}, '', ` ${first}`, first.replaceAll('-', ''), '{' + first + '}', 'not-a-session']) expect(normalizeSessionId(value)).toBeNull()
})
test('registers canonical sentinel-only keys, idempotently, without overwriting other IDs', () => {
  const storage = new MemoryStorage()
  expect(addRememberedSession(first.toUpperCase(), storage)).toEqual({ ok: true, reason: null })
  expect(addRememberedSession(first, storage)).toEqual({ ok: true, reason: null })
  expect(addRememberedSession(second, storage)).toEqual({ ok: true, reason: null })
  expect([...storage.data]).toEqual([[HISTORY_PREFIX + first, '1'], [HISTORY_PREFIX + second, '1']])
  expect(listRememberedSessions(storage).ids).toEqual([first, second])
})
test('removes one canonical ID and its case variants while leaving other namespaces untouched', () => {
  const storage = new MemoryStorage()
  storage.setItem(HISTORY_PREFIX + first.toUpperCase(), '1')
  addRememberedSession(second, storage)
  storage.setItem('practice-session', first)
  expect(removeRememberedSession(first, storage)).toEqual({ ok: true, reason: null })
  expect(listRememberedSessions(storage).ids).toEqual([second])
  expect(storage.getItem('practice-session')).toBe(first)
})
test('clear removes only the versioned namespace, including malformed entries, and preserves Practice', () => {
  localStorage.setItem(HISTORY_PREFIX + first, '1')
  localStorage.setItem(HISTORY_PREFIX + 'malformed', 'private invalid metadata')
  localStorage.setItem('rehearse.history.v2:other', 'leave alone')
  localStorage.setItem('unrelated-setting', 'leave alone')
  sessionStorage.setItem('rehearse.currentSession', first)
  expect(clearRememberedHistory()).toEqual({ ok: true, reason: null })
  expect(listRememberedSessions()).toEqual({ ids: [], error: null })
  expect(localStorage.getItem('unrelated-setting')).toBe('leave alone')
  expect(localStorage.getItem('rehearse.history.v2:other')).toBe('leave alone')
  expect(sessionStorage.getItem('rehearse.currentSession')).toBe(first)
})
test('ignores malformed keys and non-sentinel values without reading metadata into history', () => {
  const storage = new MemoryStorage()
  storage.setItem(HISTORY_PREFIX + 'malformed', '1')
  storage.setItem(HISTORY_PREFIX + first, '{"answer":"private","metrics":100}')
  storage.setItem(HISTORY_PREFIX + second.toUpperCase(), '1')
  storage.setItem('unrelated-key', 'private answer')
  expect(listRememberedSessions(storage)).toEqual({ ids: [second], error: null })
  expect(storage.data.size).toBe(4)
})
test('accepts 500 IDs, reports the 501st without eviction, and keeps duplicate adds successful', () => {
  const storage = new MemoryStorage()
  for (let index = 1; index <= 500; index += 1) expect(addRememberedSession(id(index), storage).ok).toBe(true)
  expect(addRememberedSession(id(501), storage)).toEqual({ ok: false, reason: 'capacity' })
  expect(addRememberedSession(id(1).toUpperCase(), storage)).toEqual({ ok: true, reason: null })
  expect(listRememberedSessions(storage).ids).toHaveLength(500)
  expect(storage.getItem(HISTORY_PREFIX + id(1))).toBe('1')
  expect(storage.getItem(HISTORY_PREFIX + id(501))).toBeNull()
})
test.each(['length', 'key', 'get'] as const)('handles storage %s read failure with a fixed message', (failure) => {
  const storage = new MemoryStorage()
  addRememberedSession(first, storage)
  storage.fail = failure
  expect(listRememberedSessions(storage)).toEqual({ ids: [], error: 'History could not be saved in this browser.' })
  expect(addRememberedSession(second, storage)).toEqual({ ok: false, reason: 'storage' })
})
test('set failure is a non-throwing result that does not change existing registry entries', () => {
  const storage = new MemoryStorage()
  addRememberedSession(first, storage)
  storage.fail = 'set'
  expect(addRememberedSession(second, storage)).toEqual({ ok: false, reason: 'storage' })
  expect(storage.data.has(HISTORY_PREFIX + first)).toBe(true)
})
test.each(['remove', 'clear'] as const)('handles %s failure without deleting unrelated data', (operation) => {
  const storage = new MemoryStorage()
  addRememberedSession(first, storage)
  storage.setItem('unrelated', 'preserved')
  storage.fail = 'remove'
  expect(operation === 'remove' ? removeRememberedSession(first, storage) : clearRememberedHistory(storage)).toEqual({ ok: false, reason: 'storage' })
  expect(storage.data.get('unrelated')).toBe('preserved')
})
test('blocked access to the default localStorage is handled safely', () => {
  vi.spyOn(window, 'localStorage', 'get').mockImplementation(() => { throw new Error('private security exception') })
  expect(listRememberedSessions().error).toBe('History could not be saved in this browser.')
  expect(addRememberedSession(first)).toEqual({ ok: false, reason: 'storage' })
  expect(removeRememberedSession(first)).toEqual({ ok: false, reason: 'storage' })
  expect(clearRememberedHistory()).toEqual({ ok: false, reason: 'storage' })
  expect(isHistoryStorageEvent({ storageArea: null, key: null } as StorageEvent)).toBe(false)
})
test('recognizes only namespace and clear events from localStorage', () => {
  const event = (key: string | null, storageArea: Storage | null = localStorage) => new StorageEvent('storage', { key, storageArea })
  expect(isHistoryStorageEvent(event(HISTORY_PREFIX + first))).toBe(true)
  expect(isHistoryStorageEvent(event(HISTORY_PREFIX + 'malformed'))).toBe(true)
  expect(isHistoryStorageEvent(event(null))).toBe(true)
  expect(isHistoryStorageEvent(event('unrelated'))).toBe(false)
  expect(isHistoryStorageEvent(event(null, sessionStorage))).toBe(false)
  expect(isHistoryStorageEvent(event(HISTORY_PREFIX + first, null))).toBe(false)
})
test('invalid registration and removal never mutate storage', () => {
  const storage = new MemoryStorage()
  expect(addRememberedSession('invalid', storage)).toEqual({ ok: false, reason: 'invalid' })
  expect(removeRememberedSession('invalid', storage)).toEqual({ ok: false, reason: 'invalid' })
  expect(storage.data.size).toBe(0)
})
