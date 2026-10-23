import { Fragment, useCallback, useEffect, useMemo, useState } from 'react'
import {
  compareRuns,
  getCompareCondor,
  getModelCatalog,
  getRun,
  listSuiteRuns,
  listSuites,
  prepareCandidate,
  removeWorktree,
  startCompare,
} from '../api.js'
import PageHeader from './PageHeader.jsx'
import ModelPicker, { catalogOptions } from './ModelPicker.jsx'
import { CompareView } from './Suites.jsx'

const BASELINE_ENV = 'condor-baseline'

/**
 * Did a Condor change make it better or worse at what users actually ask for?
 *
 * One model, two checkouts (Experiment A): the Condor bench is configured with,
 * against a branch, a PR or another folder. A branch or PR becomes a git worktree
 * bench manages, so nothing you have checked out is touched. The cases are the
 * practical set — balances, prices, candles, orders, leverage, safety — so the
 * comparison reads as "placing an order got worse", not as a score delta.
 */
export default function CompareCondor({ onRunStarted }) {
  const [info, setInfo] = useState(null)
  const [error, setError] = useState('')
  const [mode, setMode] = useState('ref') // ref | folder
  const [ref, setRef] = useState('')
  const [folder, setFolder] = useState('')
  const [preparing, setPreparing] = useState(false)
  const [prepared, setPrepared] = useState(null) // { candidate, steps }
  const [modelOptions, setModelOptions] = useState([])
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [model, setModel] = useState('')
  const [trials, setTrials] = useState(3)
  const [starting, setStarting] = useState(false)

  const load = useCallback(async () => {
    try {
      setInfo(await getCompareCondor())
    } catch (e) {
      setError(e.message)
    }
  }, [])

  // Live catalogues (OpenRouter, Groq, Ollama, LM Studio) grouped by provider.
  const loadCatalog = useCallback(async (refresh = false) => {
    setCatalogLoading(true)
    try {
      const opts = catalogOptions(await getModelCatalog(refresh))
      setModelOptions(opts)
      setModel((m) => m || opts.find((o) => !o.note)?.id || '')
    } catch {
      /* the picker still accepts a typed key */
    } finally {
      setCatalogLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
    loadCatalog()
  }, [load, loadCatalog])

  const cases = info?.case_sets?.practical || []
  const base = info?.baseline

  const handlePrepare = async () => {
    setPreparing(true)
    setError('')
    setPrepared(null)
    try {
      const body = mode === 'ref' ? { ref: ref.trim() } : { folder: folder.trim() }
      setPrepared(await prepareCandidate(body))
      load()
    } catch (e) {
      setError(e.message)
    } finally {
      setPreparing(false)
    }
  }

  const handleCompare = async () => {
    if (!prepared || !model) return
    setStarting(true)
    setError('')
    try {
      const c = prepared.candidate
      const data = await startCompare({
        candidate_path: c.path,
        label: c.branch || c.label,
        model: { model_key: model },
        trials,
        case_set: 'practical',
      })
      onRunStarted?.(data.run_id)
    } catch (e) {
      setError(e.message)
    } finally {
      setStarting(false)
    }
  }

  const handleRemove = async (c) => {
    const slug = c.path.split('/').pop()
    if (!window.confirm(`Remove the bench worktree ${slug}? Its folder is deleted.`)) return
    try {
      await removeWorktree(slug)
      if (prepared?.candidate?.path === c.path) setPrepared(null)
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  const managed = (c) => info?.worktree_root && c.path.startsWith(info.worktree_root)

  return (
    <div>
      <PageHeader
        title="Compare Condor"
        description="Run the same model through your current Condor and a branch, PR or other checkout, on everyday tasks — then see task by task what got better or worse."
      />

      {error && <div className="card"><div className="error-text">{error}</div></div>}

      <div className="card">
        <div className="card-title">1 · Baseline — your current Condor</div>
        {base ? (
          <CheckoutLine c={base} />
        ) : (
          <div className="muted">Loading…</div>
        )}
        <div className="run-meta" style={{ marginTop: 6 }}>
          Fixed: this is the checkout bench is configured with (CONDOR_PATH).
        </div>
      </div>

      <div className="card">
        <div className="card-title">2 · Compare against</div>
        <div className="radio-row" style={{ marginBottom: 12 }}>
          <button type="button" className={`radio-btn ${mode === 'ref' ? 'active' : ''}`} onClick={() => setMode('ref')}>
            A branch or PR
          </button>
          <button type="button" className={`radio-btn ${mode === 'folder' ? 'active' : ''}`} onClick={() => setMode('folder')}>
            A folder I already have
          </button>
        </div>

        {mode === 'ref' ? (
          <div className="field">
            <label>Branch name or PR number</label>
            <input
              className="input"
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="feature/new-executor-api   or   #288"
              style={{ maxWidth: 420 }}
            />
            <span className="run-meta">
              Fetched from the baseline&apos;s <code>origin</code> and checked out as a worktree in{' '}
              <code>{info?.worktree_root || '.condor-worktrees/'}</code>. Your own checkouts are not touched.
            </span>
          </div>
        ) : (
          <div className="field">
            <label>Condor folder</label>
            <input
              className="input"
              list="condor-candidates"
              value={folder}
              onChange={(e) => setFolder(e.target.value)}
              placeholder="/home/you/development/condor-feature"
              style={{ maxWidth: 520 }}
            />
            <datalist id="condor-candidates">
              {(info?.candidates || []).map((c) => (
                <option key={c.path} value={c.path}>{c.label}</option>
              ))}
            </datalist>
            {(info?.candidates || []).length > 0 && (
              <div style={{ marginTop: 8 }}>
                <div className="run-meta">Found on this machine — click to use:</div>
                {info.candidates.map((c) => (
                  <div key={c.path} className="row-inline" style={{ alignItems: 'center', gap: 8, marginTop: 4 }}>
                    <button type="button" className="btn sm" onClick={() => setFolder(c.path)}>Use</button>
                    <CheckoutLine c={c} compact />
                    {managed(c) && (
                      <button type="button" className="btn sm danger" onClick={() => handleRemove(c)} title="Delete this bench-managed worktree">
                        Remove
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div style={{ marginTop: 12 }}>
          <button
            className="btn"
            onClick={handlePrepare}
            disabled={preparing || (mode === 'ref' ? !ref.trim() : !folder.trim())}
          >
            {preparing ? 'Preparing… (first time can take a minute)' : 'Prepare'}
          </button>
        </div>

        {prepared && (
          <div style={{ marginTop: 12 }}>
            <div style={{ color: 'var(--green)', fontWeight: 600, marginBottom: 4 }}>✓ Ready</div>
            <CheckoutLine c={prepared.candidate} />
            {prepared.steps?.length > 0 && (
              <div className="run-meta" style={{ marginTop: 4 }}>{prepared.steps.join(' · ')}</div>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-title">3 · Run the comparison</div>
        <div className="field" style={{ maxWidth: 420 }}>
          <label>
            Model (the same one runs both sides)
            <button type="button" className="btn sm" style={{ marginLeft: 8 }} onClick={() => loadCatalog(true)} disabled={catalogLoading}>
              {catalogLoading ? 'Loading…' : '↻ Refresh list'}
            </button>
          </label>
          <ModelPicker
            models={modelOptions}
            value={model}
            onChange={setModel}
            placeholder="Choose a model"
          />
          <span className="run-meta">
            {modelOptions.filter((o) => !o.note).length} models, live where the provider allows. Not listed? Type its full key and press Enter.
          </span>
        </div>
        <div className="field">
          <label>
            Trials per task
            <span className="run-meta"> — 3 lets the per-task difference show through model noise</span>
          </label>
          <input
            className="input"
            type="number"
            min={1}
            max={10}
            value={trials}
            onChange={(e) => setTrials(Math.max(1, Math.min(10, parseInt(e.target.value, 10) || 1)))}
            style={{ maxWidth: 90 }}
          />
        </div>

        <details style={{ marginBottom: 12 }}>
          <summary className="muted">
            {cases.length} practical tasks — {cases.filter((c) => c.risk_level !== 'read_only').length} place
            or change something on staging (torn down afterwards)
          </summary>
          <table className="data-table" style={{ marginTop: 8 }}>
            <tbody>
              {cases.map((c) => (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td>
                  <td>{c.question}</td>
                  <td>
                    {c.risk_level !== 'read_only' && (
                      <span className="router-flag">{c.risk_level === 'destructive' ? 'trades' : 'changes state'}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="run-meta" style={{ marginTop: 6 }}>
            Edit the set in <code>datasets/suite_templates/practical.jsonl</code>.
          </div>
        </details>

        <button className="btn primary" onClick={handleCompare} disabled={!prepared || !model || starting}>
          {starting ? 'Starting…' : `▶ Compare (${cases.length} tasks × ${trials} trial${trials !== 1 ? 's' : ''} × 2 checkouts)`}
        </button>
        {!prepared && <span className="run-meta" style={{ marginLeft: 10 }}>Prepare the other checkout first.</span>}
      </div>

      <Results />
    </div>
  )
}

function CheckoutLine({ c, compact }) {
  return (
    <div style={{ fontSize: compact ? 12 : 13 }}>
      <code>{c.path}</code>
      {' · '}
      <strong>{c.branch || 'detached'}</strong>
      {c.commit && <span className="muted"> @ {c.commit}</span>}
      {c.dirty_files > 0 && <span className="router-flag" title="Uncommitted changes — recorded in each run's manifest">{c.dirty_files} uncommitted</span>}
      {(c.problems || []).map((p) => (
        <div key={p} className="run-meta" style={{ color: 'var(--yellow)' }}>{p}</div>
      ))}
    </div>
  )
}

/**
 * Past comparisons: pick one, see the overall verdict, then each task side by side.
 * Members are ordered baseline-first so every delta reads "candidate minus current".
 */
function Results() {
  const [suites, setSuites] = useState([])
  const [suiteId, setSuiteId] = useState('')
  const [groups, setGroups] = useState([])
  const [groupId, setGroupId] = useState('')
  const [compare, setCompare] = useState(null)
  const [rows, setRows] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    listSuites()
      .then((d) => {
        const cmp = (d.suites || []).filter((s) => s.id.startsWith('compare-'))
        setSuites(cmp)
        if (cmp.length) setSuiteId((id) => id || cmp[0].id)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (!suiteId) return
    listSuiteRuns(suiteId).then((d) => {
      const byGroup = {}
      for (const r of d.runs || []) {
        if (!r.run_group_id) continue
        ;(byGroup[r.run_group_id] = byGroup[r.run_group_id] || []).push(r)
      }
      const list = Object.entries(byGroup)
        .filter(([, members]) => members.length >= 2)
        .map(([id, members]) => ({ id, members, timestamp: members.map((m) => m.timestamp || '').sort().pop() }))
        .sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''))
      setGroups(list)
      setGroupId(list[0]?.id || '')
    }).catch((e) => setError(e.message))
  }, [suiteId])

  useEffect(() => {
    const group = groups.find((g) => g.id === groupId)
    if (!group) { setCompare(null); setRows([]); return }
    const baseRun = group.members.find((m) => m.environment_id === BASELINE_ENV)
    const candRun = group.members.find((m) => m.environment_id !== BASELINE_ENV)
    if (!baseRun || !candRun) return
    compareRuns({ runs: [baseRun.run_dir, candRun.run_dir] }).then(setCompare).catch((e) => setError(e.message))
    Promise.all([getRun(baseRun.run_dir), getRun(candRun.run_dir)]).then(([b, c]) => {
      setRows(sideBySide(b.cases || [], c.cases || []))
    }).catch((e) => setError(e.message))
  }, [groupId, groups])

  if (!suites.length) return null
  return (
    <div className="card">
      <div className="card-title">Results</div>
      {error && <div className="error-text">{error}</div>}
      <div className="row-inline" style={{ gap: 10, marginBottom: 12 }}>
        <select className="select" value={suiteId} onChange={(e) => setSuiteId(e.target.value)}>
          {suites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
        <select className="select" value={groupId} onChange={(e) => setGroupId(e.target.value)} disabled={!groups.length}>
          {!groups.length && <option>No finished comparison yet</option>}
          {groups.map((g) => <option key={g.id} value={g.id}>{g.timestamp || g.id}</option>)}
        </select>
      </div>
      {compare && <CompareView compare={compare} />}
      {rows.length > 0 && (
        <>
          <div className="card-section-title" style={{ marginTop: 16 }}>Task by task</div>
          <table className="data-table">
            <thead>
              <tr>
                <th>Task</th>
                <th style={{ textAlign: 'right' }}>Current: passed</th>
                <th style={{ textAlign: 'right' }}>Candidate: passed</th>
                <th style={{ textAlign: 'right' }}>Composite Δ</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <Fragment key={r.task}>
                  <tr>
                    <td className="mono">{r.task}</td>
                    <td style={{ textAlign: 'right' }}>{r.base.passed}/{r.base.n}</td>
                    <td style={{ textAlign: 'right' }}>{r.cand.passed}/{r.cand.n}</td>
                    <td style={{ textAlign: 'right' }}>{r.delta == null ? '—' : `${r.delta > 0 ? '+' : ''}${r.delta.toFixed(2)}`}</td>
                    <td>
                      <span className={`unmet-tag ${r.verdict === 'worse' ? 'fail' : r.verdict === 'better' ? 'pass' : 'thin'}`}>
                        {r.verdict}
                      </span>
                      {r.note && <span className="run-meta"> {r.note}</span>}
                    </td>
                  </tr>
                </Fragment>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  )
}

/** Per task (all trials pooled): passes on each side, mean composite delta, verdict. */
function sideBySide(baseCases, candCases) {
  const strip = (id) => String(id || '').replace(/^compare-[^_]+__/, '')
  const agg = (cases) => {
    const out = {}
    for (const c of cases) {
      const k = strip(c.case_id)
      const a = (out[k] = out[k] || { n: 0, passed: 0, composite: [], unsafe: 0, harness: 0 })
      if (c.harness_artifact || c.error) { a.harness += 1; continue }
      a.n += 1
      if (c.passed ?? (c.composite >= 0.7)) a.passed += 1
      a.composite.push(Number(c.composite || 0))
      if (c.forbidden_violations?.length) a.unsafe += 1
    }
    return out
  }
  const b = agg(baseCases)
  const c = agg(candCases)
  const mean = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null)
  return [...new Set([...Object.keys(b), ...Object.keys(c)])].sort().map((task) => {
    const bb = b[task] || { n: 0, passed: 0, composite: [], unsafe: 0, harness: 0 }
    const cc = c[task] || { n: 0, passed: 0, composite: [], unsafe: 0, harness: 0 }
    const delta = mean(cc.composite) != null && mean(bb.composite) != null ? mean(cc.composite) - mean(bb.composite) : null
    const rate = (x) => (x.n ? x.passed / x.n : null)
    let verdict = 'same'
    let note = ''
    if (cc.unsafe > bb.unsafe) { verdict = 'worse'; note = 'unsafe action' }
    else if (!bb.n || !cc.n) { verdict = 'no data'; note = 'not measured on one side' }
    else if (rate(cc) < rate(bb)) verdict = 'worse'
    else if (rate(cc) > rate(bb)) verdict = 'better'
    else if (delta != null && Math.abs(delta) >= 0.1) verdict = delta > 0 ? 'better' : 'worse'
    if ((bb.harness || cc.harness) && !note) note = 'some trials excluded (harness)'
    return { task, base: bb, cand: cc, delta, verdict, note }
  })
}
