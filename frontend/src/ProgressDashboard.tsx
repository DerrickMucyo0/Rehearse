import type { HistoryHydrationState } from './historyHydration'
import { DELIVERY_PROGRESS_METRICS, describeUnavailableReason, formatProgressValue, PROGRESS_METRICS, projectProgress } from './progress'
import type { DeliveryProgressMetric, ProgressGroup, ProgressMetric } from './progress'
import { deliveryUnavailableText, formatDeliveryDuration, TIMED_PAUSES_EXPLANATION, TIMED_PAUSES_LIMITATION } from './deliveryMetrics'

export interface ProgressDashboardProps {
  history: HistoryHydrationState
  onRetry: () => void
  onReload: () => void
  onPractice?: () => void
  onLoadMore?: () => void
}

function MetricTable({ metric }: { metric: ProgressMetric }) {
  return <div className="progress-table-scroll">
    <table className="progress-table">
      <caption>{metric.label} ({metric.unit})</caption>
      <thead><tr>
        <th scope="col">Date/time</th><th scope="col">Question</th>
        <th scope="col">Session status</th><th scope="col">Attempt</th>
        <th scope="col">Value ({metric.unit})</th>
      </tr></thead>
      <tbody>{metric.rows.map(({ point, value, unavailableReason }, index) => <tr key={`${point.session_id}:${point.attempt_id}:${index}`}>
        <td><time dateTime={point.submitted_at}>{new Date(point.submitted_at).toLocaleString()}</time></td>
        <td>Question {point.question_index + 1}</td>
        <td>{point.session_status === 'completed' ? 'Completed' : 'Active'}</td>
        <td>{point.attempt_number}</td>
        <td>{value === null
          ? <>Unavailable{describeUnavailableReason(unavailableReason) !== 'Unavailable' && <> — {describeUnavailableReason(unavailableReason)}</>}</>
          : formatProgressValue(value, metric.id)}</td>
      </tr>)}</tbody>
    </table>
  </div>
}

function MetricGroup({ group, metric }: { group: ProgressGroup; metric: ProgressMetric }) {
  return <div className="progress-provenance">
    {group.kind === 'measurement' ? <>
      <p>Measurement version: {group.measurementVersion}</p>
      <p>Source: {group.measurementSource === 'original_transcription' ? 'Original transcription' : group.measurementSource}</p>
    </> : <p>Finalized answers without a measurement</p>}
    <p>{metric.label} available for {metric.coverage.available} of {metric.coverage.total} finalized answers in this {group.kind === 'measurement' ? 'provenance group' : 'no-measurement group'}.</p>
    <MetricTable metric={metric} />
  </div>
}

function DeliveryMetricTable({ metric }: { metric: DeliveryProgressMetric }) {
  return <div className="progress-table-scroll">
    <table className="progress-table">
      <caption>{metric.label} ({metric.unit})</caption>
      <thead><tr>
        <th scope="col">Date/time</th><th scope="col">Question</th>
        <th scope="col">Session status</th><th scope="col">Attempt</th>
        <th scope="col">Value ({metric.unit})</th>
      </tr></thead>
      <tbody>{metric.rows.map(({ point, value, unavailableReason }, index) => <tr key={`${point.session_id}:${point.attempt_id}:${index}`}>
        <td><time dateTime={point.submitted_at}>{new Date(point.submitted_at).toLocaleString()}</time></td>
        <td>Question {point.question_index + 1}</td>
        <td>{point.session_status === 'completed' ? 'Completed' : 'Active'}</td>
        <td>{point.attempt_number}</td>
        <td>{value === null
          ? <>Unavailable{unavailableReason !== null && <> — {deliveryUnavailableText(unavailableReason)}</>}</>
          : metric.id === 'pause_count' ? String(value) : formatDeliveryDuration(value)}</td>
      </tr>)}</tbody>
    </table>
  </div>
}

