import { useState } from 'react'
import { getRun } from '../api.js'
import { fmtLatency, fmtScore, fmtTime, scoreColor, shortModel } from '../utils.js'
import CaseTable from './CaseTable.jsx'

export default function Runs({ runs, onRefresh }) {
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const open = async (runDir) => {
    if (selected === runDir) return
    setSelected(runDir)
    setDetail(null)
    setError('')
    setLoading(true)
    try {
      setDetail(await getRun(runDir))
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div>
      <div className="section-header" style={{ marginBottom: 20 }}>
        <span className="section-title">Runs</span>
        <button className="btn sm" onClick={onRefresh}>↻ Refresh</button>
      </div>

      {runs.length === 0 ? (
        <div className="card">
          <div className="empty">
            No runs yet. Go to <strong>Run</strong> to start your first benchmark.
          </div>
        </div>
      ) : (
        <div className="runs-layout">
          <div className="runs-sidebar">
            {runs.map(r => (
              <div
                key={r.run_dir}
                className={`run-item ${selected === r.run_dir ? 'active' : ''}`}
                onClick={() => open(r.run_dir)}
              >
                <div className="run-model">{shortModel(r.model) || r.run_dir}</div>
                <div className="run-meta">
                  {fmtTime(r.timestamp)}
                </div>
                <div className="run-meta" style={{ marginTop: 4 }}>
                  <span style={{ color: scoreColor(r.composite_avg), fontWeight: 600 }}>
                    {fmtScore(r.composite_avg)}
                  </span>
                  {' composite · '}
                  {r.cases_scored ?? '?'} cases
                </div>
              </div>
            ))}
          </div>

          <div>
            {!selected && (
              <div className="card">
                <div className="empty" style={{ padding: '32px 24px' }}>
                  Select a run to view results.
                </div>
              </div>
            )}
            {selected && loading && (
              <div className="card">
                <div className="empty" style={{ padding: '32px 24px' }}>Loading…</div>
              </div>
            )}
            {error && (
              <div className="card">
                <div className="error-text">{error}</div>
              </div>
            )}
            {detail && !loading && (
              <>
                <div className="card" style={{ marginBottom: 16 }}>
                  <div style={{ fontFamily: 'ui-monospace, monospace', fontSize: 13, marginBottom: 16, color: 'var(--muted)' }}>
                    {detail.run_dir}
                  </div>
                  <SummaryMetrics s={detail.summary} />
                </div>
                <div className="card">
                  <div className="card-title">Cases ({detail.cases?.length ?? 0})</div>
                  <CaseTable cases={detail.cases || []} />
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function SummaryMetrics({ s }) {
  if (!s) return null
  return (
    <div className="metrics-grid">
      {[
        { label: 'Composite', value: s.composite_avg },
        { label: 'Answer quality', value: s.answer_quality_avg },
        { label: 'Tool accuracy', value: s.tool_accuracy_avg },
        { label: 'Latency score', value: s.latency_score_avg },
        { label: 'Avg latency', value: null, text: fmtLatency(s.latency_s_avg) },
        { label: 'Cases scored', value: null, text: String(s.cases_scored ?? '—') },
      ].map(m => (
        <div key={m.label} className="metric-card">
          <div
            className="metric-value"
            style={{ color: m.value != null ? scoreColor(m.value) : 'var(--text)' }}
          >
            {m.text ?? fmtScore(m.value)}
          </div>
          <div className="metric-label">{m.label}</div>
        </div>
      ))}
    </div>
  )
}
