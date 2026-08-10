/**
 * Overview — the landing page.
 *
 * It exists because the app previously opened on an empty Suites sidebar,
 * which is a cold start with no explanation of what the tool is for or which
 * of the three "start a run" surfaces to use. This page answers both, then
 * gets out of the way: the flow cards are the primary navigation for a new
 * user, and the two lists are shortcuts back into work already done.
 */
import { useEffect, useState } from 'react'
import { getDatasets } from '../api.js'
import { buildLeaderboard, fmtScore, fmtTime, scoreColor, shortModel } from '../utils.js'
import StagingStatus from './StagingStatus.jsx'

const FLOW = [
  {
    n: 1,
    title: 'Define what to test',
    desc: 'Build a suite of cases and point it at one or more Condor checkouts, or skip straight to an ad-hoc run.',
    route: '#/suites',
  },
  {
    n: 2,
    title: 'Run it',
    desc: 'Execute against the staging hummingbot-api through Condor\'s MCP servers. Progress streams case by case.',
    route: '#/run/benchmark',
  },
  {
    n: 3,
    title: 'Read the verdict',
    desc: 'Rank models, read the model × domain matrix, and export the routing config Condor should ship with.',
    route: '#/results/leaderboard',
  },
]

export default function Overview({ runs = [], config, onNavigate }) {
  const [datasets, setDatasets] = useState(null)

  useEffect(() => {
    getDatasets().then(setDatasets).catch(() => {})
  }, [])

  const board = buildLeaderboard(runs)
  const recent = runs.slice(0, 5)

  const stats = [
    { label: 'Runs recorded', value: runs.length },
    { label: 'Models compared', value: board.length },
    { label: 'Cases in library', value: datasets?.total ?? '—' },
    { label: 'Routing domains', value: datasets?.routing_domains?.length ?? '—' },
  ]

  return (
    <div>
      <div className="hero">
        <div className="hero-text">
          <div className="hero-title">condor·bench</div>
          <p className="hero-sub">
            Benchmark models against Condor's MCP tooling, then decide which model
            should serve which routing domain. Runs execute against{' '}
            <strong style={{ color: 'var(--green)' }}>
              {config?.staging?.api_url || 'the configured hummingbot-api'}
            </strong>.
          </p>
        </div>
        <div className="page-header-actions">
          <button className="btn primary" onClick={() => onNavigate('#/run/benchmark')}>
            ▶ New benchmark
          </button>
        </div>
      </div>

      <div className="flow">
        {FLOW.map((s, i) => (
          <button key={s.n} className="flow-step" onClick={() => onNavigate(s.route)}>
            <span className="flow-step-num">{s.n}</span>
            <span className="flow-step-title">{s.title}</span>
            <span className="flow-step-desc">{s.desc}</span>
            {i < FLOW.length - 1 && <span className="flow-arrow">→</span>}
          </button>
        ))}
      </div>

      <div className="stat-row" style={{ marginBottom: 20 }}>
        {stats.map((s) => (
          <div key={s.label} className="stat">
            <div className="stat-value">{s.value}</div>
            <div className="stat-label">{s.label}</div>
          </div>
        ))}
      </div>

      <div style={{ marginBottom: 20 }}>
        <StagingStatus compact />
      </div>

      <div className="card-grid cols-2">
        <div className="card">
          <div className="card-head">
            <div className="card-title">Recent runs</div>
            <button className="btn sm ghost" onClick={() => onNavigate('#/results/runs')}>
              View all →
            </button>
          </div>
          {recent.length === 0 ? (
            <p className="muted">No runs yet — start one from step 2 above.</p>
          ) : (
            <div className="mini-list">
              {recent.map((r) => (
                <button
                  key={r.run_dir}
                  className="mini-row"
                  onClick={() => onNavigate('#/results/runs')}
                >
                  <span className="mini-name">{shortModel(r.model) || r.run_dir}</span>
                  <span className="mini-meta">{fmtTime(r.timestamp)}</span>
                  <span className="mini-score" style={{ color: scoreColor(r.composite_avg) }}>
                    {fmtScore(r.composite_avg)}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="card-head">
            <div className="card-title">Top models</div>
            <button className="btn sm ghost" onClick={() => onNavigate('#/results/leaderboard')}>
              Leaderboard →
            </button>
          </div>
          {board.length === 0 ? (
            <p className="muted">Ranking appears once at least one run has been scored.</p>
          ) : (
            <div className="mini-list">
              {board.slice(0, 5).map((r, i) => (
                <button
                  key={r.model}
                  className="mini-row"
                  onClick={() => onNavigate('#/results/leaderboard')}
                >
                  <span className="mini-rank">{i + 1}</span>
                  <span className="mini-name">{shortModel(r.model)}</span>
                  <span className="mini-meta">{r.cases_scored ?? '—'} cases</span>
                  <span className="mini-score" style={{ color: scoreColor(r.composite_avg) }}>
                    {fmtScore(r.composite_avg)}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
