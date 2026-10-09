import { useEffect, useId, useState } from 'react'
import type { Attempt, AttemptComparison as Comparison, ComparisonMetrics, DeliveryComparison, DeliverySideReason, MetricChange } from './interviewApi'
import { deliveryUnavailableText, formatDeliveryDuration, TIMED_PAUSES_EXPLANATION, TIMED_PAUSES_LIMITATION } from './deliveryMetrics'

type SideReason = NonNullable<MetricChange['before_unavailable_reason']>
type ComparisonReason = NonNullable<MetricChange['comparison_unavailable_reason']>

const sideReasons: Record<SideReason, string> = {
  no_measurement: 'No linked speaking measurement.',
  unsupported_language: 'Filler counts are unavailable for this language.',
  missing_timings: 'Word timings were not available.',
  timing_coverage_mismatch: 'Word timings do not cover the recognized words.',
  invalid_timing: 'Word timings were not usable.',
  invalid_timing_order: 'Word timings were not in a usable order.',
  unusable_span: 'A speaking duration could not be calculated from the word timings.',
}

const comparisonReasons: Record<ComparisonReason, string> = {
  measurement_version_mismatch: 'Measurement versions differ.',
  measurement_source_incompatible: 'Measurement sources are incompatible.',
  before_unavailable: 'The before measurement is unavailable.',
  after_unavailable: 'The after measurement is unavailable.',
  both_unavailable: 'Both measurements are unavailable.',
}

const metrics: { key: keyof ComparisonMetrics; label: string; decimals: 0 | 1 }[] = [
  { key: 'recognized_word_count', label: 'Recognized words', decimals: 0 },
  { key: 'um_count', label: 'Um', decimals: 0 },
  { key: 'uh_count', label: 'Uh', decimals: 0 },
  { key: 'timed_utterance_span_seconds', label: 'Speaking duration', decimals: 1 },
  { key: 'estimated_words_per_minute', label: 'Words per minute', decimals: 1 },
]
const noAttempts: Attempt[] = []

function formatValue(value: number, decimals: 0 | 1): string {
  return value.toFixed(decimals)
}

function formatDelta(value: number, decimals: 0 | 1): string {
  if (value === 0) return '0'
  // Keep the original delta's sign even when its displayed magnitude rounds to zero.
  return `${value > 0 ? '+' : '-'}${formatValue(Math.abs(value), decimals)}`
}

function MeasurementCell({ value, reason, decimals }: {
  value: number | null
  reason: MetricChange['before_unavailable_reason']
  decimals: 0 | 1
}) {
  return <td>
    {value === null ? <>
      Unavailable
      {reason !== null && <small>{sideReasons[reason]}</small>}
    </> : formatValue(value, decimals)}
  </td>
}

const deliveryMetrics: { key: keyof Pick<DeliveryComparison, 'pause_count' | 'total_pause_duration_seconds' | 'longest_pause_seconds'>; label: string; duration: boolean }[] = [
  { key: 'pause_count', label: 'Pause count', duration: false },
  { key: 'total_pause_duration_seconds', label: 'Total pause time', duration: true },
  { key: 'longest_pause_seconds', label: 'Longest pause', duration: true },
]

function DeliveryCell({ value, reason, duration }: { value: number | null; reason: DeliverySideReason | null; duration: boolean }) {
  return <td>{value !== null ? duration ? formatDeliveryDuration(value) : value : reason === 'not_recorded' ? 'Not recorded' : <>
    Unavailable
    {reason !== null && <small>{reason === 'no_measurement' ? 'No linked measurement.' : deliveryUnavailableText(reason)}</small>}
  </>}</td>
}

function deliveryDelta(value: number, duration: boolean): string {
  if (value === 0) return duration ? '0.0 s' : '0'
  return `${value > 0 ? '+' : '-'}${duration ? formatDeliveryDuration(Math.abs(value)) : Math.abs(value)}`
}

interface Props {
  comparison: Comparison
  attempts?: Attempt[]
  onCompare?: (before: number, after: number) => void
  isLoading?: boolean
  error?: string
}

