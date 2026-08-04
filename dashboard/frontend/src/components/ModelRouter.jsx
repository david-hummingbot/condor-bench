import { useState, useEffect, useCallback } from 'react'
import { getRouting } from '../api.js'
import { fmtCost, fmtPct, fmtSize, fmtTokens } from '../utils.js'

const MODE_OPTS = [
  { id: '', label: 'Any mode' },
  { id: 'live', label: 'Live' },
  { id: 'mock', label: 'Mock' },
]

export default function ModelRouter() {
  const [routing, setRouting] = useState(null)
  const [mode, setMode] = useState('')
  const [minPassRate, setMinPassRate] = useState(0.8)
  const [minCases, setMinCases] = useState(3)
  const [preferLowerTokens, setPreferLowerTokens] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setRouting(await getRouting({
        mode: mode || undefined, minPassRate, minCases, preferLowerTokens,
      }))
    } catch (e) {
      setError(e.message)
      setRouting(null)
    } finally {
      setLoading(false)
    }
  }, [mode, minPassRate, minCases, preferLowerTokens])

  useEffect(() => { load() }, [load])

  const recs = routing?.recommendations || {}
  const unmet = routing?.unmet_domains || {}
  const snippet = routing?.condor_config_snippet || {}
  const snippetText = Object.entries(snippet).map(([k, v]) => `${k} = ${v}`).join('\n')

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(routing, null, 2))
      setCopied(true)
      setTimeout(() => setCopied(false), 1800)
    } catch { /* clipboard unavailable — the JSON is on disk anyway */ }
  }

  return (
    <div>
      <div className="section-header" style={{ marginBottom: 20 }}>
        <span className="section-title">Model Router</span>
        <span style={{ color: 'var(--muted)', fontSize: 13 }}>
          {Object.keys(recs).length} domain{Object.keys(recs).length !== 1 ? 's' : ''} decided
        </span>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div className="matrix-controls">
          <div className="field">
            <label>Mode</label>
            <div className="radio-group">
              {MODE_OPTS.map(o => (
                <button key={o.id} className={`radio-btn ${mode === o.id ? 'active' : ''}`}
                  onClick={() => setMode(o.id)}>{o.label}</button>
              ))}
            </div>
          </div>
          <div className="field" style={{ maxWidth: 150 }}>
            <label>Min pass rate</label>
            <input type="number" className="input" min="0" max="1" step="0.05"
              value={minPassRate} onChange={e => setMinPassRate(Number(e.target.value))} />
          </div>
          <div className="field" style={{ maxWidth: 130 }}>
            <label>Min cases</label>
            <input type="number" className="input" min="1" step="1"
              value={minCases} onChange={e => setMinCases(Number(e.target.value))} />
          </div>
          <div className="field" style={{ justifyContent: 'flex-end' }}>
            <label className="toggle">
              <input type="checkbox" checked={preferLowerTokens}
                onChange={e => setPreferLowerTokens(e.target.checked)} />
              <span className="toggle-track" />
              <span className="toggle-label">Prefer lower tokens</span>
            </label>
          </div>
          <button className="btn sm" onClick={load} disabled={loading} style={{ marginLeft: 'auto' }}>
            {loading ? '…' : '↻ Recompute'}
          </button>
        </div>
        <div className="matrix-note">
          Each domain gets the <strong>smallest model that passes it</strong> — not the
          best-scoring one. Cloud models sort last, so a local model that passes always
          wins. Token cost never rejects a passing model; “prefer lower tokens” only
          reorders models of the same size.
        </div>
      </div>

      {error && <div className="card"><div className="empty">{error}</div></div>}

      {routing && (
        <>
          <div className="card">
            <div className="card-title">Recommendations</div>
            {Object.keys(recs).length === 0 ? (
              <div className="empty">No domain met the criteria yet.</div>
            ) : (
              <table className="lb-table">
                <thead>
                  <tr>
                    <th>Domain</th>
                    <th>Model</th>
                    <th>Size</th>
                    <th>Pass</th>
                    <th>Cases</th>
                    <th>Avg tokens</th>
                    <th>Avg cost</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(recs).map(([domain, rec]) => (
                    <tr key={domain}>
                      <td className="model-cell">{domain}</td>
                      <td style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                        {rec.model}
                      </td>
                      <td>{fmtSize(rec.params_b)}</td>
                      <td style={{ color: 'var(--green)' }}>{fmtPct(rec.pass_rate)}</td>
                      <td>{rec.scored}{rec.excluded ? ` (+${rec.excluded} excl)` : ''}</td>
                      <td>{fmtTokens(rec.avg_total_tokens)}</td>
                      <td>{fmtCost(rec.avg_cost_usd)}</td>
                      <td style={{ textAlign: 'left', fontSize: 12, color: 'var(--muted)' }}>
                        {rec.rationale}
                        {rec.no_local_passed && (
                          <span className="router-flag" title="no local model passed this domain">
                            cloud only
                          </span>
                        )}
                        {rec.tie_breaker && <div className="run-meta">{rec.tie_breaker}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          {Object.keys(unmet).length > 0 && (
            <div className="card">
              <div className="card-title">Unmet domains</div>
              <div className="matrix-note" style={{ marginTop: -8, marginBottom: 14 }}>
                A gap that was measured, not a missing run. “Insufficient evidence” means the
                dataset is too thin here to decide — a different finding from “the models
                aren't good enough”.
              </div>
              {Object.entries(unmet).map(([domain, gap]) => (
                <div key={domain} className="unmet-row">
                  <div className="unmet-head">
                    <span className="model-cell">{domain}</span>
                    <span className={`unmet-tag ${gap.insufficient_evidence ? 'thin' : 'fail'}`}>
                      {gap.insufficient_evidence ? 'insufficient evidence' : 'no model passed'}
                    </span>
                  </div>
                  <div className="run-meta">{gap.reason}</div>
                  {gap.best_attempt?.model && (
                    <div className="run-meta">
                      best attempt: {gap.best_attempt.model} at {fmtPct(gap.best_attempt.pass_rate)}
                      {' '}({gap.best_attempt.scored} case{gap.best_attempt.scored !== 1 ? 's' : ''})
                    </div>
                  )}
                  {gap.blockers && !gap.insufficient_evidence && (
                    <div style={{ marginTop: 6 }}>
                      {Object.entries(gap.blockers).map(([m, reasons]) => (
                        <div key={m} className="run-meta">
                          <span style={{ fontFamily: 'ui-monospace, monospace' }}>{m}</span>: {reasons.join('; ')}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {Object.keys(routing.config_conflicts || {}).length > 0 && (
            <div className="card">
              <div className="card-title">Shared config keys</div>
              <div className="matrix-note" style={{ marginTop: -8, marginBottom: 14 }}>
                More than one domain writes these keys and they disagree. The larger model
                wins, because a shared key has to satisfy every domain using it.
              </div>
              {Object.entries(routing.config_conflicts).map(([key, c]) => (
                <div key={key} className="unmet-row">
                  <div className="unmet-head">
                    <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>{key}</span>
                    <span className="unmet-tag thin">used {c.chosen}</span>
                  </div>
                  {Object.entries(c.per_domain).map(([d, m]) => (
                    <div key={d} className="run-meta">{d} → {m}</div>
                  ))}
                </div>
              ))}
            </div>
          )}

          <ToolGaps gaps={routing.tool_gaps} />

          {routing.unranked_models?.length > 0 && (
            <div className="card">
              <div className="card-title">Benchmarked but unrankable</div>
              <div className="matrix-note" style={{ marginTop: -8 }}>{routing.unranked_note}</div>
              <div style={{ marginTop: 10, fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>
                {routing.unranked_models.join(', ')}
              </div>
            </div>
          )}

          <div className="card">
            <div className="card-title">Condor config</div>
            {snippetText ? (
              <pre className="config-snippet">{snippetText}</pre>
            ) : (
              <div className="empty">No domain produced a config line yet.</div>
            )}
            <div className="inline-row" style={{ marginTop: 12 }}>
              <button className="btn sm" onClick={copy}>
                {copied ? '✓ Copied' : 'Copy routing JSON'}
              </button>
              <span className="run-meta">
                Also written to results/routing_recommendations.json
              </span>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Criteria used</div>
            <div className="score-chips">
              <Chip label="Min pass rate" value={fmtPct(routing.criteria?.min_pass_rate)} />
              <Chip label="Min cases" value={String(routing.criteria?.min_cases)} />
              <Chip label="Case pass bar" value={String(routing.criteria?.pass_threshold)} />
              <Chip label="Destructive floor" value={String(routing.criteria?.destructive_floor)} />
              <Chip label="Prefer lower tokens" value={routing.routing_options?.prefer_lower_tokens ? 'on' : 'off'} />
              <Chip label="Mode" value={routing.mode || 'any'} />
            </div>
          </div>
        </>
      )}
    </div>
  )
}

function ToolGaps({ gaps }) {
  if (!gaps) return null
  const smallest = gaps.smallest_passing || {}
  const unhandled = gaps.unhandled || []
  if (!Object.keys(smallest).length && !unhandled.length) return null
  return (
    <div className="card">
      <div className="card-title">Per-tool minimum</div>
      {unhandled.length > 0 && (
        <div className="error-text" style={{ marginBottom: 12 }}>
          No model passes: {unhandled.join(', ')} — keep these on a cloud model, or fix the
          case if it is the case that's wrong.
        </div>
      )}
      <div className="tool-min-grid">
        {Object.entries(smallest).map(([tool, v]) => (
          <div key={tool} className="tool-min-cell">
            <div className="tool-min-name">{tool}</div>
            <div className="tool-min-model">{v.model}</div>
            <div className="run-meta">{fmtSize(v.params_b)} · {fmtPct(v.pass_rate)}</div>
          </div>
        ))}
      </div>
    </div>
  )
}

function Chip({ label, value }) {
  return (
    <span className="score-chip">
      <span className="chip-label">{label}</span>
      <span className="chip-val">{value}</span>
    </span>
  )
}
