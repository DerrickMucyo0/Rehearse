import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import History from './History'
import Interview from './Interview'
import ProgressDashboard from './ProgressDashboard'
import { useHistoryHydration } from './useHistoryHydration'
import { bootstrapAuth, getAuthState, logoutAuth, reloadSignIn, signIn, subscribeAuth } from './auth'
import type { AuthState } from './auth'

type BackendStatus = 'Checking backend…' | 'Backend connected' | 'Backend unavailable'
type Section = 'practice' | 'history' | 'progress'
type AuthenticatedState = Extract<AuthState, { status: 'authenticated' }>

function Workspace({ auth }: { auth: AuthenticatedState }) {
  const [section, setSection] = useState<Section>('practice')
  const [practiceBusy, setPracticeBusy] = useState(false)
  const navigationLocked = useRef(false)
  const [factsGeneration, setFactsGeneration] = useState(0)
  const invalidateHistory = useCallback(() => { setFactsGeneration((value) => value + 1) }, [])
  const hydration = useHistoryHydration(factsGeneration, section !== 'practice' && !practiceBusy)
  const [logoutBusy, setLogoutBusy] = useState(false)
  const logoutLocked = useRef(false)
  const [logoutError, setLogoutError] = useState('')
  const updateNavigationLock = useCallback((busy: boolean) => {
    navigationLocked.current = busy
    setPracticeBusy(busy)
  }, [])

  function navigate(next: Section) {
    if (navigationLocked.current && next !== 'practice') return
    setSection(next)
  }

  async function logout() {
    if (logoutLocked.current) return
    logoutLocked.current = true
    setLogoutBusy(true)
    setLogoutError('')
    const ownsWorkspace = () => {
      const current = getAuthState()
      return current.status === 'authenticated' && current.generation === auth.generation
    }
    try { await logoutAuth() } catch {
      if (ownsWorkspace()) setLogoutError('Unable to sign out right now. Please try again.')
    } finally {
      if (ownsWorkspace()) { logoutLocked.current = false; setLogoutBusy(false) }
    }
  }

  return <section className="workspace" aria-label="Rehearse workspace">
    <div className="workspace-toolbar">
      <p className="workspace-label"><span className="status-dot" /> Your practice space</p>
      <button className="logout-button" type="button" onClick={() => void logout()} disabled={logoutBusy}>
        {logoutBusy ? 'Signing out…' : 'Log out'}
      </button>
    </div>
    {auth.notice && <p role="alert">{auth.notice}</p>}
    {logoutError && <p role="alert">{logoutError}</p>}
    <nav className="app-nav" aria-label="Main navigation">
      {(['practice', 'history', 'progress'] as const).map((target) => (
        <button key={target} type="button" aria-current={section === target ? 'page' : undefined}
          aria-controls={`${target}-panel`} disabled={practiceBusy && target !== 'practice'}
          onClick={() => navigate(target)}>
          {target[0].toUpperCase() + target.slice(1)}
        </button>
      ))}
    </nav>
    {practiceBusy && <p className="navigation-notice" role="status">Finish the current Practice operation before navigating.</p>}
    {/* Idle drafts remain mounted only within this authenticated workspace. */}
    <div id="practice-panel" hidden={section !== 'practice'}>
      <Interview active={section === 'practice'} onNavigationBusyChange={updateNavigationLock} onHistoryFactsChange={invalidateHistory} />
    </div>
    {section === 'history' && <div id="history-panel">
      <History hydration={hydration} onPractice={() => navigate('practice')} />
    </div>}
    {section === 'progress' && <div id="progress-panel">
      <ProgressDashboard history={hydration.history} onRetry={hydration.retry} onReload={hydration.reload}
        onLoadMore={hydration.loadMore} onPractice={() => navigate('practice')} />
    </div>}
  </section>
}

export default function App() {
  const auth = useSyncExternalStore(subscribeAuth, getAuthState)
  const [bootstrapped, setBootstrapped] = useState(false)
  const [status, setStatus] = useState<BackendStatus>('Checking backend…')
  useEffect(() => {
    let active = true
    void bootstrapAuth().finally(() => { if (active) setBootstrapped(true) })
    return () => { active = false }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(), 5000)
    let active = true
    void (async () => {
      try {
        const response = await fetch('/api/health', { signal: controller.signal })
        if (!response.ok) throw new Error('Health request failed')
        const health: unknown = await response.json()
        if (typeof health !== 'object' || health === null ||
            !('status' in health) || health.status !== 'ok' ||
            !('service' in health) || health.service !== 'rehearse-api') throw new Error('Unexpected health response')
        if (active) setStatus('Backend connected')
      } catch {
        if (active) setStatus('Backend unavailable')
      } finally { window.clearTimeout(timeout) }
    })()
    return () => { active = false; window.clearTimeout(timeout); controller.abort() }
  }, [])

  return <div className="app-page" id="top">
    <header className="site-header">
      <a className="brand" href="#top" aria-label="Rehearse home">
        <span className="brand-mark" aria-hidden="true">R</span>
        <span>Rehearse</span>
      </a>
      <span className="header-note">A calmer way to practice the hard moments</span>
    </header>
    <main className="app-main">
      <section className="app-intro" aria-labelledby="app-title">
        <p className="eyebrow">COMMUNICATION PRACTICE</p>
        <h1 id="app-title">Practice. Diagnose. Drill. Improve.</h1>
        <p className="intro-copy">Build confidence for interviews, presentations, thesis defenses, and important conversations.</p>
      </section>
    {(!bootstrapped || auth.status === 'loading') && <p role="status">Checking sign-in…</p>}
    {bootstrapped && auth.status === 'signed_out' && <section aria-label="Sign in">
      <p>Sign in to practice and view your history.</p>
      <button type="button" onClick={signIn}>Sign in</button>
    </section>}
    {bootstrapped && auth.status === 'stale' && <section aria-label="Sign-in changed">
      <p role="alert">Your sign-in changed. Reload to enter the current account.</p>
      <button type="button" onClick={reloadSignIn}>Reload current sign-in</button>
    </section>}
    {bootstrapped && auth.status === 'unavailable' && <section aria-label="Authentication unavailable">
      <p role="alert">Authentication is temporarily unavailable. Please try again.</p>
      <button type="button" onClick={() => void bootstrapAuth()}>Try again</button>
    </section>}
    {bootstrapped && auth.status === 'authenticated' && <Workspace key={auth.generation} auth={auth} />}
    </main>
    <footer className="site-footer">
      <span>Private practice, at your pace</span>
      <p className="backend-status" role="status">{status}</p>
    </footer>
  </div>
}
