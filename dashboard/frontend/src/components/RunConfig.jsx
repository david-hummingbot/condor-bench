import { useState, useEffect } from 'react'
import { getProviders, getProviderModels, getAcpModels, createRun, getDatasets } from '../api.js'
import StagingStatus from './StagingStatus.jsx'
import PageHeader from './PageHeader.jsx'

const LAYER_OPTS = [
  { id: 'consult', label: 'Consult', hint: 'Layer 1 — end-to-end advisory + strategy creation' },
  { id: 'tick', label: 'Tick', hint: 'Layer 1 — simulated agent ticks, agent-scoped' },
  { id: 'tool', label: 'Tools', hint: 'Layer 2 — one case per MCP tool' },
  { id: 'agent', label: 'Agents', hint: 'Layer 3 — routed to a specific Condor assistant' },
]

export default function RunConfig({ onRunStarted, isRunning, config }) {
  const [providers, setProviders] = useState([])
  // cfg: { [providerId]: { enabled, apiKey, baseUrl, loadedModels, selectedModel, loading, error } }
  const [cfg, setCfg] = useState({})
  const [layers, setLayers] = useState([])   // empty = all layers
  const [domain, setDomain] = useState('')
  const [category, setCategory] = useState('')
  const [riskLevels, setRiskLevels] = useState([])  // empty = all risk levels
  const [datasets, setDatasets] = useState(null)
  const [staging, setStaging] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

  useEffect(() => {
    getDatasets().then(setDatasets).catch(() => {})
  }, [])

  useEffect(() => {
    getProviders()
      .then(d => {
        setProviders(d.providers || [])
        const init = {}
        for (const p of d.providers || []) {
          init[p.id] = {
            enabled: false,
            apiKey: '',
            baseUrl: p.default_url || '',
            loadedModels: [],
            selectedModel: p.models?.[0] || '',
            loading: false,
            error: '',
          }
        }
        setCfg(init)
      })
      .catch(() => {})
  }, [])

  const update = (id, patch) =>
    setCfg(prev => ({ ...prev, [id]: { ...prev[id], ...patch } }))

  const toggle = (p) => {
    const id = typeof p === 'string' ? p : p.id
    const enabling = !cfg[id]?.enabled
    update(id, { enabled: enabling })
    // Fetch an ACP bridge's model list as soon as it is switched on, so the working
    // default is already selected. Waiting for a button press meant the common path
    // ran with the CLI's configured model, which is the one that can 400 on every
    // prompt and produce a run of empty rows.
    if (enabling && typeof p === 'object' && p.fetch_acp_models && !cfg[id]?.acpModels) {
      loadAcpModels(p)
    }
  }

  const loadModels = async (p) => {
    const state = cfg[p.id]
    // Cloud providers use a fixed api_base; local providers use the user-supplied baseUrl
    const url = p.api_base || state.baseUrl
    if (!url) return
    update(p.id, { loading: true, error: '' })
    try {
      const data = await getProviderModels(url, state.apiKey)
      const models = data.models || []
      update(p.id, {
        loadedModels: models,
        selectedModel: models[0] || state.selectedModel,
        loading: false,
      })
    } catch (e) {
      update(p.id, { loading: false, error: e.message })
    }
  }

  const enabledModels = () => {
    const out = []
    for (const p of providers) {
      const state = cfg[p.id]
      if (!state?.enabled) continue
      if (p.bare_key) {
        // An ACP agent runs whatever model its CLI is configured with unless the key
        // names one. Naming one is not optional in practice: a locally configured
        // model the bridge cannot use fails every prompt with an API 400.
        const model = state.selectedModel
        out.push({
          model_key: model ? `${p.id}:${model}` : p.id,
          api_key: null,
          base_url: null,
        })
      } else {
        const model = state.selectedModel
        if (!model) continue
        const key = p.id === 'lmstudio'
          ? `lmstudio:${model}`
          : `${p.id}:${model}`
        out.push({
          model_key: key,
          api_key: state.apiKey || null,
          base_url: state.baseUrl || null,
        })
      }
    }
    return out
  }

  /**
   * Ask an ACP bridge which models it accepts. Also the fastest way to find out the
   * bridge works at all — if this errors, no run against that agent can succeed, and
   * the message carries the bridge's stderr rather than leaving empty rows behind.
   */
  const loadAcpModels = async (p) => {
    update(p.id, { loading: true, error: '' })
    try {
      const data = await getAcpModels(p.id)
      const models = data.models || []
      update(p.id, {
        acpModels: models,
        acpCurrent: data.current || '',
        // Default to the bridge's own recommendation, not to the CLI's configured
        // model: that one is whatever happens to be in ~/.claude/settings.json and
        // is exactly what can fail every prompt in the run.
        selectedModel: models.some(m => m.id === 'default') ? 'default' : (models[0]?.id || ''),
        loading: false,
      })
    } catch (e) {
      update(p.id, { loading: false, error: e.message, acpModels: [] })
    }
  }

  const toggleLayer = (id) => {
    const next = layers.includes(id) ? layers.filter(l => l !== id) : [...layers, id]
    setLayers(next)
    // Drop a domain/category the new layer set can no longer produce. Leaving a
    // stale one selected is exactly how "Tools + general_consult" — a combination
    // with no case behind it — became submittable.
    const rows = (datasets?.combos || []).filter(
      c => !next.length || next.includes(c.layer)
    )
    if (domain && !rows.some(c => c.domain === domain)) setDomain('')
    const cat = category.trim()
    if (cat && !rows.some(c => c.category === cat)) setCategory('')
    // Same for risk: `tick` has no read_only-only shape to fall back on, so a
    // stale selection here empties the run just as silently as a stale domain.
    const keptRisk = riskLevels.filter(r => rows.some(c => c.risk_level === r))
    if (keptRisk.length !== riskLevels.length) setRiskLevels(keptRisk)
  }

  const toggleRisk = (id) => {
    setRiskLevels(riskLevels.includes(id)
      ? riskLevels.filter(r => r !== id)
      : [...riskLevels, id])
  }

  const handleStart = async () => {
    const models = enabledModels()
    if (!models.length) return
    setSubmitting(true)
    setSubmitError('')
    try {
      const body = {
        models,
        layers: layers.length ? layers : null,
        domain: domain || null,
        category: category.trim() || null,
        risk_levels: riskLevels.length ? riskLevels : null,
      }
      const data = await createRun(body)
      onRunStarted(data.run_id)
    } catch (e) {
      setSubmitError(e.message)
    } finally {
      setSubmitting(false)
    }
  }

  const modelCount = enabledModels().length
  const stagingBlocked =
    (staging?.checks || []).some(c => c.blocking && !c.ok)

  /**
   * The three filters AND together, and the axes barely overlap — a Tool case's
   * domain is always a `tool:` bucket and its category is always "tool". Offering
   * the routing domains and the whole category list next to a Tools selection
   * proposed combinations that match nothing, and the only feedback was the run
   * being refused after Start with "No cases matched the selected filters."
   *
   * So every list below is derived from the real (layer, domain, category)
   * combinations: each filter offers only values that survive the others, and the
   * count is exact rather than an estimate that ignored the category.
   */
  const combos = datasets?.combos || []
  const matching = (opts = {}) => {
    const wantLayers = opts.layers ?? layers
    const wantDomain = opts.domain ?? domain
    const wantCategory = opts.category ?? category.trim()
    const wantRisk = opts.riskLevels ?? riskLevels
    return combos.filter(c =>
      (!wantLayers.length || wantLayers.includes(c.layer)) &&
      (!wantDomain || c.domain === wantDomain) &&
      (!wantCategory || c.category === wantCategory) &&
      (!wantRisk.length || wantRisk.includes(c.risk_level))
    )
  }
  const countOf = (rows) => rows.reduce((n, c) => n + c.count, 0)

  // Domains available under the chosen layers, ignoring the category so narrowing
  // the category can never empty the domain list you picked from.
  const domainOptions = (() => {
    const seen = new Map()
    for (const c of matching({ domain: '', category: '', riskLevels: [] })) {
      seen.set(c.domain, (seen.get(c.domain) || 0) + c.count)
    }
    return [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  })()

  const categoryOptions = (() => {
    const seen = new Map()
    for (const c of matching({ category: '', riskLevels: [] })) {
      if (c.category) seen.set(c.category, (seen.get(c.category) || 0) + c.count)
    }
    return [...seen.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  })()

  // Risk levels available under the other three filters, ignoring the current risk
  // selection so a chosen level never removes itself from the list.
  const riskOptions = (() => {
    const seen = new Map()
    for (const c of matching({ riskLevels: [] })) {
      if (c.risk_level) seen.set(c.risk_level, (seen.get(c.risk_level) || 0) + c.count)
    }
    return ['read_only', 'mutating', 'destructive']
      .filter(r => seen.has(r))
      .map(r => [r, seen.get(r)])
  })()

  const selectedCases = datasets ? countOf(matching()) : null

  const groups = [
    { label: 'CLI Agents', kinds: ['agent'] },
    { label: 'Cloud APIs', kinds: ['cloud'] },
    { label: 'Local Models', kinds: ['local'] },
  ]

  return (
    <div>
      <PageHeader
        title="Benchmark"
        description="A throwaway run against the shared case library — pick models, narrow the dataset, and go. For a saved, repeatable definition that A/Bs two Condor checkouts, use Suites instead."
        meta={datasets ? `${datasets.total} cases · ${datasets.agent_scoped} agent-scoped` : null}
      />

      {/* Shown above the model picker on purpose: which API the run will hit
          matters more than which model runs against it. */}
      <div style={{ marginBottom: 16 }}>
        <StagingStatus compact onReport={setStaging} />
      </div>

      {groups.map(g => {
        const ps = providers.filter(p => g.kinds.includes(p.kind))
        if (!ps.length) return null
        return (
          <div key={g.label} className="card" style={{ marginBottom: 16 }}>
            <div className="card-title">{g.label}</div>
            <div className="provider-list">
              {ps.map(p => {
                const state = cfg[p.id]
                if (!state) return null
                const allModels = [...(state.loadedModels.length ? state.loadedModels : p.models)]
                return (
                  <div key={p.id} className={`provider-row ${state.enabled ? 'enabled' : ''}`}>
                    <div className="provider-header" onClick={() => toggle(p)}>
                      <label className="toggle" onClick={e => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={state.enabled}
                          onChange={() => toggle(p)}
                        />
                        <span className="toggle-track" />
                      </label>
                      <span className="provider-label">{p.label}</span>
                      <span className={`provider-kind ${p.kind}`}>{p.kind}</span>
                    </div>

                    {state.enabled && (
                      <div className="provider-body">
                        {p.fetch_acp_models && (
                          <div className="field">
                            <label>Model</label>
                            <div className="inline-row">
                              {state.acpModels?.length > 0 ? (
                                <select
                                  className="select"
                                  value={state.selectedModel || ''}
                                  onChange={e => update(p.id, { selectedModel: e.target.value })}
                                >
                                  <option value="">CLI default (whatever it is configured with)</option>
                                  {state.acpModels.map(m => (
                                    <option key={m.id} value={m.id}>
                                      {m.name}{m.id === state.acpCurrent ? ' — CLI current' : ''}
                                    </option>
                                  ))}
                                </select>
                              ) : (
                                <span className="run-meta">
                                  Not selected — the run will use whatever model this CLI is
                                  configured with.
                                </span>
                              )}
                              <button
                                className="btn sm"
                                onClick={() => loadAcpModels(p)}
                                disabled={state.loading}
                              >
                                {state.loading ? '…' : state.acpModels?.length ? '↻' : 'Load models'}
                              </button>
                            </div>
                            {state.error && <span className="error-text">{state.error}</span>}
                            {state.acpModels?.length > 0 && state.selectedModel && (
                              <span className="run-meta">
                                {state.acpModels.find(m => m.id === state.selectedModel)?.description}
                              </span>
                            )}
                          </div>
                        )}

                        {p.needs_api_key && (
                          <div className="field">
                            <label>API Key</label>
                            <input
                              type="password"
                              className="input"
                              placeholder={p.key_hint || 'API key...'}
                              value={state.apiKey}
                              onChange={e => update(p.id, { apiKey: e.target.value })}
                            />
                          </div>
                        )}

                        {p.supports_url && (
                          <div className="field">
                            <label>Base URL</label>
                            <div className="inline-row">
                              <input
                                type="text"
                                className="input"
                                placeholder={p.default_url || 'http://host:port'}
                                value={state.baseUrl}
                                onChange={e => update(p.id, { baseUrl: e.target.value })}
                              />
                              {p.fetch_models && (
                                <button
                                  className="btn sm"
                                  onClick={() => loadModels(p)}
                                  disabled={state.loading || !state.baseUrl}
                                >
                                  {state.loading ? '…' : 'Load models'}
                                </button>
                              )}
                            </div>
                            {state.error && (
                              <span className="error-text">{state.error}</span>
                            )}
                          </div>
                        )}

                        {/* Cloud providers with a fixed API base (e.g. OpenRouter, Groq) */}
                        {!p.supports_url && p.fetch_models && (
                          <div className="field">
                            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                              <button
                                className="btn sm"
                                onClick={() => loadModels(p)}
                                disabled={state.loading}
                              >
                                {state.loading ? '…' : 'Load all models'}
                              </button>
                              {state.loadedModels.length > 0 && (
                                <span style={{ fontSize: 12, color: 'var(--muted)' }}>
                                  {state.loadedModels.length} models loaded
                                </span>
                              )}
                            </div>
                            {state.error && (
                              <span className="error-text">{state.error}</span>
                            )}
                          </div>
                        )}

                        {!p.bare_key && allModels.length > 0 && (
                          <div className="field">
                            <label>Model</label>
                            {allModels.length <= 8 ? (
                              <select
                                className="select"
                                value={state.selectedModel}
                                onChange={e => update(p.id, { selectedModel: e.target.value })}
                              >
                                {allModels.map(m => (
                                  <option key={m} value={m}>{m}</option>
                                ))}
                              </select>
                            ) : (
                              <input
                                type="text"
                                className="input"
                                placeholder="model name"
                                value={state.selectedModel}
                                onChange={e => update(p.id, { selectedModel: e.target.value })}
                                list={`models-${p.id}`}
                              />
                            )}
                            {allModels.length > 8 && (
                              <datalist id={`models-${p.id}`}>
                                {allModels.map(m => <option key={m} value={m} />)}
                              </datalist>
                            )}
                          </div>
                        )}

                        {p.id === 'custom' && (
                          <div className="field">
                            <label>API Key (optional)</label>
                            <input
                              type="password"
                              className="input"
                              placeholder="Leave blank if not required"
                              value={state.apiKey}
                              onChange={e => update(p.id, { apiKey: e.target.value })}
                            />
                          </div>
                        )}

                        {!p.bare_key && !allModels.length && p.supports_url && (
                          <div className="field">
                            <label>Model name</label>
                            <input
                              type="text"
                              className="input"
                              placeholder="e.g. llama3.1:8b"
                              value={state.selectedModel}
                              onChange={e => update(p.id, { selectedModel: e.target.value })}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}

      <div className="card">
        <div className="card-title">Options</div>

        <div className="field" style={{ marginBottom: 14 }}>
          <label>Dataset layers {layers.length === 0 && <span className="run-meta">(all)</span>}</label>
          <div className="radio-group">
            {LAYER_OPTS.map(o => (
              <button
                key={o.id}
                className={`radio-btn ${layers.includes(o.id) ? 'active' : ''}`}
                onClick={() => toggleLayer(o.id)}
                title={o.hint}
              >
                {o.label}
                {datasets?.layers?.[o.id] != null && (
                  <span className="radio-count">{datasets.layers[o.id]}</span>
                )}
              </button>
            ))}
          </div>
        </div>

        <div className="field" style={{ marginBottom: 14 }}>
          <label>
            Domain (optional)
            {layers.length > 0 && (
              <span className="run-meta"> — only domains in the selected layers</span>
            )}
          </label>
          <select
            className="select"
            value={domain}
            onChange={e => setDomain(e.target.value)}
            style={{ maxWidth: 380 }}
          >
            <option value="">All domains</option>
            {domainOptions.map(([d, n]) => (
              <option key={d} value={d}>
                {/* Layer 2 groups are capability buckets, not routing targets —
                    they filter perfectly well, they just never reach the Router. */}
                {d.startsWith('tool:') ? `${d.slice(5)} — capability bucket` : d} ({n})
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Category (optional)</label>
          <select
            className="select"
            value={category}
            onChange={e => setCategory(e.target.value)}
            style={{ maxWidth: 380 }}
          >
            <option value="">All categories</option>
            {categoryOptions.map(([c, n]) => (
              <option key={c} value={c}>{c} ({n})</option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>
            Risk level {riskLevels.length === 0 && <span className="run-meta">(all)</span>}
            <span className="run-meta">
              {' '}— read_only alone is the cheap tool-calling probe
            </span>
          </label>
          <div className="radio-row">
            {riskOptions.map(([r, n]) => (
              <button
                key={r}
                type="button"
                className={`radio-btn ${riskLevels.includes(r) ? 'active' : ''}`}
                onClick={() => toggleRisk(r)}
                title={
                  r === 'read_only' ? 'No state change — nothing to undo afterwards'
                  : r === 'mutating' ? 'Changes condor-side state; teardown runs after each case'
                  : 'Capital-affecting, and must clear DESTRUCTIVE_FLOOR to be routable'
                }
              >
                {r}
                <span className="radio-count">{n}</span>
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="run-summary-bar">
        <div className="run-summary-info">
          {modelCount === 0
            ? 'Select at least one model above'
            : (
              <>
                <strong>{modelCount}</strong> model{modelCount !== 1 ? 's' : ''}
                {selectedCases != null && <> × <strong>{selectedCases}</strong> case{selectedCases !== 1 ? 's' : ''}</>}
              </>
            )}
          {/* Caught here rather than after Start: the backend's only answer to an
              impossible filter set is to refuse the run once it has been submitted. */}
          {selectedCases === 0 && (
            <span className="error-text" style={{ marginLeft: 12 }}>
              no case matches these filters — widen the layers, domain, category or risk level
            </span>
          )}
          {stagingBlocked && (
            <span className="error-text" style={{ marginLeft: 12 }}>
              staging pre-flight is failing — the run will refuse to start
            </span>
          )}
          {submitError && (
            <span className="error-text" style={{ marginLeft: 12 }}>{submitError}</span>
          )}
        </div>
        <button
          className="btn primary"
          onClick={handleStart}
          disabled={
            modelCount === 0 || selectedCases === 0 || submitting || isRunning || stagingBlocked
          }
        >
          {isRunning ? '⏳ Running…' : submitting ? 'Starting…' : '▶ Start Benchmark'}
        </button>
      </div>
    </div>
  )
}
