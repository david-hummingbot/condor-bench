import { useState, useEffect, useRef } from 'react'
import { cancelRun, streamUrl } from '../api.js'
import { scoreColor, fmtScore, fmtLatency, PASS_THRESHOLD } from '../utils.js'

export default function LiveRun({ runId, onDone, onViewRuns }) {
  const [status, setStatus] = useState('idle')
  const [total, setTotal] = useState(0)
  const [completed, setCompleted] = useState(0)
  const [currentCase, setCurrentCase] = useState(null)
  const [currentModel, setCurrentModel] = useState(null)
  const [cases, setCases] = useState([]) // live result rows
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(null)
  const esRef = useRef(null)

  useEffect(() => {
    if (!runId) return
    setStatus('connecting')
    setCases([])
    setCompleted(0)
    setError('')

    const es = new EventSource(streamUrl(runId))
    esRef.current = es

    es.onmessage = (e) => {
      let evt
      try { evt = JSON.parse(e.data) } catch { return }
      const t = evt.type

      if (t === 'run_started') {
        setStatus('running')
        setTotal(evt.total || 0)
      } else if (t === 'model_started') {
        setCurrentModel(evt.model)
      } else if (t === 'case_started') {
        setCurrentCase({ id: evt.case_id, type: evt.case_type })
        setTotal(evt.total || 0)
      } else if (t === 'case_done') {
        setCompleted(evt.completed || 0)
        setTotal(evt.total || 0)
        setCurrentCase(null)
        if (evt.scorecard) {
          setCases(prev => [
            { ...evt.scorecard, response: evt.response, model: evt.model, error: evt.error },
            ...prev,
          ])
        }
      } else if (t === 'model_done') {
        setCurrentModel(null)
      } else if (t === 'run_done') {
        setStatus(evt.status || 'completed')
        setCurrentCase(null)
        setCurrentModel(null)
        if (evt.error) setError(evt.error)
        es.close()
        if (onDone) onDone()
      }
    }

    es.onerror = () => {
      if (status !== 'completed' && status !== 'cancelled' && status !== 'failed') {
        setError('Connection lost')
        setStatus('failed')
      }
      es.close()
    }

    return () => { es.close(); esRef.current = null }
  }, [runId])

  const pct = total > 0 ? Math.round((completed / total) * 100) : 0

  const handleCancel = async () => {
    if (!runId) return
    try { await cancelRun(runId) } catch {}
  }

  if (!runId) {
    return (
      <div>
        <div className="section-header" style={{ marginBottom: 20 }}>
          <span className="section-title">Live Run</span>
        </div>
        <div className="card">
          <div className="empty">
            No active run. Go to the <strong>Run</strong> tab to start a benchmark.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div>
      <div className="section-header" style={{ marginBottom: 20 }}>
        <span className="section-title">Live Run</span>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span className={`status-badge ${status}`}>{status}</span>
          {(status === 'running' || status === 'connecting') && (
            <button className="btn sm danger" onClick={handleCancel}>Cancel</button>
          )}
          {(status === 'completed' || status === 'cancelled' || status === 'failed') && (
            <button className="btn sm" onClick={onViewRuns}>View Runs →</button>
          )}
        </div>
      </div>

      <div className="card">
        {currentModel && (
          <div style={{ marginBottom: 10, fontSize: 13, color: 'var(--muted)' }}>
            Model: <strong style={{ color: 'var(--text)' }}>{currentModel}</strong>
          </div>
        )}

        <div className="progress-bar-wrap">
          <div className="progress-bar-fill" style={{ width: pct + '%' }} />
        </div>
        <div className="progress-label">
          <span>
            {currentCase
              ? <>{currentCase.type === 'tick' ? '🔁' : '💬'} {currentCase.id}</>
              : status === 'completed' ? 'All done' : ''}
          </span>
          <span>{completed} / {total || '?'}</span>
        </div>

        {error && (
          <div className="error-text" style={{ marginTop: 8 }}>{error}</div>
        )}
      </div>

      {cases.length > 0 && (
        <div className="card" style={{ marginTop: 16 }}>
          <div className="card-title">Results ({cases.length})</div>
          <table className="case-table">
            <thead>
              <tr>
                <th>Case</th>
                <th>Model</th>
                <th style={{ textAlign: 'right' }}>Composite</th>
                <th style={{ textAlign: 'right' }}>Quality</th>
                <th style={{ textAlign: 'right' }}>Tools</th>
                <th style={{ textAlign: 'right' }}>Latency</th>
                <th style={{ textAlign: 'right' }}>Pass</th>
              </tr>
            </thead>
            <tbody>
              {cases.map((c, i) => (
                <>
                  <tr
                    key={c.case_id + i}
                    className="expand-row"
                    onClick={() => setExpanded(expanded === i ? null : i)}
                  >
                    <td>
                      <span style={{ marginRight: 6 }}>{expanded === i ? '▾' : '▸'}</span>
                      {c.case_id}
                    </td>
                    <td style={{ color: 'var(--muted)', fontSize: 12 }}>
                      {c.model ? c.model.split(':').slice(1).join(':') || c.model : '—'}
                    </td>
                    <td style={{ textAlign: 'right', color: scoreColor(c.composite), fontWeight: 600 }}>
                      {fmtScore(c.composite)}
                    </td>
                    <td style={{ textAlign: 'right', color: scoreColor(c.answer_quality) }}>
                      {fmtScore(c.answer_quality)}
                    </td>
                    <td style={{ textAlign: 'right', color: scoreColor(c.tool_accuracy) }}>
                      {fmtScore(c.tool_accuracy)}
                    </td>
                    <td style={{ textAlign: 'right', color: 'var(--muted)' }}>
                      {fmtLatency(c.latency_s)}
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      {c.error
                        ? <span style={{ color: 'var(--red)' }}>✗ err</span>
                        : (c.composite >= PASS_THRESHOLD
                          ? <span style={{ color: 'var(--green)' }}>✓</span>
                          : <span style={{ color: 'var(--red)' }}>✗</span>)}
                    </td>
                  </tr>
                  {expanded === i && (
                    <tr key={`exp-${i}`}>
                      <td colSpan={7} style={{ padding: '0 12px 12px' }}>
                        <div className="case-detail">
                          {c.error && (
                            <div className="error-text" style={{ marginBottom: 8 }}>{c.error}</div>
                          )}
                          <div className="case-detail-grid">
                            <div>
                              <div className="case-detail-label">Response</div>
                              <div className="case-detail-text">
                                {c.response || '(no response)'}
                              </div>
                            </div>
                            <div>
                              <div className="case-detail-label">Judge Reasoning</div>
                              <div className="case-detail-text">
                                {c.answer_reason || '—'}
                              </div>
                            </div>
                          </div>
                          <div className="score-chips">
                            {[
                              ['Composite', c.composite],
                              ['Quality', c.answer_quality],
                              ['Tools', c.tool_accuracy],
                              ['Latency score', c.latency_score],
                            ].map(([label, val]) => (
                              <span key={label} className="score-chip">
                                <span className="chip-label">{label}</span>
                                <span className="chip-val" style={{ color: scoreColor(val) }}>
                                  {fmtScore(val)}
                                </span>
                              </span>
                            ))}
                            <span className="score-chip">
                              <span className="chip-label">Latency</span>
                              <span className="chip-val">{fmtLatency(c.latency_s)}</span>
                            </span>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
