import {
  Bar, BarChart, CartesianGrid, Cell,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { buildLeaderboard, fmtLatency, fmtScore, scoreColor, shortModel, weightSummary } from '../utils.js'
import PageHeader from './PageHeader.jsx'
import EmptyState from './EmptyState.jsx'

const DESCRIPTION =
  'Overall model ranking by composite score, using each model’s most recent run. ' +
  'For per-domain strengths rather than one number per model, use the Matrix.'

export default function Leaderboard({ runs, onNavigate, config }) {
  if (!runs || runs.length === 0) {
    return (
      <div>
        <PageHeader title="Leaderboard" description={DESCRIPTION} />
        <div className="card">
          <EmptyState
            title="Nothing to rank yet"
            description="The leaderboard needs at least one scored run before it can order models."
            actions={[
              { label: '▶ New benchmark', primary: true, onClick: () => onNavigate?.('#/run/benchmark') },
            ]}
          />
        </div>
      </div>
    )
  }

  const rows = buildLeaderboard(runs)
  const weights = weightSummary(config?.scoring?.weights)
  const chartData = rows.map(r => ({
    model: shortModel(r.model),
    fullModel: r.model,
    composite: Number((r.composite_avg ?? 0).toFixed(3)),
  }))

  return (
    <div>
      <PageHeader
        title="Leaderboard"
        description={DESCRIPTION}
        meta={`${rows.length} model${rows.length !== 1 ? 's' : ''}`}
      >
        <button className="btn sm" onClick={() => onNavigate?.('#/results/matrix')}>
          Matrix →
        </button>
      </PageHeader>

      <div className="card">
        <div className="card-title">Model ranking</div>
        <div style={{ height: 60 + chartData.length * 48 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart layout="vertical" data={chartData} margin={{ left: 20, right: 60, top: 4, bottom: 4 }}>
              <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="var(--border)" />
              <XAxis type="number" domain={[0, 1]} tick={{ fontSize: 11, fill: 'var(--muted)' }} />
              <YAxis
                type="category"
                dataKey="model"
                width={160}
                tick={{ fontSize: 12, fill: 'var(--text)', fontFamily: 'ui-monospace, monospace' }}
              />
              <Tooltip
                contentStyle={{ background: 'var(--panel)', border: '1px solid var(--border)', borderRadius: 8 }}
                formatter={v => [fmtScore(v), 'Composite']}
                labelFormatter={label => {
                  const row = chartData.find(d => d.model === label)
                  return row?.fullModel ?? label
                }}
              />
              <Bar dataKey="composite" radius={[0, 4, 4, 0]}
                label={{ position: 'right', formatter: fmtScore, fill: 'var(--muted)', fontSize: 11 }}>
                {chartData.map(d => (
                  <Cell key={d.model} fill={scoreColor(d.composite)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card">
        <div className="card-title">Score breakdown — latest run per model</div>
        {/* Every weighted component, not a subset: params and live validity are 0.25
            of the composite between them, so leaving them out made the columns fail
            to add up to the number they sit next to. */}
        {weights && (
          <div className="matrix-note" style={{ marginTop: -8, marginBottom: 14 }}>
            Composite = {weights}. A metric shows “—” when no case in the run pinned
            ground truth for it; its weight moves to answer quality rather than
            scoring zero.
          </div>
        )}
        <table className="lb-table">
          <thead>
            <tr>
              <th className="rank-cell">#</th>
              <th>Model</th>
              <th>Composite</th>
              <th>Quality</th>
              <th>Tools</th>
              <th>Params</th>
              <th>Live validity</th>
              <th>Latency score</th>
              <th>Avg latency</th>
              <th>Cases</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.model}>
                <td className="rank-cell">{i + 1}</td>
                <td>
                  <div className="model-cell">{shortModel(r.model)}</div>
                  <div className="model-provider">{r.model?.split(':')[0]}</div>
                </td>
                <td style={{ color: scoreColor(r.composite_avg), fontWeight: 600 }}>
                  {fmtScore(r.composite_avg)}
                </td>
                <td style={{ color: scoreColor(r.answer_quality_avg) }}>
                  {fmtScore(r.answer_quality_avg)}
                </td>
                <td style={{ color: scoreColor(r.tool_accuracy_avg) }}>
                  {fmtScore(r.tool_accuracy_avg)}
                </td>
                <td style={{ color: scoreColor(r.tool_params_avg) }}>
                  {fmtScore(r.tool_params_avg)}
                </td>
                <td style={{ color: scoreColor(r.live_validity_avg) }}>
                  {fmtScore(r.live_validity_avg)}
                </td>
                <td style={{ color: scoreColor(r.latency_score_avg) }}>
                  {fmtScore(r.latency_score_avg)}
                </td>
                <td style={{ color: 'var(--muted)' }}>{fmtLatency(r.latency_s_avg)}</td>
                <td style={{ color: 'var(--muted)' }}>{r.cases_scored ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
