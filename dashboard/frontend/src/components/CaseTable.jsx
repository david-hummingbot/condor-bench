import { Fragment, useState } from 'react'
import { fmtCost, fmtLatency, fmtScore, fmtTokens, PASS_THRESHOLD, scoreColor } from '../utils.js'
import casePrompts from '../casePrompts.json'
import caseTypes from '../caseTypes.json'

function caseQuestion(c) {
  return c.question || casePrompts[c.case_id] || ''
}

/**
 * Dataset layer for a case row.
 *
 * The persisted `case_type` is authoritative. Runs saved before that field existed
 * fall back to the case library, and only then to an id-prefix guess — the guess
 * cannot get chat-scoped Layer 3 cases right, because those were merged into the
 * consult layer while keeping their `agent_*` ids, so it labels eight of the
 * current cases as the wrong layer.
 */
function caseType(c) {
  if (c.case_type) return c.case_type
  const known = caseTypes[c.case_id]
  if (known) return known
  if (c.case_id?.startsWith('tool_')) return 'tool'
  if (c.case_id?.startsWith('agent_')) return 'agent'
  if (c.domain === 'tick_execution' || /^t\d/.test(c.case_id || '')) return 'tick'
  return 'consult'
}

const COLUMNS = 12

export default function CaseTable({ cases }) {
  const [expanded, setExpanded] = useState(null)

  if (!cases || cases.length === 0) {
    return <div className="empty" style={{ padding: '24px' }}>No cases in this run.</div>
  }

  return (
    <div className="table-scroll case-table-scroll">
      <table className="case-table">
        <thead>
          <tr>
            <th>Case</th>
            <th>Type</th>
            <th>Domain</th>
            <th>Risk</th>
            <th title="0.45 quality + 0.20 tools + 0.15 params + 0.10 live validity + 0.10 latency. A component with no ground truth scores nothing and its weight moves to quality.">
              Composite
            </th>
            <th>Quality</th>
            <th title="Ordered phases for build cases, recall for job cases, F1 for Layer 2 probes. Zeroed outright by a banned call.">
              Tools
            </th>
            <th title="Pinned parameters the case named. Blank when it pinned none.">Params</th>
            <th title="Live validity: did the real API responses look right, plus any post-condition probe. Blank when the case asserts nothing.">
              Valid
            </th>
            <th>Latency</th>
            <th>Tokens</th>
            <th>Pass</th>
          </tr>
        </thead>
        <tbody>
          {cases.map(c => {
            const isOpen = expanded === c.case_id
            const passed = !c.error && (c.composite ?? 0) >= PASS_THRESHOLD
            const ctype = caseType(c)
            const question = caseQuestion(c)
            const risk = c.risk_level || 'read_only'
            return (
              <Fragment key={c.case_id}>
                <tr
                  className="expand-row"
                  onClick={() => setExpanded(isOpen ? null : c.case_id)}
                >
                  <td>
                    <span style={{ color: 'var(--muted)', marginRight: 6 }}>{isOpen ? '▾' : '▸'}</span>
                    <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 12 }}>{c.case_id}</span>
                    {c.harness_artifact && (
                      <span
                        className="router-flag"
                        title={`Excluded from the routing matrix: ${c.harness_artifact}`}
                      >
                        harness
                      </span>
                    )}
                    {c.post_condition_failed && (
                      <span
                        className="router-flag"
                        title={`Composite capped — ${c.post_condition_failed}`}
                      >
                        not built
                      </span>
                    )}
                    {c.forbidden_violations?.length > 0 && (
                      <span
                        className="router-flag"
                        title={`Tool score zeroed — called a banned action: ${c.forbidden_violations.join(', ')}`}
                      >
                        banned call
                      </span>
                    )}
                  </td>
                  <td><span className={`type-tag ${ctype}`}>{ctype}</span></td>
                  <td style={{ color: 'var(--muted)', fontSize: 12, textAlign: 'left' }}>
                    {c.domain || c.category || '—'}
                  </td>
                  <td style={{ textAlign: 'left' }}>
                    <span className={`risk-tag ${risk}`}>{risk.replace('_', ' ')}</span>
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.composite), fontWeight: 600 }}>
                    {c.error ? <span style={{ color: 'var(--red)', fontSize: 11 }}>ERR</span> : fmtScore(c.composite)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.answer_quality) }}>
                    {c.error ? '—' : fmtScore(c.answer_quality)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.tool_accuracy) }}>
                    {c.error ? '—' : fmtScore(c.tool_accuracy)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.tool_params) }}>
                    {c.error ? '—' : fmtScore(c.tool_params)}
                  </td>
                  <td style={{ textAlign: 'right', color: scoreColor(c.live_validity) }}>
                    {c.error ? '—' : fmtScore(c.live_validity)}
                  </td>
                  <td style={{ textAlign: 'right', color: 'var(--muted)' }}>
                    {fmtLatency(c.latency_s)}
                  </td>
                  <td style={{ textAlign: 'right', color: 'var(--muted)' }}>
                    {fmtTokens(c.usage?.total_tokens)}
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
                  <tr>
                    <td colSpan={COLUMNS} style={{ padding: '0 12px 12px' }}>
                      <div className="case-detail">
                        {c.error && (
                          <div className="error-text" style={{ marginBottom: 10 }}>{c.error}</div>
                        )}
                        {c.harness_artifact && (
                          <div style={{ marginBottom: 10, color: 'var(--yellow)', fontSize: 12 }}>
                            Harness artifact — excluded from the routing matrix rather than
                            counted as a model failure: {c.harness_artifact}
                          </div>
                        )}
                        {c.post_condition_failed && (
                          <div className="error-text" style={{ marginBottom: 10 }}>
                            {c.post_condition_failed}. This is the model's failure, not the
                            harness's — the composite is capped so the case cannot pass, however
                            convincing the answer read.
                          </div>
                        )}
                        {c.forbidden_violations?.length > 0 && (
                          <div className="error-text" style={{ marginBottom: 10 }}>
                            Called a banned action: {c.forbidden_violations.join(', ')} — the tool
                            score is 0.00 outright, not docked. A restraint violation is not a
                            partial-credit situation.
                          </div>
                        )}
                        <div className="case-detail-grid">
                          <div>
                            <div className="case-detail-label">Response</div>
                            <div className="case-detail-text">
                              {question && (
                                <div className="case-question">
                                  <span className="case-question-label">Question</span>
                                  {question}
                                </div>
                              )}
                              {c.response || '(no response)'}
                            </div>
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
                            ['Params', c.tool_params],
                            ['Live validity', c.live_validity],
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

                        <TokenChips usage={c.usage} judge={c.judge_usage} />
                        <PhaseDetail phases={c.phase_detail} />
                        <ToolTrace
                          calls={c.tool_call_details}
                          expected={c.expected_tools}
                          internal={c.agent_internal_calls}
                        />
                        <ParamDetail detail={c.tool_param_detail} />
                        <ValidityDetail detail={c.live_validity_detail} />
                        <WiringDetail wiring={c.wiring} />
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function TokenChips({ usage, judge }) {
  const hasUsage = usage && Object.keys(usage).length > 0
  const hasJudge = judge && Object.keys(judge).length > 0
  if (!hasUsage && !hasJudge) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div className="case-detail-label">Tokens</div>
      <div className="score-chips" style={{ marginTop: 6 }}>
        {hasUsage ? (
          <>
            <Chip label="In" value={fmtTokens(usage.input_tokens)} />
            <Chip label="Out" value={fmtTokens(usage.output_tokens)} />
            {usage.cache_read_tokens != null && (
              <Chip label="Cache read" value={fmtTokens(usage.cache_read_tokens)} />
            )}
            <Chip label="Total" value={fmtTokens(usage.total_tokens)} />
            <Chip label="Cost" value={fmtCost(usage.cost_usd)} />
          </>
        ) : (
          /* An unmeasured backend is not a free one — say which it is. */
          <Chip label="Model tokens" value="not reported by this backend" />
        )}
        {hasJudge && (
          <Chip
            label="Judge"
            value={`${fmtTokens(judge.total_tokens)} · ${fmtCost(judge.cost_usd)} (not scored)`}
          />
        )}
      </div>
    </div>
  )
}

