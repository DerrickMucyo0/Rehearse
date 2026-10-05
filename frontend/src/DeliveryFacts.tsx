import { deliveryUnavailableText, formatDeliveryDuration, TIMED_PAUSES_EXPLANATION, TIMED_PAUSES_LIMITATION } from './deliveryMetrics'
import type { DeliveryMetrics } from './deliveryMetrics'

export default function DeliveryFacts({ metrics, headingLevel = 4 }: { metrics: DeliveryMetrics | null; headingLevel?: 4 | 6 }) {
  const unavailable = metrics === null ? 'Not recorded' : 'Unavailable'
  return <section className="delivery-facts" aria-label="Timed pauses">
    {headingLevel === 6 ? <h6>Timed pauses</h6> : <h4>Timed pauses</h4>}
    <p>{TIMED_PAUSES_EXPLANATION}</p>
    <p>{TIMED_PAUSES_LIMITATION}</p>
    {metrics !== null && <>
      <p>Measurement version: {metrics.version}</p>
      <p>Source: {metrics.source === 'original_transcription' ? 'Original transcription' : metrics.source}</p>
    </>}
    <dl>
      <dt>Pause count</dt><dd>{metrics?.pause_count ?? unavailable}</dd>
      <dt>Total pause time</dt><dd>{metrics?.total_pause_duration_seconds === null || metrics === null
        ? unavailable : formatDeliveryDuration(metrics.total_pause_duration_seconds)}</dd>
      <dt>Longest pause</dt><dd>{metrics?.longest_pause_seconds === null || metrics === null
        ? unavailable : formatDeliveryDuration(metrics.longest_pause_seconds)}</dd>
    </dl>
    {metrics?.unavailable_reason && <p>Unavailable — {deliveryUnavailableText(metrics.unavailable_reason)}</p>}
  </section>
}
