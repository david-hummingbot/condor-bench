import { useState, useEffect, useRef } from 'react'
import { cancelRun, streamUrl } from '../api.js'
import { scoreColor, fmtScore, fmtLatency, PASS_THRESHOLD } from '../utils.js'
import casePrompts from '../casePrompts.json'
import PageHeader from './PageHeader.jsx'
import EmptyState from './EmptyState.jsx'

function caseQuestion(c) {
  return c.question || casePrompts[c.case_id] || ''
}

export default function LiveRun({ runId, onDone, onViewRuns, onNavigate }) {
  const [status, setStatus] = useState('idle')
  const [total, setTotal] = useState(0)
  const [completed, setCompleted] = useState(0)
  const [currentCase, setCurrentCase] = useState(null)
  const [currentModel, setCurrentModel] = useState(null)
  const [cases, setCases] = useState([]) // live result rows
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(null)
  const esRef = useRef(null)

  const [memberInfo, setMemberInfo] = useState(null)

  useEffect(() => {
    if (!runId) return
    setStatus('connecting')
    setCases([])
    setCompleted(0)
    setError('')
    setMemberInfo(null)

    const es = new EventSource(streamUrl(runId))
    esRef.current = es

    es.onmessage = (e) => {
      let evt
      try { evt = JSON.parse(e.data) } catch { return }
      const t = evt.type

      if (t === 'run_started') {
        setStatus('running')
        // Suite Run-all reports total_members; ad-hoc reports total cases.
        setTotal(evt.total_members || evt.total || 0)
        if (evt.total_members) {
          setMemberInfo({ index: 0, total: evt.total_members, label: 'suite' })
        }
      } else if (t === 'member_started') {
        setCurrentModel(evt.model)
        setMemberInfo({
          index: evt.member_index,
          total: evt.total_members,
          environment_id: evt.environment_id,
          mode: evt.mode,
        })
        setCompleted(Math.max(0, (evt.member_index || 1) - 1))
        setTotal(evt.total_members || 0)
      } else if (t === 'member_done') {
        setCompleted(evt.member_index || completed + 1)
        setCases(prev => [
          {
            case_id: `member:${evt.environment_id}`,
            model: evt.model,
            domain: evt.environment_id,
            composite: null,
            response: `run_dir=${evt.run_dir} cases=${evt.cases}`,
            question: `Environment ${evt.environment_id}`,
          },
          ...prev,
        ])
      } else if (t === 'member_failed') {
        setCases(prev => [
          {
            case_id: `member:${evt.environment_id}`,
            model: evt.model,
            error: evt.error || 'failed',
            question: `Environment ${evt.environment_id}`,
          },
          ...prev,
        ])
        setCompleted((c) => c + 1)
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
            {
              ...evt.scorecard,
              response: evt.response,
              question: evt.question || evt.scorecard.question,
              model: evt.model,
              error: evt.error,
            },
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
        <PageHeader
          title="Live run"
          description="Case-by-case progress for a benchmark in flight. Nothing is running right now."
        />
        <div className="card">
          <EmptyState
            title="No active run"
            description="Start a run and this page streams each case as it is executed and scored."
            actions={[
              { label: '▶ New benchmark', primary: true, onClick: () => onNavigate?.('#/run/benchmark') },
              { label: 'Run a suite', onClick: () => onNavigate?.('#/suites') },
              { label: 'Past runs', onClick: () => onNavigate?.('#/results/runs') },
            ]}
          />
        </div>
      </div>
    )
  }

  return (
    <div>
      <PageHeader
        title="Live run"
        description="Case-by-case progress, streamed as the run executes. Results are saved even if you navigate away."
      >
        <span className={`status-badge ${status}`}>{status}</span>
        {(status === 'running' || status === 'connecting') && (
          <button className="btn sm danger" onClick={handleCancel}>Cancel</button>
        )}
        {(status === 'completed' || status === 'cancelled' || status === 'failed') && (
          <button className="btn sm primary" onClick={onViewRuns}>View in Results →</button>
        )}
      </PageHeader>

      <div className="card">
        {memberInfo && (
          <div className="muted" style={{ marginBottom: 8 }}>
            Suite member {memberInfo.index}/{memberInfo.total}
            {memberInfo.environment_id ? ` · ${memberInfo.environment_id}` : ''}
            {memberInfo.mode ? ` · ${memberInfo.mode}` : ''}
          </div>
        )}
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
                                {caseQuestion(c) && (
                                  <div className="case-question">
                                    <span className="case-question-label">Question</span>
                                    {caseQuestion(c)}
                                  </div>
                                )}
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
