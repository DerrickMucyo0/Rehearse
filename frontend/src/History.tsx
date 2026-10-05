import { useState } from 'react'
import { useHistoryHydration } from './useHistoryHydration'
import type { SharedHistoryHydration } from './useHistoryHydration'
import SessionDetail, { SessionFacts } from './SessionDetail'

interface Props {
  sessionIds: string[]
  storageError: string | null
  onRemove: (id: string) => void
  onClear: () => void
  onPractice: () => void
  hydration?: SharedHistoryHydration
}

export default function History(props: Props) {
  // A different registry starts a fresh cancellable read generation and drops
  // any detail cache belonging to its previous membership.
  return props.hydration
    ? <HistoryView key={props.sessionIds.join('\n')} {...props} hydration={props.hydration} />
    : <StandaloneHistory key={props.sessionIds.join('\n')} {...props} />
}

function StandaloneHistory(props: Props) {
  const hydration = useHistoryHydration(props.sessionIds, 0, true)
  return <HistoryView {...props} hydration={hydration} />
}

function HistoryView({ sessionIds, storageError, onRemove, onClear, onPractice, hydration }: Props & { hydration: SharedHistoryHydration }) {
  const { history } = hydration
  const [selectedId, setSelectedId] = useState<string | null>(null)

  function remove(id: string) {
    if (selectedId === id) setSelectedId(null)
    onRemove(id)
  }

  function clear() {
    if (!window.confirm('Clear sessions remembered on this browser?\n\nThis removes the local history list. It does not delete sessions stored on the server.')) return
    hydration.discard()
    setSelectedId(null)
    onClear()
  }

  const currentIds = new Set(sessionIds)
  const summaries = history.summaries.filter((summary) => currentIds.has(summary.session_id))
  const missingIds = history.missingIds.filter((id) => currentIds.has(id))
  const selection = selectedId !== null && currentIds.has(selectedId) ? selectedId : null
  if (selection !== null) return <SessionDetail key={selection} sessionId={selection}
    onBack={() => setSelectedId(null)} onRemove={remove} />

  return <section className="history-list" aria-label="Remembered session history" aria-busy={history.status === 'loading'}>
    <h2>History</h2>
    <p>Sessions remembered on this browser. Removing them from this list does not delete sessions stored on the server.</p>
    {storageError && <p role="status">{storageError}</p>}
    {sessionIds.length === 0 && <>
      <p>No sessions are remembered on this browser yet.</p>
      <button type="button" onClick={onPractice}>Practice</button>
    </>}
    {sessionIds.length > 0 && <div className="attempt-actions">
      <button type="button" disabled={history.status === 'loading'} onClick={hydration.reload}>Reload history</button>
      <button type="button" onClick={clear}>Clear remembered history</button>
    </div>}
    {history.status === 'loading' && sessionIds.length > 0 && <p role="status">Loading remembered sessions…</p>}
    {(history.status === 'partial' || history.status === 'error' || history.failedChunks.length > 0) && <div>
      <p role="alert">{summaries.length > 0 || missingIds.length > 0
        ? 'Some remembered sessions could not be loaded. These results are incomplete.'
        : 'Remembered sessions could not be loaded.'}</p>
      <button type="button" disabled={history.status === 'loading'} onClick={hydration.retry}>
        Retry failed history requests
      </button>
    </div>}
    {summaries.map((summary) => <article className="history-summary" key={summary.session_id}>
      <h3>{summary.status === 'active' ? 'Active' : 'Completed'} session</h3>
      <SessionFacts summary={summary} />
      <div className="attempt-actions">
        <button type="button" onClick={() => setSelectedId(summary.session_id)}>Open session</button>
        <button type="button" onClick={() => remove(summary.session_id)}>Remove from this browser</button>
      </div>
    </article>)}
    {missingIds.length > 0 && <section aria-label="Unavailable remembered sessions">
      <h3>Unavailable remembered sessions</h3>
      {missingIds.map((id, index) => <article className="history-summary" key={id}>
        <p>Unavailable remembered session {index + 1}</p>
        <button type="button" onClick={() => remove(id)}>Remove from this browser</button>
      </article>)}
    </section>}
  </section>
}
