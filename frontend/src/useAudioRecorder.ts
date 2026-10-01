import { useEffect, useRef, useState } from 'react'

const MAX_BYTES = 10 * 1024 * 1024
const MIME_TYPES = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4', 'audio/webm', 'audio/ogg']
type State = 'idle' | 'requesting' | 'recording' | 'stopping' | 'ready' | 'error'

export function useAudioRecorder() {
  const [state, setState] = useState<State>('idle')
  const [blob, setBlob] = useState<Blob | null>(null)
  const [error, setError] = useState('')
  const generation = useRef(0)
  const locked = useRef(false)
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  function release() {
    if (timer.current) clearTimeout(timer.current)
    timer.current = null
    stream.current?.getTracks().forEach((track) => track.stop())
    stream.current = null
  }

  function dispose() {
    const current = recorder.current
    recorder.current = null
    if (current) {
      current.ondataavailable = null
      current.onstop = null
      current.onerror = null
      try { if (current.state !== 'inactive') current.stop() } catch { /* Still release tracks. */ }
    }
    release()
  }

  useEffect(() => () => {
    generation.current += 1
    locked.current = false
    dispose()
  }, [])

  function fail(message: string) {
    generation.current += 1
    dispose()
    locked.current = false
    setBlob(null)
    setError(message)
    setState('error')
  }

  async function start() {
    if (locked.current) return
    locked.current = true
    const attempt = ++generation.current
    setBlob(null)
    setError('')
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      fail('Microphone recording is unavailable. Use a supported browser on localhost or HTTPS, or type your answer.')
      return
    }
    setState('requesting')
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (attempt !== generation.current) {
        media.getTracks().forEach((track) => track.stop())
        return
      }
      stream.current = media
      const mimeType = MIME_TYPES.find((type) => MediaRecorder.isTypeSupported?.(type))
      const current = mimeType ? new MediaRecorder(media, { mimeType }) : new MediaRecorder(media)
      recorder.current = current
      const chunks: Blob[] = []
      let size = 0
      current.ondataavailable = (event) => {
        if (attempt !== generation.current) return
        size += event.data.size
        if (size > MAX_BYTES) {
          fail('Recording exceeds 10 MiB. Please record a shorter answer.')
          return
        }
        if (event.data.size) chunks.push(event.data)
      }
      current.onerror = () => {
        if (attempt === generation.current) fail('Recording failed. Check your microphone and try again.')
      }
      current.onstop = () => {
        if (attempt !== generation.current) return
        release()
        locked.current = false
        recorder.current = null
        const audio = new Blob(chunks, { type: current.mimeType || chunks[0]?.type || '' })
        if (!audio.size || !audio.type) {
          fail('No usable audio was recorded. Please try again or type your answer.')
          return
        }
        setBlob(audio)
        setState('ready')
      }
      current.start(1000)
      setState('recording')
      timer.current = setTimeout(stop, 5 * 60 * 1000)
    } catch (cause) {
      if (attempt !== generation.current) return
      fail(cause instanceof DOMException && cause.name === 'NotAllowedError'
        ? 'Microphone permission denied. Allow microphone access in browser settings or type your answer.'
        : 'Unable to start recording. Check that a microphone is connected and available.')
    }
  }

  function stop() {
    const current = recorder.current
    if (!current || current.state === 'inactive') return
    setState('stopping')
    try {
      current.stop()
      release()
    } catch {
      fail('Unable to finish recording. Please try again.')
    }
  }

  return { state, blob, error, start, stop }
}
