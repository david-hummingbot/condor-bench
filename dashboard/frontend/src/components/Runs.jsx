import { useState } from 'react'
import { getRun } from '../api.js'
import { fmtLatency, fmtScore, fmtTime, scoreColor, shortModel, weightSummary } from '../utils.js'
import CaseTable from './CaseTable.jsx'
import PageHeader from './PageHeader.jsx'
import EmptyState from './EmptyState.jsx'

export default function Runs({ runs, onRefresh, onNavigate, config }) {
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
      <PageHeader
        title="Runs"
        description="Every scored run, newest first. Open one to see its per-case verdicts, judge reasoning, and raw responses."
        meta={`${runs.length} run${runs.length !== 1 ? 's' : ''}`}
      >
        <button className="btn sm" onClick={onRefresh}>↻ Refresh</button>
      </PageHeader>

      {runs.length === 0 ? (
        <div className="card">
          <EmptyState
            title="No runs yet"
            description="Start a benchmark and its results land here as soon as the judge has scored them."
            actions={[
              { label: '▶ New benchmark', primary: true, onClick: () => onNavigate?.('#/run/benchmark') },
              { label: 'Browse suites', onClick: () => onNavigate?.('#/suites') },
            ]}
          />
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
                <div className="run-model">
                  {shortModel(r.model) || r.run_dir}
                  {r.run_type === 'custom-prompt' && (
                    <span style={{
                      marginLeft: 6, fontSize: 10, padding: '1px 5px',
                      background: 'rgba(79,140,255,0.15)', color: 'var(--accent)',
                      borderRadius: 3, verticalAlign: 'middle',
                    }}>prompt</span>
                  )}
                </div>
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
                <EmptyState
                  title="Select a run"
                  description="Pick a run on the left to see its scores case by case. To compare models against each other instead, use the Leaderboard or Matrix."
                  actions={[
                    { label: 'Leaderboard →', onClick: () => onNavigate?.('#/results/leaderboard') },
                    { label: 'Matrix →', onClick: () => onNavigate?.('#/results/matrix') },
                  ]}
                />
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
                  {detail.summary?.prompt_question && (
                    <div style={{
                      marginBottom: 16, padding: '10px 12px',
                      background: 'var(--panel-2)', borderRadius: 6,
                      fontSize: 13, lineHeight: 1.5, color: 'var(--text)',
                    }}>
                      <span style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', display: 'block', marginBottom: 4 }}>Prompt</span>
                      {detail.summary.prompt_question}
                    </div>
                  )}
                  <SummaryMetrics s={detail.summary} weights={config?.scoring?.weights} />
                  <RunFlags s={detail.summary} />
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

/**
 * Every weighted component of the composite, not a subset.
 *
 * Params and live validity carry 0.25 of the composite between them, so a
 * breakdown that stopped at quality/tools/latency left a quarter of the score
 * unaccounted for — a run could sit well below its quality average with nothing
 * on the card explaining why.
 */
function SummaryMetrics({ s, weights }) {
  if (!s) return null
  const summary = weightSummary(weights)
  return (
    <>
      <div className="metrics-grid">
        {[
          { label: 'Composite', value: s.composite_avg },
          { label: 'Answer quality', value: s.answer_quality_avg },
          { label: 'Tool accuracy', value: s.tool_accuracy_avg },
          { label: 'Tool params', value: s.tool_params_avg },
          { label: 'Live validity', value: s.live_validity_avg },
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
      {summary && (
        <div className="run-meta" style={{ marginTop: 10 }}>
          Composite = {summary}. A component with no ground truth for a case scores
          nothing and its weight moves to answer quality — that is why a “—” here does
          not drag the composite down.
        </div>
      )}
    </>
  )
}

/**
 * Run-level counts the scorer now reports: rows blamed on the harness (excluded from
 * routing) and rows whose asserted end state never materialised (capped, so they
 * cannot pass). Per-case flags already exist in the table; without the roll-up you
 * have to open every row to find out whether the run had any.
 */
function RunFlags({ s }) {
  const artifacts = s?.harness_artifacts || 0
  const unbuilt = s?.post_condition_failures || 0
  const infra = s?.infra_excluded || 0
  if (!artifacts && !unbuilt && !infra) return null
  return (
    <div style={{ marginTop: 12, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {infra > 0 && (
        <span className="router-flag" title="Infrastructure failures — excluded from the averages rather than scored zero">
          {infra} infra excluded
        </span>
      )}
      {artifacts > 0 && (
        <span className="router-flag" title={(s.harness_artifact_cases || []).map(c => `${c.case_id}: ${c.reason}`).join('\n')}>
          {artifacts} harness artifact{artifacts !== 1 ? 's' : ''}
        </span>
      )}
      {unbuilt > 0 && (
        <span className="router-flag" title={(s.post_condition_failure_cases || []).map(c => `${c.case_id}: ${c.reason}`).join('\n')}>
          {unbuilt} post-condition failure{unbuilt !== 1 ? 's' : ''}
        </span>
      )}
    </div>
  )
}
