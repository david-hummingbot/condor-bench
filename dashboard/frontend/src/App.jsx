import { useState, useEffect, useCallback } from 'react'
import RunConfig from './components/RunConfig.jsx'
import LiveRun from './components/LiveRun.jsx'
import Leaderboard from './components/Leaderboard.jsx'
import Matrix from './components/Matrix.jsx'
import ModelRouter from './components/ModelRouter.jsx'
import Runs from './components/Runs.jsx'
import CustomPrompt from './components/CustomPrompt.jsx'
import { listRuns } from './api.js'
import './styles.css'

export default function App() {
  const [tab, setTab] = useState('run')
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

  useEffect(() => {
    refreshRuns()
    fetch('/api/config')
      .then(r => r.json())
      .then(d => {
        setJudgeOk(d.judge_key_configured)
        setConfig(d)
      })
      .catch(() => {})
  }, [])

  const handleRunStarted = (runId) => {
    setActiveRunId(runId)
    setRunStatus('running')
    setTab('live')
  }

  const handleRunDone = () => {
    setRunStatus('done')
    refreshRuns()
  }

  const isRunning = runStatus === 'running'
  const isLive = config?.mode === 'live'

  return (
    <div className="app">
      <div className="topbar">
        <span className="topbar-logo">condor<span>bench</span></span>
        <nav className="tabs">
          {[
            { id: 'run', label: 'Run' },
            { id: 'live', label: 'Live', dot: isRunning },
            { id: 'prompt', label: 'Prompt' },
            { id: 'leaderboard', label: 'Leaderboard' },
            { id: 'matrix', label: 'Matrix' },
            { id: 'router', label: 'Router' },
            { id: 'runs', label: 'Results' },
          ].map(t => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? 'active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              {t.dot && <span className="tab-dot" />}
            </button>
          ))}
        </nav>
        <div className="topbar-right">
          {config && (
            /* Which backend a run is about to hit is the single most consequential
               thing on this page, so it is always visible — not one click away. */
            <span className={`mode-badge ${isLive ? 'live' : 'mock'}`} title={config.mode_banner}>
              {isLive ? '● live' : '○ mock'}
              {isLive && config.staging?.api_url && (
                <span className="mode-badge-url">{config.staging.api_url}</span>
              )}
            </span>
          )}
          {!judgeOk && (
            <span className="warn-badge">
              ⚠ ANTHROPIC_API_KEY not set — quality scoring disabled
            </span>
          )}
        </div>
      </div>

      <div className="page">
        {tab === 'run' && (
          <RunConfig
            onRunStarted={handleRunStarted}
            isRunning={isRunning}
            config={config}
          />
        )}
        {tab === 'live' && (
          <LiveRun
            runId={activeRunId}
            onDone={handleRunDone}
            onViewRuns={() => setTab('runs')}
          />
        )}
        {tab === 'prompt' && <CustomPrompt />}
        {tab === 'leaderboard' && <Leaderboard runs={runs} />}
        {tab === 'matrix' && <Matrix />}
        {tab === 'router' && <ModelRouter />}
        {tab === 'runs' && (
          <Runs runs={runs} onRefresh={refreshRuns} />
        )}
      </div>
    </div>
  )
}
