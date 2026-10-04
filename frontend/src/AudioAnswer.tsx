import { useEffect, useRef, useState } from 'react'
import { transcribeAudio, uploadAudio } from './interviewApi'
import type { InterviewSession, SpeakingMetrics } from './interviewApi'
import { useAudioRecorder } from './useAudioRecorder'

interface Props {
  session: InterviewSession
  disabled: boolean
  hasAnswer: boolean
  onTranscript: (text: string) => void
  onTranscribing: (busy: boolean) => void
}

export default function AudioAnswer({ session, disabled, hasAnswer, onTranscript, onTranscribing }: Props) {
  const recording = useAudioRecorder()
  const [uploadState, setUploadState] = useState<'idle' | 'uploading' | 'accepted' | 'transcribing' | 'transcribed' | 'error'>('idle')
  const [error, setError] = useState('')
  // One owner, tied to the original Blob; edited answer text is never an input.
  const [measurements, setMeasurements] = useState<{ blob: Blob; metrics: SpeakingMetrics } | null>(null)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => {
    controller.current?.abort()
    onTranscribing(false)
  }, [onTranscribing])
  const pendingRequest = uploadState === 'uploading' || uploadState === 'transcribing'
  const metrics = measurements && measurements.blob === recording.blob ? measurements.metrics : null

  async function transcribe() {
    const audio = recording.blob
    if (!audio || controller.current || disabled || hasAnswer) return
    const pending = new AbortController()
    controller.current = pending
    setUploadState('transcribing')
    onTranscribing(true)
    setError('')
    try {
      const result = await transcribeAudio(session, audio, pending.signal)
      if (!pending.signal.aborted) {
        onTranscript(result.text)
        setMeasurements({ blob: audio, metrics: result.metrics })
        setUploadState('transcribed')
      }
    } catch (cause) {
      if (!pending.signal.aborted) {
        setUploadState('error')
        setError(cause instanceof Error ? cause.message : 'Transcription failed. Please try again.')
      }
    } finally {
      if (!pending.signal.aborted) onTranscribing(false)
      if (controller.current === pending) controller.current = null
    }
  }

  async function send() {
    if (!recording.blob || controller.current || disabled) return
    const pending = new AbortController()
    controller.current = pending
    setUploadState('uploading')
    setError('')
    try {
      await uploadAudio(session, recording.blob, pending.signal)
      if (!pending.signal.aborted) setUploadState('accepted')
    } catch (cause) {
      if (!pending.signal.aborted) {
        setUploadState('error')
        setError(cause instanceof Error ? cause.message : 'Audio upload failed. Please try again.')
      }
    } finally {
      if (controller.current === pending) controller.current = null
    }
  }

  return (
    <section aria-label="Record an audio answer">
      <p>Record an answer (up to 5 minutes / 10 MiB). Transcribe Recording sends audio to ElevenLabs.
        Review and edit the transcript, then Submit Answer to continue.</p>
      <button type="button" disabled={disabled || pendingRequest || ['requesting', 'recording', 'stopping'].includes(recording.state)}
        onClick={() => { setMeasurements(null); setUploadState('idle'); setError(''); void recording.start() }}>
        Record Answer
      </button>
      {recording.state === 'recording' && <button type="button" onClick={recording.stop}>Stop Recording</button>}
      <p role="status">
        {recording.state === 'requesting' && 'Waiting for microphone permission…'}
        {recording.state === 'recording' && 'Recording… Microphone is active.'}
        {recording.state === 'stopping' && 'Finishing recording…'}
        {recording.state === 'ready' && uploadState === 'idle' && 'Recording stopped. Ready to send.'}
        {uploadState === 'uploading' && 'Uploading recording…'}
        {uploadState === 'transcribing' && 'Transcribing recording…'}
        {uploadState === 'transcribed' && 'Transcript ready. Review and edit your answer before submitting.'}
        {uploadState === 'accepted' && 'Recording accepted. It was not saved or transcribed. Submit a typed answer to continue.'}
      </p>
      {recording.blob && uploadState !== 'accepted' && (
        <button type="button" disabled={disabled || pendingRequest} onClick={() => void send()}>Send Recording</button>
      )}
      {recording.blob && (
        <button type="button" disabled={disabled || pendingRequest || hasAnswer} onClick={() => void transcribe()}>
          Transcribe Recording
        </button>
      )}
      {hasAnswer && <p>Clear your answer before transcribing to avoid replacing your text.</p>}
      {(recording.error || error) && <p role="alert">{recording.error || error}</p>}
      {metrics && (
        <section className="speaking-measurements" aria-label="Speaking measurements">
          <h3>Speaking measurements</h3>
          <p>Based on your original recording. Editing the transcript won’t change these measurements.</p>
          <dl>
            <dt>Words</dt><dd>{metrics.recognized_word_count}</dd>
            <dt>Um</dt><dd>{metrics.um_count ?? 'Unavailable'}</dd>
            <dt>Uh</dt><dd>{metrics.uh_count ?? 'Unavailable'}</dd>
            <dt>Timed speech span</dt><dd>{metrics.timed_utterance_span_seconds === null
              ? 'Unavailable' : `${Number(metrics.timed_utterance_span_seconds.toFixed(1))} sec`}</dd>
            <dt>Estimated WPM</dt><dd>{metrics.estimated_words_per_minute === null
              ? 'Unavailable' : `${Math.round(metrics.estimated_words_per_minute)} WPM`}</dd>
          </dl>
          {metrics.filler_unavailable_reason && <p>Um/uh counts unavailable for this language.</p>}
          {metrics.timing_unavailable_reason && <p>{metrics.timing_unavailable_reason === 'timing_coverage_mismatch'
            ? 'Timing measurements aren’t available because complete word timing wasn’t available.'
            : 'Timing measurements aren’t available for this transcription.'}</p>}
        </section>
      )}
    </section>
  )
}
