import { useCallback, useEffect, useRef, useState } from 'react'
import History from './History'
import Interview from './Interview'
import ProgressDashboard from './ProgressDashboard'
import { useHistoryHydration } from './useHistoryHydration'
import { useHistoryRegistry } from './useHistoryRegistry'

type BackendStatus = 'Checking backend…' | 'Backend connected' | 'Backend unavailable'
type Section = 'practice' | 'history' | 'progress'

export default function App() {
  const [status, setStatus] = useState<BackendStatus>('Checking backend…')
  const [section, setSection] = useState<Section>('practice')
  const [practiceBusy, setPracticeBusy] = useState(false)
  const navigationLocked = useRef(false)
  const registry = useHistoryRegistry()
  const [factsGeneration, setFactsGeneration] = useState(0)
  const invalidateHistory = useCallback(() => { setFactsGeneration((value) => value + 1) }, [])
  const hydration = useHistoryHydration(registry.sessionIds, registry.cacheGeneration + factsGeneration,
    section !== 'practice' && !practiceBusy)
  const updateNavigationLock = useCallback((busy: boolean) => {
    navigationLocked.current = busy
    setPracticeBusy(busy)
  }, [])

  function navigate(next: Section) {
    if (navigationLocked.current && next !== 'practice') return
    setSection(next)
  }

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
      <nav className="app-nav" aria-label="Main navigation">
        {(['practice', 'history', 'progress'] as const).map((target) => (
          <button key={target} type="button" aria-current={section === target ? 'page' : undefined}
            aria-controls={`${target}-panel`} disabled={practiceBusy && target !== 'practice'}
            onClick={() => navigate(target)}>
            {target[0].toUpperCase() + target.slice(1)}
          </button>
        ))}
      </nav>
      {practiceBusy && <p role="status">Finish the current Practice operation before navigating.</p>}
      {/* Preserve safe idle drafts, measured draft identity, and review/retry state. */}
      <div id="practice-panel" hidden={section !== 'practice'}>
        {registry.storageError && <p role="status">{registry.storageError}</p>}
        <Interview onSessionAccess={registry.remember} onNavigationBusyChange={updateNavigationLock}
          onHistoryFactsChange={invalidateHistory} />
      </div>
      {section === 'history' && <div id="history-panel">
        <History key={registry.cacheGeneration + factsGeneration} sessionIds={registry.sessionIds} storageError={registry.storageError}
          hydration={hydration}
          onRemove={registry.remove} onClear={registry.clear} onPractice={() => navigate('practice')} />
      </div>}
      {section === 'progress' && <div id="progress-panel">
        <ProgressDashboard history={hydration.history} storageError={registry.storageError}
          onRetry={hydration.retry} onReload={hydration.reload} onPractice={() => navigate('practice')} />
      </div>}
      <p className="backend-status" role="status">{status}</p>
    </main>
  )
}
