import { useState } from 'react'
import { useHistoryHydration } from './useHistoryHydration'
import type { SharedHistoryHydration } from './useHistoryHydration'
import SessionDetail, { SessionFacts } from './SessionDetail'

interface Props {
  onPractice: () => void
  hydration?: SharedHistoryHydration
}

export default function History(props: Props) {
  return props.hydration
    ? <HistoryView {...props} hydration={props.hydration} />
    : <StandaloneHistory {...props} />
}

function StandaloneHistory(props: Props) {
  const hydration = useHistoryHydration(0, true)
  return <HistoryView {...props} hydration={hydration} />
}

function HistoryView({ onPractice, hydration }: Props & { hydration: SharedHistoryHydration }) {
  const { history } = hydration
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selection = selectedId !== null && history.summaries.some((item) => item.session_id === selectedId) ? selectedId : null
  if (selection !== null) return <SessionDetail key={selection} sessionId={selection} onBack={() => setSelectedId(null)} />

  return <section className="history-list" aria-label="Session history" aria-busy={history.status === 'loading'}>
    <h2>History</h2>
    <p>Saved sessions for your current sign-in.</p>
    {history.status === 'complete' && history.summaries.length === 0 && <>
      <p>No saved sessions yet.</p>
      <button type="button" onClick={onPractice}>Practice</button>
    </>}
    <div className="attempt-actions">
      <button type="button" disabled={history.status === 'loading'} onClick={hydration.reload}>Reload history</button>
    </div>
    {history.status === 'loading' && <p role="status">Loading saved sessions…</p>}
    {history.pageError && <div>
      <p role="alert">{history.summaries.length > 0
        ? 'More saved sessions could not be loaded. These results are incomplete.'
        : 'Saved sessions could not be loaded.'}</p>
      <button type="button" disabled={history.status === 'loading'} onClick={hydration.retry}>Retry history request</button>
    </div>}
    {history.summaries.map((summary) => <article className="history-summary" key={summary.session_id}>
      <h3>{summary.status === 'active' ? 'Active' : 'Completed'} session</h3>
      <SessionFacts summary={summary} />
      <button type="button" onClick={() => setSelectedId(summary.session_id)}>Open session</button>
    </article>)}
    {hydration.canLoadMore && <button type="button" onClick={hydration.loadMore}>Load more sessions</button>}
  </section>
}
