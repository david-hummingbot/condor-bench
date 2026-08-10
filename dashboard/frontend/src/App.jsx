import { useState, useEffect, useCallback } from 'react'
import RunConfig from './components/RunConfig.jsx'
import LiveRun from './components/LiveRun.jsx'
import Leaderboard from './components/Leaderboard.jsx'
import Matrix from './components/Matrix.jsx'
import ModelRouter from './components/ModelRouter.jsx'
import Runs from './components/Runs.jsx'
import CustomPrompt from './components/CustomPrompt.jsx'
import Suites from './components/Suites.jsx'
import Settings from './components/Settings.jsx'
import Overview from './components/Overview.jsx'
import { listRuns } from './api.js'
import './styles.css'

/**
 * Information architecture.
 *
 * The app has nine surfaces. Flat, they read as nine unrelated peers and give
 * no answer to "which of these three do I use to start a run?". Grouped, they
 * are a workflow: understand the state of things, define what to test, run it,
 * read the verdict — with configuration parked at the end.
 *
 * Live is deliberately NOT a section. A run is a state, not a place; it
 * surfaces as a strip that follows the user across every page, so the tab is
 * never sitting there empty between runs.
 */
const SECTIONS = [
  { id: 'overview', label: 'Overview' },
  { id: 'suites', label: 'Suites' },
  {
    id: 'run',
    label: 'Quick Run',
    pages: [
      { id: 'benchmark', label: 'Benchmark' },
      { id: 'prompt', label: 'Prompt' },
    ],
  },
  {
    id: 'results',
    label: 'Results',
    pages: [
      { id: 'runs', label: 'Runs' },
      { id: 'leaderboard', label: 'Leaderboard' },
      { id: 'matrix', label: 'Matrix' },
      { id: 'router', label: 'Router' },
    ],
  },
  { id: 'settings', label: 'Settings' },
]

const DEFAULT_ROUTE = { section: 'overview', page: null }
// Wide data views need more than the reading-width canvas.
const WIDE_PAGES = new Set(['results/matrix', 'results/router'])

function parseHash() {
  const raw = (window.location.hash || '').replace(/^#\/?/, '')
  const [section, page] = raw.split('/')
  if (section === 'live') return { section: 'live', page: null }
  const match = SECTIONS.find((s) => s.id === section)
  if (!match) return DEFAULT_ROUTE
  if (!match.pages) return { section: match.id, page: null }
  const pageMatch = match.pages.find((p) => p.id === page)
  return { section: match.id, page: (pageMatch || match.pages[0]).id }
}

/** Hash routing so a reload, a bookmark, or a shared link lands where it should. */
function useHashRoute() {
  const [route, setRoute] = useState(parseHash)

  useEffect(() => {
    const onChange = () => setRoute(parseHash())
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])

  const navigate = useCallback((href) => {
    const next = href.startsWith('#') ? href : `#/${href}`
    if (window.location.hash === next) setRoute(parseHash())
    else window.location.hash = next
  }, [])

  return [route, navigate]
}

export default function App() {
  const [route, navigate] = useHashRoute()
  const [runs, setRuns] = useState([])
  const [activeRunId, setActiveRunId] = useState(null)
  const [runStatus, setRunStatus] = useState(null) // 'running' | 'done'
  const [judgeOk, setJudgeOk] = useState(true)
  const [config, setConfig] = useState(null)

  const refreshRuns = useCallback(async () => {
    try {
      const data = await listRuns()
      setRuns(data.runs || [])
    } catch {}
  }, [])

  const refreshConfig = useCallback(() => {
    fetch('/api/config')
      .then((r) => r.json())
      .then((d) => {
        setJudgeOk(d.judge_key_configured)
        setConfig(d)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    refreshRuns()
    refreshConfig()
  }, [])

  const handleRunStarted = (runId) => {
    setActiveRunId(runId)
    setRunStatus('running')
    navigate('#/live')
  }

  const handleRunDone = () => {
    setRunStatus('done')
    refreshRuns()
  }

  const isRunning = runStatus === 'running'
  const section = SECTIONS.find((s) => s.id === route.section)
  const key = route.page ? `${route.section}/${route.page}` : route.section
  const onLivePage = route.section === 'live'

  return (
    <div className="app">
      <header className="topbar">
        <span className="topbar-logo">
          condor<span>bench</span>
        </span>
        <nav className="nav-primary">
          {SECTIONS.map((s) => (
            <button
              key={s.id}
              className={`nav-item ${route.section === s.id ? 'active' : ''}`}
              onClick={() => navigate(`#/${s.id}`)}
            >
              {s.label}
            </button>
          ))}
        </nav>
        <div className="topbar-right">
          {config && (
            <span className="target-badge" title={config.target_banner}>
              ● live
              {config.staging?.api_url && (
                <span className="target-badge-url">{config.staging.api_url}</span>
              )}
            </span>
          )}
          {!judgeOk && (
            <span className="warn-badge" title="Set ANTHROPIC_API_KEY in Settings">
              ⚠ No judge key
            </span>
          )}
        </div>
      </header>

      {section?.pages && (
        <nav className="subnav">
          {section.pages.map((p) => (
            <button
              key={p.id}
              className={`subnav-item ${route.page === p.id ? 'active' : ''}`}
              onClick={() => navigate(`#/${section.id}/${p.id}`)}
            >
              {p.label}
              {section.id === 'results' && p.id === 'runs' && runs.length > 0 && (
                <span className="subnav-count">{runs.length}</span>
              )}
            </button>
          ))}
        </nav>
      )}

      {/* A run in flight follows the user everywhere rather than hiding in a tab. */}
      {activeRunId && !onLivePage && (
        <div className={`live-strip ${isRunning ? '' : 'done'}`}>
          <span className="live-strip-dot" />
          <span className="live-strip-label">
            {isRunning ? 'Benchmark running' : 'Benchmark finished'}
          </span>
          <span className="live-strip-meta">{activeRunId}</span>
          <div className="live-strip-actions">
            <button className="btn sm" onClick={() => navigate('#/live')}>
              {isRunning ? 'Watch progress →' : 'View output →'}
            </button>
            {!isRunning && (
              <button className="btn sm ghost" onClick={() => setActiveRunId(null)}>
                Dismiss
              </button>
            )}
          </div>
        </div>
      )}

      <main className={`page ${WIDE_PAGES.has(key) ? 'wide' : ''}`}>
        {key === 'overview' && (
          <Overview runs={runs} config={config} onNavigate={navigate} />
        )}
        {key === 'suites' && <Suites onRunStarted={handleRunStarted} />}
        {key === 'run/benchmark' && (
          <RunConfig onRunStarted={handleRunStarted} isRunning={isRunning} config={config} />
        )}
        {key === 'run/prompt' && <CustomPrompt />}
        {key === 'live' && (
          <LiveRun
            runId={activeRunId}
            onDone={handleRunDone}
            onViewRuns={() => navigate('#/results/runs')}
            onNavigate={navigate}
          />
        )}
        {key === 'results/runs' && (
          <Runs runs={runs} onRefresh={refreshRuns} onNavigate={navigate} />
        )}
        {key === 'results/leaderboard' && <Leaderboard runs={runs} onNavigate={navigate} />}
        {key === 'results/matrix' && <Matrix />}
        {key === 'results/router' && <ModelRouter />}
        {key === 'settings' && <Settings onSaved={refreshConfig} />}
      </main>
    </div>
  )
}
