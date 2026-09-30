import { useEffect, useState } from 'react'
import Interview from './Interview'

type BackendStatus = 'Checking backend…' | 'Backend connected' | 'Backend unavailable'

export default function App() {
  const [status, setStatus] = useState<BackendStatus>('Checking backend…')

  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    let active = true

    async function checkBackend() {
      try {
        const response = await fetch('/api/health', { signal: controller.signal })
        if (!response.ok) throw new Error('Health request failed')
        const health: unknown = await response.json()
        if (
          typeof health !== 'object' || health === null ||
          !('status' in health) || health.status !== 'ok' ||
          !('service' in health) || health.service !== 'rehearse-api'
        ) throw new Error('Unexpected health response')
        if (active) setStatus('Backend connected')
      } catch {
        if (active) setStatus('Backend unavailable')
      } finally {
        window.clearTimeout(timeout)
      }
    }

    void checkBackend()
    return () => {
      active = false
      window.clearTimeout(timeout)
      controller.abort()
    }
  }, [])

  return (
    <main>
      <h1>Rehearse</h1>
      <p>Practice. Diagnose. Drill. Improve.</p>
      <Interview />
      <p className="backend-status" role="status">{status}</p>
    </main>
  )
}
