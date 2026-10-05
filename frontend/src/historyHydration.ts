import { getHistorySummaries, HistoryApiError } from './historyApi'
import type { HistorySummary } from './historyApi'
import { normalizeSessionId } from './historyStorage'

export type HistoryHydrationStatus = 'idle' | 'loading' | 'complete' | 'partial' | 'error'
export interface HistoryHydrationState {
  status: HistoryHydrationStatus
  summaries: HistorySummary[]
  missingIds: string[]
  failedChunks: string[][]
  rememberedCount: number
}
export interface HistoryHydrationOptions {
  signal?: AbortSignal
  previous?: HistoryHydrationState
  retryChunks?: readonly (readonly string[])[]
}

function uniqueIds(values: readonly unknown[]): string[] {
  return [...new Set(values.map(normalizeSessionId).filter((id): id is string => id !== null))]
}
function chunks(ids: string[]): string[][] {
  const result: string[][] = []
  for (let index = 0; index < ids.length; index += 50) result.push(ids.slice(index, index + 50))
  return result
}
function checkCancellation(signal?: AbortSignal): void {
  if (signal?.aborted) throw new HistoryApiError('History request cancelled.', null, true)
}
export function sortHistorySummaries(summaries: readonly HistorySummary[]): HistorySummary[] {
  return [...summaries].sort((left, right) => {
    const milliseconds = Date.parse(right.last_saved_activity_at) - Date.parse(left.last_saved_activity_at)
    if (milliseconds !== 0) return milliseconds
    // PostgreSQL timestamps retain microseconds; Date.parse retains milliseconds.
    const fractional = (value: string) => (/\.(\d+)(?:Z|[+-]\d{2}:\d{2})$/.exec(value)?.[1].slice(3) ?? '').padEnd(9, '0')
    const leftFraction = fractional(left.last_saved_activity_at)
    const rightFraction = fractional(right.last_saved_activity_at)
    if (leftFraction !== rightFraction) return leftFraction < rightFraction ? 1 : -1
    return left.session_id < right.session_id ? -1 : left.session_id > right.session_id ? 1 : 0
  })
}

export async function hydrateHistory(values: readonly unknown[], options: HistoryHydrationOptions = {}): Promise<HistoryHydrationState> {
  checkCancellation(options.signal)
  const ids = uniqueIds(values)
  const known = new Set(ids)
  const retrying = options.retryChunks !== undefined && options.previous !== undefined
  const requested = retrying
    ? uniqueIds(options.retryChunks!.flat()).filter((id) => known.has(id))
    : ids
  const retried = new Set(requested)
  const summaries = new Map<string, HistorySummary>()
  const missing = new Set<string>()
  const failures: string[][] = []
  if (retrying) {
    for (const item of options.previous!.summaries) if (known.has(item.session_id) && !retried.has(item.session_id)) summaries.set(item.session_id, item)
    for (const id of options.previous!.missingIds) if (known.has(id) && !retried.has(id)) missing.add(id)
    for (const failed of options.previous!.failedChunks) {
      const retained = failed.filter((id) => known.has(id) && !retried.has(id))
      if (retained.length > 0) failures.push(retained)
    }
  }
  const requests = chunks(requested)
  const outcomes: ({ summaries: HistorySummary[]; missing_session_ids: string[] } | null)[] = Array.from({ length: requests.length }, () => null)
  let next = 0
  async function worker(): Promise<void> {
    while (next < requests.length) {
      checkCancellation(options.signal)
      const index = next++
      try {
        outcomes[index] = await getHistorySummaries(requests[index], { signal: options.signal })
      } catch (error) {
        if (error instanceof HistoryApiError && error.cancelled) throw error
        checkCancellation(options.signal)
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, requests.length) }, () => worker()))
  checkCancellation(options.signal)
  outcomes.forEach((outcome, index) => {
    if (outcome === null) failures.push(requests[index])
    else {
      for (const item of outcome.summaries) summaries.set(item.session_id, item)
      for (const id of outcome.missing_session_ids) missing.add(id)
    }
  })
  // IDs added while a failed batch is being retried still need their own read.
  const covered = new Set([...summaries.keys(), ...missing, ...failures.flat()])
  const uncovered = ids.filter((id) => !covered.has(id))
  if (uncovered.length > 0) failures.push(...chunks(uncovered))
  return {
    status: failures.length === 0 ? 'complete' : summaries.size + missing.size > 0 ? 'partial' : 'error',
    summaries: sortHistorySummaries([...summaries.values()]),
    missingIds: ids.filter((id) => missing.has(id)),
    failedChunks: failures,
    rememberedCount: ids.length,
  }
}
