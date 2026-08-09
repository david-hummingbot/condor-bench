/**
 * Suites tab — MCPJam-style Evaluate: environments, cases, Run all, Compare.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  compareRuns,
  createEnvironment,
  createSuite,
  createSuiteCase,
  deleteEnvironment,
  deleteSuiteCase,
  getModelRegistry,
  getProviders,
  getSuite,
  importSuiteCases,
  listEnvironments,
  listSuiteRuns,
  listSuites,
  patchSuite,
  runSuite,
  validateEnvironment,
} from '../api.js'

function buildModelOptions(registry, providers) {
  const seen = new Set()
  const out = []
  for (const m of registry || []) {
    if (!m.key || seen.has(m.key)) continue
    seen.add(m.key)
    const size = m.params_b != null ? `${m.params_b}B` : m.provider || 'cloud'
    out.push({ key: m.key, label: `${m.key} (${size})` })
  }
  for (const p of providers || []) {
    if (p.bare_key) {
      if (!seen.has(p.id)) {
        seen.add(p.id)
        out.push({ key: p.id, label: `${p.label} (agent)` })
      }
      continue
    }
    for (const model of p.models || []) {
      const key = `${p.id}:${model}`
      if (seen.has(key)) continue
      seen.add(key)
      out.push({ key, label: key })
    }
  }
  return out
}

export default function Suites({ onRunStarted }) {
  const [suites, setSuites] = useState([])
  const [envs, setEnvs] = useState([])
  const [modelOptions, setModelOptions] = useState([])
  const [selectedId, setSelectedId] = useState(null)
  const [detail, setDetail] = useState(null)
  const [tab, setTab] = useState('cases') // cases | runs | compare | envs
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [runs, setRuns] = useState([])
  const [compare, setCompare] = useState(null)
  const [newCaseQ, setNewCaseQ] = useState('')
  const [importIds, setImportIds] = useState('c001,c002')
  const [envForm, setEnvForm] = useState({
    name: '',
    condor_path: '',
    expected_branch: 'main',
    mode: 'live',
    server_name: 'bench_staging',
    require_clean: true,
  })
  const [suiteForm, setSuiteForm] = useState({
    name: '',
    environment_ids: [],
    model_key: '',
  })
  const [validation, setValidation] = useState(null)

  const refresh = useCallback(async () => {
    const [s, e] = await Promise.all([listSuites(), listEnvironments()])
    setSuites(s.suites || [])
    setEnvs(e.environments || [])
  }, [])

  useEffect(() => {
    refresh().catch((err) => setError(String(err.message || err)))
    Promise.all([getModelRegistry(), getProviders()])
      .then(([reg, prov]) => {
        const opts = buildModelOptions(reg.models, prov.providers)
        setModelOptions(opts)
        setSuiteForm((f) => ({
          ...f,
          model_key: f.model_key || opts[0]?.key || '',
        }))
      })
      .catch(() => {})
  }, [refresh])

  const loadDetail = useCallback(async (id) => {
    if (!id) {
      setDetail(null)
      return
    }
    const data = await getSuite(id)
    setDetail(data)
    const r = await listSuiteRuns(id)
    setRuns(r.runs || [])
  }, [])

  useEffect(() => {
    if (selectedId) {
      loadDetail(selectedId).catch((err) => setError(String(err.message || err)))
    }
  }, [selectedId, loadDetail])

  async function handleCreateEnv(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await createEnvironment(envForm)
      setEnvForm({
        name: '',
        condor_path: '',
        expected_branch: 'main',
        mode: 'live',
        server_name: 'bench_staging',
        require_clean: true,
      })
      await refresh()
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleCreateSuite(e) {
    e.preventDefault()
    if (!suiteForm.model_key) {
      setError('Choose a model — this is the fixed model every Environment in the suite runs against.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const created = await createSuite({
        name: suiteForm.name,
        environment_ids: suiteForm.environment_ids,
        models: [{ model_key: suiteForm.model_key }],
        include_in_matrix: false,
      })
      setSuiteForm({
        name: '',
        environment_ids: [],
        model_key: modelOptions[0]?.key || '',
      })
      await refresh()
      setSelectedId(created.id)
      setTab('cases')
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleRunAll() {
    if (!detail) return
    setBusy(true)
    setError(null)
    try {
      const { run_id } = await runSuite(detail.id, {})
      onRunStarted?.(run_id)
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleImport() {
    if (!detail) return
    setBusy(true)
    setError(null)
    try {
      const ids = importIds.split(/[\s,]+/).filter(Boolean)
      await importSuiteCases(detail.id, {
        case_ids: ids,
        version: detail.version,
      })
      await loadDetail(detail.id)
      await refresh()
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleNewCase(e) {
    e.preventDefault()
    if (!detail || !newCaseQ.trim()) return
    setBusy(true)
    setError(null)
    try {
      await createSuiteCase(detail.id, {
        version: detail.version,
        type: 'consult',
        question: newCaseQ.trim(),
        expected_tools: [],
        risk_level: 'read_only',
      })
      setNewCaseQ('')
      await loadDetail(detail.id)
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleDeleteCase(caseId) {
    if (!detail) return
    setBusy(true)
    try {
      await deleteSuiteCase(detail.id, caseId, detail.version)
      await loadDetail(detail.id)
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleCompare() {
    const groups = {}
    for (const r of runs) {
      const gid = r.run_group_id
      if (!gid) continue
      groups[gid] = groups[gid] || []
      groups[gid].push(r)
    }
    const gid = Object.keys(groups).find((k) => groups[k].length >= 2)
    if (!gid) {
      setError('Need a completed Run-all with at least two environment members to compare.')
      return
    }
    setBusy(true)
    try {
      const data = await compareRuns({ runGroup: gid })
      setCompare(data)
      setTab('compare')
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function handleValidate(envId) {
    setBusy(true)
    try {
      setValidation(await validateEnvironment(envId))
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  async function toggleSuiteEnv(envId) {
    if (!detail) return
    const ids = new Set(detail.environment_ids || [])
    if (ids.has(envId)) ids.delete(envId)
    else ids.add(envId)
    setBusy(true)
    try {
      await patchSuite(detail.id, {
        version: detail.version,
        environment_ids: [...ids],
      })
      await loadDetail(detail.id)
      await refresh()
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="suites-page">
      {error && (
        <div className="card" style={{ borderColor: 'var(--red)', marginBottom: 12 }}>
          <div style={{ color: 'var(--red)' }}>{error}</div>
          <button className="btn btn-ghost" style={{ marginTop: 8 }} onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}

      <div className="suites-layout">
        <aside className="suites-sidebar card">
          <div className="card-title">Suites</div>
          <ul className="suite-list">
            {suites.map((s) => (
              <li key={s.id}>
                <button
                  className={`suite-list-item ${selectedId === s.id ? 'active' : ''}`}
                  onClick={() => setSelectedId(s.id)}
                >
                  <strong>{s.name}</strong>
                  <span className="muted">{s.id}</span>
                </button>
              </li>
            ))}
          </ul>

          <form className="suite-create" onSubmit={handleCreateSuite}>
            <div className="card-section-title">New suite</div>
            <input
              placeholder="Name"
              value={suiteForm.name}
              onChange={(e) => setSuiteForm({ ...suiteForm, name: e.target.value })}
              required
            />
            <label className="settings-field">
              <span className="settings-label">Model</span>
              <select
                value={suiteForm.model_key}
                onChange={(e) => setSuiteForm({ ...suiteForm, model_key: e.target.value })}
                required
              >
                {!modelOptions.length && <option value="">Loading models…</option>}
                {modelOptions.map((m) => (
                  <option key={m.key} value={m.key}>
                    {m.label}
                  </option>
                ))}
              </select>
              <span className="muted settings-hint">
                Fixed model for every Environment in this suite — so Condor A/B
                deltas are from the checkout, not from switching models.
              </span>
            </label>
            <div className="env-checkboxes">
              {envs.map((env) => (
                <label key={env.id}>
                  <input
                    type="checkbox"
                    checked={suiteForm.environment_ids.includes(env.id)}
                    onChange={() => {
                      const set = new Set(suiteForm.environment_ids)
                      if (set.has(env.id)) set.delete(env.id)
                      else set.add(env.id)
                      setSuiteForm({ ...suiteForm, environment_ids: [...set] })
                    }}
                  />
                  {env.name}
                </label>
              ))}
            </div>
            <button className="btn btn-primary" type="submit" disabled={busy}>
              Create suite
            </button>
          </form>
        </aside>

        <main className="suites-main">
          {!detail ? (
            <div className="card">
              <p className="muted">Select or create a suite. Attach Environments that point at different Condor checkouts, then Run all to compare.</p>
              <p className="muted" style={{ marginTop: 8 }}>
                Trusted-local only: Environment <code>condor_path</code> is executed by the backend. Bind the dashboard to localhost; do not expose it.
              </p>
            </div>
          ) : (
            <>
              <div className="card suite-header">
                <div>
                  <h2 style={{ fontSize: 18, marginBottom: 4 }}>{detail.name}</h2>
                  <div className="muted">
                    {detail.id} · v{detail.version} · {(detail.cases || []).length} cases ·{' '}
                    {(detail.environment_ids || []).length} env(s)
                  </div>
                </div>
                <div className="suite-actions">
                  <button className="btn btn-primary" onClick={handleRunAll} disabled={busy}>
                    Run all
                  </button>
                  <button className="btn" onClick={handleCompare} disabled={busy}>
                    Compare runs
                  </button>
                </div>
              </div>

              <div className="suite-subtabs">
                {['cases', 'runs', 'compare', 'envs'].map((t) => (
                  <button
                    key={t}
                    className={`tab ${tab === t ? 'active' : ''}`}
                    onClick={() => setTab(t)}
                  >
                    {t === 'envs' ? 'Environments' : t[0].toUpperCase() + t.slice(1)}
                  </button>
                ))}
              </div>

              {tab === 'cases' && (
                <div className="card">
                  <div className="card-section-title">Import from library</div>
                  <div className="row-inline">
                    <input
                      value={importIds}
                      onChange={(e) => setImportIds(e.target.value)}
                      placeholder="c001,c002,…"
                      style={{ flex: 1 }}
                    />
                    <button className="btn" onClick={handleImport} disabled={busy}>
                      Import
                    </button>
                  </div>

                  <form className="row-inline" onSubmit={handleNewCase} style={{ marginTop: 12 }}>
                    <input
                      value={newCaseQ}
                      onChange={(e) => setNewCaseQ(e.target.value)}
                      placeholder="New case question"
                      style={{ flex: 1 }}
                    />
                    <button className="btn btn-primary" type="submit" disabled={busy}>
                      + New case
                    </button>
                  </form>

                  <table className="data-table" style={{ marginTop: 16 }}>
                    <thead>
                      <tr>
                        <th>ID</th>
                        <th>Type</th>
                        <th>Question</th>
                        <th>Risk</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {(detail.cases || []).map((c) => (
                        <tr key={c.id}>
                          <td className="mono">{c.id}</td>
                          <td>{c.type}</td>
                          <td>{c.question || c.scenario_name || '—'}</td>
                          <td>{c.risk_level}</td>
                          <td>
                            <button className="btn btn-ghost" onClick={() => handleDeleteCase(c.id)}>
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {tab === 'runs' && (
                <div className="card">
                  <div className="card-title">All runs</div>
                  {!runs.length && <p className="muted">No suite runs yet.</p>}
                  <ul className="run-list">
                    {runs.map((r) => (
                      <li key={r.run_dir}>
                        <strong>{r.run_dir}</strong>
                        <span className="muted">
                          {' '}
                          · {r.environment_id || '—'} · {r.model} · pass {r.pass_rate ?? '—'} ·{' '}
                          {r.condor?.commit || '—'}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {tab === 'compare' && (
                <div className="card">
                  <div className="card-title">Compare</div>
                  {!compare && <p className="muted">Run Compare runs after a multi-environment Run all.</p>}
                  {compare && (
                    <>
                      <p>
                        comparable:{' '}
                        <strong style={{ color: compare.comparable ? 'var(--green)' : 'var(--orange)' }}>
                          {String(compare.comparable)}
                        </strong>
                        {compare.differences?.length > 0 && (
                          <span className="muted"> — {compare.differences.join(', ')}</span>
                        )}
                      </p>
                      {compare.differences?.includes('prompt_only_mock') && (
                        <p className="muted">Mock multi-env compare is prompt-only, not a Condor wiring A/B.</p>
                      )}
                      <pre className="code-block">{JSON.stringify(compare.deltas, null, 2)}</pre>
                      <div className="card-section-title">Members</div>
                      <pre className="code-block">{JSON.stringify(compare.members, null, 2)}</pre>
                    </>
                  )}
                </div>
              )}

              {tab === 'envs' && (
                <div className="card">
                  <div className="card-title">Environments on this suite</div>
                  <div className="env-checkboxes" style={{ marginBottom: 16 }}>
                    {envs.map((env) => (
                      <label key={env.id}>
                        <input
                          type="checkbox"
                          checked={(detail.environment_ids || []).includes(env.id)}
                          onChange={() => toggleSuiteEnv(env.id)}
                        />
                        {env.name} <span className="muted">({env.mode} · {env.condor_path})</span>
                      </label>
                    ))}
                  </div>

                  <div className="card-section-title">All environments</div>
                  <ul className="run-list">
                    {envs.map((env) => (
                      <li key={env.id}>
                        <strong>{env.name}</strong>
                        <span className="muted">
                          {' '}
                          · {env.id} · v{env.version} · {env.mode} · {env.condor_path}
                        </span>
                        <button className="btn btn-ghost" onClick={() => handleValidate(env.id)}>
                          Validate
                        </button>
                        <button
                          className="btn btn-ghost"
                          onClick={async () => {
                            try {
                              await deleteEnvironment(env.id)
                              await refresh()
                            } catch (err) {
                              setError(String(err.message || err))
                            }
                          }}
                        >
                          Delete
                        </button>
                      </li>
                    ))}
                  </ul>

                  {validation && (
                    <pre className="code-block" style={{ marginTop: 12 }}>
                      {JSON.stringify(validation, null, 2)}
                    </pre>
                  )}

                  <form onSubmit={handleCreateEnv} className="env-form">
                    <div className="card-section-title">New environment</div>
                    <input
                      placeholder="name"
                      value={envForm.name}
                      onChange={(e) => setEnvForm({ ...envForm, name: e.target.value })}
                      required
                    />
                    <input
                      placeholder="condor_path"
                      value={envForm.condor_path}
                      onChange={(e) => setEnvForm({ ...envForm, condor_path: e.target.value })}
                      required
                    />
                    <input
                      placeholder="expected_branch"
                      value={envForm.expected_branch}
                      onChange={(e) => setEnvForm({ ...envForm, expected_branch: e.target.value })}
                    />
                    <select
                      value={envForm.mode}
                      onChange={(e) => setEnvForm({ ...envForm, mode: e.target.value })}
                    >
                      <option value="live">live</option>
                      <option value="mock">mock</option>
                    </select>
                    <input
                      placeholder="server_name"
                      value={envForm.server_name}
                      onChange={(e) => setEnvForm({ ...envForm, server_name: e.target.value })}
                    />
                    <label>
                      <input
                        type="checkbox"
                        checked={envForm.require_clean}
                        onChange={(e) =>
                          setEnvForm({ ...envForm, require_clean: e.target.checked })
                        }
                      />{' '}
                      require_clean
                    </label>
                    <button className="btn btn-primary" type="submit" disabled={busy}>
                      Create environment
                    </button>
                  </form>
                </div>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  )
}