/**
 * Ordered build phases, present only when the case declares `steps`. Without this
 * a phase-scored tool number is unreadable: 0.50 could be a skipped phase or the
 * right calls in the wrong order, and the row gives no way to tell which.
 */
function PhaseDetail({ phases }) {
  if (!phases?.length) return null
  const met = phases.filter(p => p.satisfied).length
  return (
    <div style={{ marginTop: 12 }}>
      <div className="case-detail-label">
        Build phases — {met}/{phases.length} satisfied
      </div>
      <div className="run-meta" style={{ marginBottom: 6 }}>
        Scored on ordered phases rather than tool-name F1: extra calls and retries cost
        nothing, skipping a phase or doing it out of order costs that phase's share.
      </div>
      {phases.map((p, i) => (
        <div key={i} className="run-meta">
          <span style={{ color: p.satisfied ? 'var(--green)' : 'var(--red)' }}>
            {p.satisfied ? '✓' : '✗'}
          </span>{' '}
          {p.phase}
          {p.required?.length > 0 && (
            <span style={{ fontFamily: 'ui-monospace, monospace' }}> ({p.required.join(', ')})</span>
          )}
          {p.missing_or_out_of_order?.length > 0 && (
            <span style={{ color: 'var(--red)' }}>
              {' '}— missing or out of order: {p.missing_or_out_of_order.join(', ')}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

function ToolTrace({ calls, expected, internal }) {
  if (!calls?.length && !expected?.length) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div className="case-detail-label">Tool calls</div>
      {expected?.length > 0 && (
        <div className="run-meta">expected: {expected.join(', ')}</div>
      )}
      {/* Named rather than quietly dropped: the agent's own tools are in the trace
          below but excluded from the tool, param and validity scores, because they
          are not choices about condor's surface. */}
      {internal?.length > 0 && (
        <div className="run-meta" style={{ color: 'var(--yellow)' }}>
          not scored — the agent's own tools: {internal.join(', ')}
        </div>
      )}
      {!calls?.length ? (
        <div className="case-detail-text">(no tool calls)</div>
      ) : (
        calls.map((call, i) => (
          <pre key={i} className="config-snippet" style={{ marginTop: 6 }}>
            {call.tool}({JSON.stringify(call.args ?? {}, null, 2)})
          </pre>
        ))
      )}
    </div>
  )
}

function ParamDetail({ detail }) {
  if (!detail || !Object.keys(detail).length) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div className="case-detail-label">Pinned parameters</div>
      {Object.entries(detail).map(([tool, d]) => (
        <div key={tool} className="run-meta">
          <span style={{ fontFamily: 'ui-monospace, monospace' }}>{tool}</span>
          {!d.called && <span style={{ color: 'var(--red)' }}> — never called</span>}
          {d.matched?.length > 0 && (
            <span style={{ color: 'var(--green)' }}> ✓ {d.matched.join(', ')}</span>
          )}
          {d.mismatched && Object.keys(d.mismatched).length > 0 && (
            <span style={{ color: 'var(--red)' }}>
              {' '}✗ {Object.entries(d.mismatched)
                .map(([k, v]) => `${k}: wanted ${JSON.stringify(v.expected)}, got ${JSON.stringify(v.actual)}`)
                .join('; ')}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}

function ValidityDetail({ detail }) {
  const rows = detail?.responses
  if (!rows?.length) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div className="case-detail-label">Live tool responses</div>
      {rows.map((r, i) => (
        <div key={i} className="run-meta">
          <span style={{ fontFamily: 'ui-monospace, monospace' }}>{r.tool}</span>
          {' '}
          <span style={{ color: r.score >= 0.8 ? 'var(--green)' : 'var(--red)' }}>
            {fmtScore(r.score)}
          </span>
          {r.error && <span style={{ color: 'var(--red)' }}> — {r.error}</span>}
          {r.empty && <span style={{ color: 'var(--yellow)' }}> — empty payload</span>}
          {r.preview && <div className="case-detail-text">{r.preview}</div>}
        </div>
      ))}
      {detail.unfulfilled_assertions?.length > 0 && (
        <div className="run-meta" style={{ color: 'var(--yellow)' }}>
          assertions with no matching call: {detail.unfulfilled_assertions.join(', ')}
        </div>
      )}
    </div>
  )
}

function WiringDetail({ wiring }) {
  if (!wiring || !Object.keys(wiring).length) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div className="case-detail-label">MCP wiring</div>
      <div className="score-chips" style={{ marginTop: 6 }}>
        <Chip label="agent_slug" value={wiring.agent_slug ?? 'chat-scoped'} />
        {wiring.api_url && <Chip label="API" value={wiring.api_url} />}
        {wiring.server_name && <Chip label="Server" value={wiring.server_name} />}
        {wiring.tool_count_effective != null && (
          <Chip label="Tools offered" value={String(wiring.tool_count_effective)} />
        )}
        {wiring.assistant_prompt && <Chip label="Prompt" value={wiring.assistant_prompt} />}
      </div>
      {wiring.autodiscovery_extras?.length > 0 && (
        <div className="run-meta" style={{ color: 'var(--yellow)', marginTop: 6 }}>
          ACP auto-discovery added {wiring.autodiscovery_extras.join(', ')} from
          condor/.mcp.json — tool counts here are not comparable to the PydanticAI path.
        </div>
      )}
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
