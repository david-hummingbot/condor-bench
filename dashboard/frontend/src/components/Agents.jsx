import { useEffect, useState } from 'react'
import { getHubAgents } from '../api.js'
import {
  fmtCost, fmtLatency, fmtPct, fmtScore, fmtTime, heatColor, providerOf, scoreColor, shortModel,
} from '../utils.js'
import PageHeader from './PageHeader.jsx'
import EmptyState from './EmptyState.jsx'

const DESCRIPTION =
  'Hub agents that have been benchmarked, and how each model did against them. ' +
  'Scores pool every scored case across a model’s runs of the agent’s current version.'

export default function Agents({ onNavigate, focus }) {
  const [agents, setAgents] = useState(null)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(focus || null)

  const load = () => {
    setError('')
    getHubAgents()
      .then((d) => setAgents(d.agents || []))
      .catch((e) => setError(e.message))
  }
  useEffect(load, [])
  useEffect(() => { if (focus) setSelected(focus) }, [focus])

  const pick = (slug) => {
    setSelected(slug)
    onNavigate?.(`#/results/agents/${encodeURIComponent(slug)}`)
  }

  const agent = agents?.find((a) => a.slug === selected) || agents?.[0]

  return (
    <div>
      <PageHeader
        title="Agents"
        description={DESCRIPTION}
        meta={agents ? `${agents.length} agent${agents.length !== 1 ? 's' : ''}` : null}
      >
        <button className="btn sm" onClick={load}>↻ Refresh</button>
      </PageHeader>

      {error && <div className="card"><div className="error-text">{error}</div></div>}
      {!agents && !error && (
        <div className="card"><div className="empty" style={{ padding: '32px 24px' }}>Loading…</div></div>
      )}

      {agents && agents.length === 0 && (
        <div className="card">
          <EmptyState
            title="No hub agents benchmarked yet"
            description={
              'Run one from the CLI — uv run python runner.py hub hub:<slug> -m <model> -m <model> — ' +
              'and its results are grouped here by agent and model.'
            }
          />
        </div>
      )}

      {agent && (
        <div className="runs-layout">
          <div className="runs-sidebar">
            {agents.map((a) => {
              const best = a.models.find((m) => m.composite != null)
              return (
                <div
                  key={a.slug}
                  className={`run-item ${a.slug === agent.slug ? 'active' : ''}`}
                  onClick={() => pick(a.slug)}
                >
                  <div className="run-model">{a.name}</div>
                  <div className="run-meta">hub:{a.slug} · v{a.version ?? '?'}</div>
                  <div className="run-meta" style={{ marginTop: 4 }}>
                    {a.models.length} model{a.models.length !== 1 ? 's' : ''}
                    {best && (
                      <>
                        {' · best '}
                        <span style={{ color: scoreColor(best.composite), fontWeight: 600 }}>
                          {fmtScore(best.composite)}
                        </span>
                        {' '}{shortModel(best.model)}
                      </>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
          <AgentDetail agent={agent} onNavigate={onNavigate} />
        </div>
      )}
    </div>
  )
}

function AgentDetail({ agent, onNavigate }) {
  const models = agent.models
  return (
    <div>
      <div className="card">
        <div className="card-head">
          <div className="card-title">{agent.name}</div>
          <span className="muted">
            hub:{agent.slug} · v{agent.version ?? '?'} · last run {fmtTime(agent.last_run)}
          </span>
        </div>
        <div className="run-meta" style={{ lineHeight: 1.7 }}>
          <div><strong className="agent-k">Skills</strong> {agent.skills.join(', ') || '—'}</div>
          <div><strong className="agent-k">Loops</strong> {agent.loops.join(', ') || '—'}</div>
          {agent.code_files_skipped.length > 0 && (
            <div><strong className="agent-k">Not installed</strong> {agent.code_files_skipped.join(', ')}</div>
          )}
        </div>
        {agent.findings.length > 0 && (
          <ul className="agent-findings">
            {agent.findings.map((f) => <li key={f}>{f}</li>)}
          </ul>
        )}
      </div>

      <div className="card">
        <div className="card-title">Models</div>
        <div className="matrix-scroll">
          <table className="lb-table agents-models">
            <thead>
              <tr>
                <th className="rank-cell">#</th>
                <th>Model</th>
                <th>Composite</th>
                <th>Pass</th>
                <th>Quality</th>
                <th title="Mean model time of a passing case">Time/pass</th>
                <th title="Mean tool calls of a passing case">Calls/pass</th>
                <th>Cost/pass</th>
                <th title="Scored case records / all case records, over every run">Cases</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m, i) => (
                <tr key={m.model}>
                  <td className="rank-cell">{m.composite != null ? i + 1 : ''}</td>
                  <td>
                    <div className="model-cell">{shortModel(m.model)}</div>
                    <div className="model-provider">{providerOf(m.model)}</div>
                  </td>
                  <td style={{ color: scoreColor(m.composite), fontWeight: 600 }}>{fmtScore(m.composite)}</td>
                  <td>
                    {m.pass_rate == null ? '—' : `${fmtPct(m.pass_rate)}`}
                    {m.pass_rate != null && <div className="muted">{m.passed}/{m.cases_scored}</div>}
                  </td>
                  <td style={{ color: scoreColor(m.answer_quality) }}>{fmtScore(m.answer_quality)}</td>
                  <td style={{ color: 'var(--muted)' }}>{fmtLatency(m.model_time_per_success)}</td>
                  <td style={{ color: 'var(--muted)' }}>
                    {m.tool_calls_per_success == null ? '—' : m.tool_calls_per_success.toFixed(1)}
                  </td>
                  <td style={{ color: 'var(--muted)' }}>{fmtCost(m.cost_per_success)}</td>
                  <td style={{ color: 'var(--muted)' }}>
                    {m.cases_scored}/{m.cases_total}
                    <div className="muted">{m.runs} run{m.runs !== 1 ? 's' : ''}</div>
                    {m.runs_not_scored > 0 && (
                      <span
                        className="router-flag"
                        title="Every case in these runs hit a harness fault or error, so they carry no score. Open the run below for the reason."
                      >
                        {m.runs_not_scored} not scored
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {models.some((m) => m.unsafe > 0) && (
          <div className="matrix-note" style={{ color: 'var(--red)' }}>
            {models.filter((m) => m.unsafe > 0).map((m) => `${shortModel(m.model)}: ${m.unsafe} case(s) made a forbidden call`).join(' · ')}
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-title">Cases × models</div>
        <div className="matrix-scroll">
          <table className="matrix-table agents-cases">
            <thead>
              <tr>
                <th className="matrix-corner" />
                {models.map((m) => (
                  <th key={m.model}><div className="matrix-model">{shortModel(m.model)}</div></th>
                ))}
              </tr>
            </thead>
            <tbody>
              {agent.cases.map((c) => (
                <tr key={c.case_id}>
                  <th className="matrix-row-label" title={c.case_id}>{c.label}</th>
                  {models.map((m) => {
                    const cell = c.by_model[m.model]
                    if (!cell) return <td key={m.model} className="matrix-cell matrix-empty">—</td>
                    return (
                      <td
                        key={m.model}
                        className="matrix-cell"
                        style={{ background: heatColor(cell.composite) }}
                        title={
                          `composite ${fmtScore(cell.composite)} · quality ${fmtScore(cell.answer_quality)}\n` +
                          `passed ${cell.passed}/${cell.scored} scored · ${cell.trials} trial(s)`
                        }
                      >
                        <span className="matrix-value">{fmtScore(cell.composite)}</span>
                        <span className="matrix-n">
                          {cell.scored ? `${cell.passed}/${cell.scored} pass` : 'not scored'}
                        </span>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Runs</div>
        <table className="data-table">
          <thead>
            <tr>
              <th>When</th>
              <th>Model</th>
              <th>Composite</th>
              <th>Pass</th>
              <th>Cases</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {agent.runs.map((r) => (
              <tr key={r.run_dir} style={{ opacity: r.current_version ? 1 : 0.55 }}>
                <td className="muted">{fmtTime(r.timestamp)}</td>
                <td>
                  {shortModel(r.model)}
                  {!r.current_version && (
                    <span className="router-flag" style={{ marginLeft: 6 }} title="Run against an earlier version of this agent — not in the scores above">
                      older version
                    </span>
                  )}
                  {r.execute && <span className="router-flag" style={{ marginLeft: 6 }}>execute</span>}
                </td>
                <td style={{ color: scoreColor(r.composite), fontWeight: 600 }}>{fmtScore(r.composite)}</td>
                <td>{fmtPct(r.pass_rate)}</td>
                <td className="muted" title={r.not_scored_reasons.join('\n')}>
                  {r.cases_scored}/{r.cases_total} scored
                </td>
                <td style={{ textAlign: 'right' }}>
                  <button className="btn sm ghost" onClick={() => onNavigate?.(`#/results/runs/${r.run_dir}`)}>
                    Open →
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