export default function ProgressDashboard({ history, onRetry, onReload, onPractice, onLoadMore }: ProgressDashboardProps) {
  const progress = projectProgress(history.summaries)
  const loading = history.status === 'loading' || history.status === 'idle'
  const complete = history.status === 'complete'
  const empty = complete && history.summaries.length === 0
  const unrecordedDelivery = progress.points.filter((point) => point.measurement !== null && point.measurement.delivery_metrics === null).length
  return <section className="progress-dashboard" aria-label="Progress">
    <h2>Progress</h2>
    <p>Objective practice history from your saved sessions.</p>
    {empty ? <>
      <p>No saved sessions yet.</p>
      {onPractice && <button type="button" onClick={onPractice}>Practice</button>}
    </> : <>
      {loading && <p role="status">Loading saved sessions…</p>}
      {history.status === 'error' && <p role="alert">Saved sessions could not be loaded.</p>}
      {history.pageError && history.status !== 'error' && <p role="alert">More saved sessions could not be loaded.</p>}
      {!complete && <p>Progress totals are unavailable until all saved sessions load.</p>}
      {!loading && <div className="progress-actions">
        {history.pageError && <button type="button" onClick={onRetry}>Retry history request</button>}
        {history.nextCursor !== null && !history.pageError && onLoadMore && <button type="button" onClick={onLoadMore}>Load more sessions</button>}
        <button type="button" onClick={onReload}>Reload history</button>
      </div>}
      {complete && <section aria-label="Progress overview">
        <h3>Overview</h3>
        <dl className="progress-counts">
          <div><dt>Completed sessions</dt><dd>{progress.overview.completedSessions}</dd></div>
          <div><dt>Active sessions</dt><dd>{progress.overview.activeSessions}</dd></div>
          <div><dt>Finalized questions</dt><dd>{progress.overview.finalizedQuestions}</dd></div>
          <div><dt>Saved attempts</dt><dd>{progress.overview.savedAttempts}</dd></div>
          <div><dt>Saved retries</dt><dd>{progress.overview.savedRetries}</dd></div>
          <div><dt>Measured final answers</dt><dd>{progress.overview.measuredFinalAnswers}</dd></div>
        </dl>
      </section>}
      {progress.points.length === 0 && !loading && history.status !== 'error' && <p>No finalized question answers are available in the loaded sessions.</p>}
      {progress.points.length > 0 && <section aria-label="Finalized answer measurements">
        <h3>Measurements</h3>
        {!complete && <p>Showing finalized answers from loaded sessions only.</p>}
        <p>Each row is the final attempt of a finalized question. Measurement versions and sources are shown separately.</p>
        {PROGRESS_METRICS.map((definition) => <section key={definition.id} aria-label={definition.label}>
          <h4>{definition.label}</h4>
          {progress.groups.map((group) => <MetricGroup
            key={group.kind === 'measurement' ? JSON.stringify([group.measurementVersion, group.measurementSource]) : 'no_measurement'}
            group={group} metric={group.metrics.find((metric) => metric.id === definition.id)!} />)}
        </section>)}
        <section aria-label="Timed pauses">
          <h4>Timed pauses</h4>
          <p>{TIMED_PAUSES_EXPLANATION}</p>
          <p>{TIMED_PAUSES_LIMITATION}</p>
          {unrecordedDelivery > 0 && <p>Timed pause analysis: Not recorded for {unrecordedDelivery} finalized measured {unrecordedDelivery === 1 ? 'answer' : 'answers'}.</p>}
          {progress.deliveryGroups.length > 0 && DELIVERY_PROGRESS_METRICS.map((definition) => <section key={definition.id} aria-label={definition.label}>
            <h5>{definition.label}</h5>
            {progress.deliveryGroups.map((group) => <div className="progress-provenance" key={JSON.stringify([group.deliveryVersion, group.measurementSource])}>
              <p>Delivery measurement version: {group.deliveryVersion}</p>
              <p>Source: {group.measurementSource === 'original_transcription' ? 'Original transcription' : group.measurementSource}</p>
              <p>{definition.label} available for {group.metrics.find((metric) => metric.id === definition.id)!.coverage.available} of {group.points.length} finalized answers in this delivery provenance group.</p>
              <DeliveryMetricTable metric={group.metrics.find((metric) => metric.id === definition.id)!} />
            </div>)}
          </section>)}
        </section>
      </section>}
    </>}
  </section>
}