export default function AttemptComparison({ comparison, attempts = noAttempts, onCompare, isLoading = false, error = '' }: Props) {
  const headingId = useId()
  const data = comparison.comparison
  const [beforeAttemptNumber, setBeforeAttemptNumber] = useState<number | null>(
    comparison.before_attempt?.attempt_number ?? attempts[0]?.attempt_number ?? null,
  )
  const [afterAttemptNumber, setAfterAttemptNumber] = useState<number | null>(
    comparison.after_attempt?.attempt_number ?? attempts.at(-1)?.attempt_number ?? null,
  )

  useEffect(() => {
    setBeforeAttemptNumber(comparison.before_attempt?.attempt_number ?? attempts[0]?.attempt_number ?? null)
    setAfterAttemptNumber(comparison.after_attempt?.attempt_number ?? attempts.at(-1)?.attempt_number ?? null)
  }, [comparison.before_attempt?.attempt_number, comparison.after_attempt?.attempt_number, attempts])

  if (data === null) return null

  const beforeOptions = attempts.filter((attempt) => afterAttemptNumber !== null && attempt.attempt_number < afterAttemptNumber)
  const afterOptions = attempts.filter((attempt) => beforeAttemptNumber !== null && attempt.attempt_number > beforeAttemptNumber)
  const hasValidSelection = beforeAttemptNumber !== null && afterAttemptNumber !== null && beforeAttemptNumber < afterAttemptNumber
  const selectionChanged = hasValidSelection && (
    beforeAttemptNumber !== comparison.before_attempt?.attempt_number ||
    afterAttemptNumber !== comparison.after_attempt?.attempt_number
  )

  return <section className="attempt-comparison" aria-labelledby={headingId}>
    <h3 id={headingId}>Before / After comparison</h3>
    <p>
      Before: Attempt {comparison.before_attempt?.attempt_number}.
      {' '}After: Attempt {comparison.after_attempt?.attempt_number}.
    </p>
    {onCompare && attempts.length > 1 && <div className="comparison-controls" aria-busy={isLoading}>
      <fieldset disabled={isLoading}>
        <legend>Choose attempts to compare</legend>
        <div className="comparison-selects">
          <label htmlFor={`${headingId}-before`}>Before attempt
            <select id={`${headingId}-before`} value={beforeAttemptNumber ?? ''}
              onChange={(event) => setBeforeAttemptNumber(Number(event.target.value))}>
              {beforeOptions.map((attempt) => <option key={attempt.id} value={attempt.attempt_number}>
                Attempt {attempt.attempt_number}{attempt.measurement_id ? ' (speaking metrics available)' : ' (no speaking measurement)'}
              </option>)}
            </select>
          </label>
          <label htmlFor={`${headingId}-after`}>After attempt
            <select id={`${headingId}-after`} value={afterAttemptNumber ?? ''}
              onChange={(event) => setAfterAttemptNumber(Number(event.target.value))}>
              {afterOptions.map((attempt) => <option key={attempt.id} value={attempt.attempt_number}>
                Attempt {attempt.attempt_number}{attempt.measurement_id ? ' (speaking metrics available)' : ' (no speaking measurement)'}
              </option>)}
            </select>
          </label>
        </div>
      </fieldset>
      <button type="button" onClick={() => {
        if (beforeAttemptNumber !== null && afterAttemptNumber !== null && beforeAttemptNumber < afterAttemptNumber) {
          onCompare(beforeAttemptNumber, afterAttemptNumber)
        }
      }} disabled={!selectionChanged || isLoading || !hasValidSelection}>
        {isLoading ? 'Comparing…' : 'Compare attempts'}
      </button>
      {error && <p role="alert">{error}</p>}
    </div>}
    <table className="comparison-table">
      <caption>Speaking duration is shown in seconds.</caption>
      <thead><tr>
        <th scope="col">Metric</th>
        <th scope="col">Before</th>
        <th scope="col">After</th>
        <th scope="col">Change</th>
      </tr></thead>
      <tbody>{metrics.map(({ key, label, decimals }) => {
        const metric = data[key]
        return <tr key={key}>
          <th scope="row">{label}</th>
          <MeasurementCell value={metric.before} reason={metric.before_unavailable_reason} decimals={decimals} />
          <MeasurementCell value={metric.after} reason={metric.after_unavailable_reason} decimals={decimals} />
          <td>{metric.comparable && metric.delta !== null ? formatDelta(metric.delta, decimals) : <>
            Unavailable
            {metric.comparison_unavailable_reason !== null && <small>
              {comparisonReasons[metric.comparison_unavailable_reason]}
            </small>}
          </>}</td>
        </tr>
      })}</tbody>
    </table>
    {comparison.delivery_comparison !== null && <section aria-label="Timed pauses comparison">
      <h4>Timed pauses</h4>
      <p>{TIMED_PAUSES_EXPLANATION}</p>
      <p>{TIMED_PAUSES_LIMITATION}</p>
      <table className="comparison-table">
        <caption>Timed pauses: durations are shown in seconds.</caption>
        <thead><tr><th scope="col">Metric</th><th scope="col">Before</th><th scope="col">After</th><th scope="col">Change</th></tr></thead>
        <tbody>{deliveryMetrics.map(({ key, label, duration }) => {
          const metric = comparison.delivery_comparison![key]
          return <tr key={key}>
            <th scope="row">{label}</th>
            <DeliveryCell value={metric.before} reason={metric.before_unavailable_reason} duration={duration} />
            <DeliveryCell value={metric.after} reason={metric.after_unavailable_reason} duration={duration} />
            <td>{metric.comparable && metric.delta !== null ? deliveryDelta(metric.delta, duration) : <>
              Unavailable
              {metric.comparison_unavailable_reason !== null && <small>{comparisonReasons[metric.comparison_unavailable_reason]}</small>}
            </>}</td>
          </tr>
        })}</tbody>
      </table>
    </section>}
  </section>
}
