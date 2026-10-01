import { useEffect, useRef, useState } from 'react'
import { uploadAudio } from './interviewApi'
import type { InterviewSession } from './interviewApi'
import { useAudioRecorder } from './useAudioRecorder'

export default function AudioAnswer({ session, disabled }: { session: InterviewSession; disabled: boolean }) {
  const recording = useAudioRecorder()
  const [uploadState, setUploadState] = useState<'idle' | 'uploading' | 'accepted' | 'error'>('idle')
  const [error, setError] = useState('')
  const controller = useRef<AbortController | null>(null)
  useEffect(() => () => controller.current?.abort(), [])

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
      <p>Record an answer (up to 5 minutes / 10 MiB). Audio is discarded after validation.
        It is not transcribed and does not advance the question.</p>
      <button type="button" disabled={disabled || uploadState === 'uploading' || ['requesting', 'recording', 'stopping'].includes(recording.state)}
        onClick={() => { setUploadState('idle'); setError(''); void recording.start() }}>
        Record Answer
      </button>
      {recording.state === 'recording' && <button type="button" onClick={recording.stop}>Stop Recording</button>}
      <p role="status">
        {recording.state === 'requesting' && 'Waiting for microphone permission…'}
        {recording.state === 'recording' && 'Recording… Microphone is active.'}
        {recording.state === 'stopping' && 'Finishing recording…'}
        {recording.state === 'ready' && uploadState === 'idle' && 'Recording stopped. Ready to send.'}
        {uploadState === 'uploading' && 'Uploading recording…'}
        {uploadState === 'accepted' && 'Recording accepted. It was not saved or transcribed. Submit a typed answer to continue.'}
      </p>
      {recording.blob && uploadState !== 'accepted' && (
        <button type="button" disabled={disabled || uploadState === 'uploading'} onClick={() => void send()}>Send Recording</button>
      )}
      {(recording.error || error) && <p role="alert">{recording.error || error}</p>}
    </section>
  )
}
