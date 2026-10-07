import { getHistorySummaries, HistoryApiError } from './historyApi'
import type { HistorySummary } from './historyApi'
import { isAuthBoundaryError } from './auth'

export type HistoryHydrationStatus = 'idle' | 'loading' | 'complete' | 'partial' | 'error'
export interface HistoryHydrationState {
  status: HistoryHydrationStatus
  summaries: HistorySummary[]
  nextCursor: string | null
  pageError: boolean
}
export interface HistoryHydrationOptions {
  signal?: AbortSignal
  previous?: HistoryHydrationState
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

// Server discovery owns membership. A cursor is an in-memory continuation fact,
// never an identity or a browser-held authorization capability.
export async function hydrateHistory(options: HistoryHydrationOptions = {}): Promise<HistoryHydrationState> {
  checkCancellation(options.signal)
  const previous = options.previous
  const cursor = previous?.nextCursor ?? undefined
  if (previous && cursor === undefined && previous.status === 'complete') return previous
  try {
    const page = await getHistorySummaries({ signal: options.signal, ...(cursor === undefined ? {} : { cursor }) })
    checkCancellation(options.signal)
    if (cursor !== undefined && page.next_cursor === cursor) throw new HistoryApiError('Unexpected history response. Please try again.')
    const summaries = new Map((previous?.summaries ?? []).map((item) => [item.session_id, item]))
    for (const item of page.items) summaries.set(item.session_id, item)
    return {
      status: page.next_cursor === null ? 'complete' : 'partial',
      summaries: sortHistorySummaries([...summaries.values()]),
      nextCursor: page.next_cursor,
      pageError: false,
    }
  } catch (error) {
    if (isAuthBoundaryError(error) || (error instanceof HistoryApiError && error.cancelled)) throw error
    checkCancellation(options.signal)
    return { status: previous?.summaries.length ? 'partial' : 'error', summaries: previous?.summaries ?? [],
      nextCursor: cursor ?? null, pageError: true }
  }
}
