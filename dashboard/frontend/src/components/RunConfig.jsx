import { useState, useEffect } from 'react'
import { getProviders, getProviderModels, createRun, getDatasets } from '../api.js'
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

  const toggle = (id) => update(id, { enabled: !cfg[id]?.enabled })

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
        out.push({ model_key: p.id, api_key: null, base_url: null })
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

  const toggleLayer = (id) =>
    setLayers(prev => (prev.includes(id) ? prev.filter(l => l !== id) : [...prev, id]))

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

  // Case count for the current filters, so "start" isn't a guess about scope.
  // Counted off the layer × domain cross-tab: summing one axis and ignoring the
  // other reported a domain's whole case count for "this domain, tick layer only".
  // The category filter is free text applied server-side, so it is not counted here.
  const selectedCases = (() => {
    if (!datasets) return null
    const cross = datasets.layer_domains || {}
    const wantedLayers = layers.length ? layers : Object.keys(cross)
    let n = 0
    for (const layer of wantedLayers) {
      const byDomain = cross[layer] || {}
      for (const [d, count] of Object.entries(byDomain)) {
        if (domain && d !== domain) continue
        n += count
      }
    }
    return n
  })()

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
                    <div className="provider-header" onClick={() => toggle(p.id)}>
                      <label className="toggle" onClick={e => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={state.enabled}
                          onChange={() => toggle(p.id)}
                        />
                        <span className="toggle-track" />
                      </label>
                      <span className="provider-label">{p.label}</span>
                      <span className={`provider-kind ${p.kind}`}>{p.kind}</span>
                    </div>

                    {state.enabled && (
                      <div className="provider-body">
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
          <label>Routing domain (optional)</label>
          <select
            className="select"
            value={domain}
            onChange={e => setDomain(e.target.value)}
            style={{ maxWidth: 320 }}
          >
            <option value="">All domains</option>
            {(datasets?.routing_domains || []).map(d => (
              <option key={d} value={d}>
                {d} ({datasets.domains?.[d] ?? 0})
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label>Category filter (optional)</label>
          <input
            type="text"
            className="input"
            placeholder="e.g. risk, concepts, troubleshooting"
            value={category}
            onChange={e => setCategory(e.target.value)}
            style={{ maxWidth: 320 }}
            list="bench-categories"
          />
          <datalist id="bench-categories">
            {(datasets?.categories || []).map(c => <option key={c} value={c} />)}
          </datalist>
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
                {category.trim() && (
                  <span className="run-meta"> · before the category filter narrows it</span>
                )}
              </>
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
          disabled={modelCount === 0 || submitting || isRunning || stagingBlocked}
        >
          {isRunning ? '⏳ Running…' : submitting ? 'Starting…' : '▶ Start Benchmark'}
        </button>
      </div>
    </div>
  )
}
