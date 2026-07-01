import { useState } from 'react'
import { fmtLatency, fmtScore, PASS_THRESHOLD, scoreColor } from '../utils.js'

export default function CaseTable({ cases }) {
  const [expanded, setExpanded] = useState(null)

  if (!cases || cases.length === 0) {
    return <div className="empty" style={{ padding: '24px' }}>No cases in this run.</div>
  }

  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="case-table">
        <thead>
          <tr>
            <th>Case</th>
            <th>Type</th>
            <th>Category</th>
            <th>Composite</th>
            <th>Quality</th>
            <th>Tools</th>
            <th>Latency score</th>
            <th>Latency</th>
            <th>Pass</th>
          </tr>
        </thead>
        <tbody>
          {cases.map(c => {
            const isOpen = expanded === c.case_id
            const passed = !c.error && (c.composite ?? 0) >= PASS_THRESHOLD
            const ctype = c.case_id?.startsWith('t') ? 'tick' : 'consult'
            return (
              <>
                <tr
                  key={c.case_id}
                  className="expand-row"
                  onClick={() => setExpanded(isOpen ? null : c.case_id)}
                >
                  <td>
                    <span style={{ color: 'var(--muted)', marginRight: 6 }}>{isOpen ? '▾' : '▸'}</span>
                    <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>{c.case_id}</span>
                  </td>
                  <td><span className={`type-tag ${ctype}`}>{ctype}</span></td>
                  <td style={{ color: 'var(--muted)', fontSize: 12 }}>{c.category || '—'}</td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.composite), fontWeight: 600 }}>
                    {c.error ? <span style={{ color: 'var(--red)', fontSize: 11 }}>ERR</span> : fmtScore(c.composite)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.answer_quality) }}>
                    {c.error ? '—' : fmtScore(c.answer_quality)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.tool_accuracy) }}>
                    {c.error ? '—' : fmtScore(c.tool_accuracy)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.latency_score) }}>
                    {c.error ? '—' : fmtScore(c.latency_score)}
                  </td>
                  <td style={{ textAlign: 'right', color: 'var(--muted)' }}>
                    {fmtLatency(c.latency_s)}
                  </td>
                  <td style={{ textAlign: 'right' }}>
                    {c.error
                      ? <span style={{ color: 'var(--orange)' }}>⚠</span>
                      : passed
                        ? <span style={{ color: 'var(--green)' }}>✓</span>
                        : <span style={{ color: 'var(--red)' }}>✗</span>}
                  </td>
                </tr>
                {isOpen && (
                  <tr key={`${c.case_id}-det`}>
                    <td colSpan={9} style={{ padding: '0 12px 12px' }}>
                      <div className="case-detail">
                        {c.error && (
                          <div className="error-text" style={{ marginBottom: 10 }}>{c.error}</div>
                        )}
                        <div className="case-detail-grid">
                          <div>
                            <div className="case-detail-label">Response</div>
                            <div className="case-detail-text">{c.response || '(no response)'}</div>
                          </div>
                          <div>
                            <div className="case-detail-label">Judge Reasoning</div>
                            <div className="case-detail-text">{c.answer_reason || '—'}</div>
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
                          <span className="score-chip">
                            <span className="chip-label">Baseline</span>
                            <span className="chip-val">{fmtLatency(c.baseline_latency_s)}</span>
                          </span>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
              </>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
