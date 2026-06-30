import { useState, useEffect } from 'react'
import { getProviders, getProviderModels, createRun } from '../api.js'

const CASE_FILTER_OPTS = [
  { id: 'all', label: 'All cases' },
  { id: 'consult', label: 'Consult only' },
  { id: 'tick', label: 'Tick only' },
]

export default function RunConfig({ onRunStarted, isRunning }) {
  const [providers, setProviders] = useState([])
  // cfg: { [providerId]: { enabled, apiKey, baseUrl, loadedModels, selectedModel, loading, error } }
  const [cfg, setCfg] = useState({})
  const [caseFilter, setCaseFilter] = useState('all')
  const [category, setCategory] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')

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
    if (!state.baseUrl) return
    update(p.id, { loading: true, error: '' })
    try {
      const data = await getProviderModels(state.baseUrl, state.apiKey)
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

  const handleStart = async () => {
    const models = enabledModels()
    if (!models.length) return
    setSubmitting(true)
    setSubmitError('')
    try {
      const body = {
        models,
        consult_only: caseFilter === 'consult',
        tick_only: caseFilter === 'tick',
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

  const groups = [
    { label: 'CLI Agents', kinds: ['agent'] },
    { label: 'Cloud APIs', kinds: ['cloud'] },
    { label: 'Local Models', kinds: ['local'] },
  ]

  return (
    <div>
      <div className="section-header" style={{ marginBottom: 20 }}>
        <span className="section-title">Configure Benchmark</span>
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
          <label>Case filter</label>
          <div className="radio-group">
            {CASE_FILTER_OPTS.map(o => (
              <button
                key={o.id}
                className={`radio-btn ${caseFilter === o.id ? 'active' : ''}`}
                onClick={() => setCaseFilter(o.id)}
              >
                {o.label}
              </button>
            ))}
          </div>
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
          />
        </div>
      </div>

      <div className="run-summary-bar">
        <div className="run-summary-info">
          {modelCount === 0
            ? 'Select at least one model above'
            : <><strong>{modelCount}</strong> model{modelCount !== 1 ? 's' : ''} selected</>}
          {submitError && (
            <span className="error-text" style={{ marginLeft: 12 }}>{submitError}</span>
          )}
        </div>
        <button
          className="btn primary"
          onClick={handleStart}
          disabled={modelCount === 0 || submitting || isRunning}
        >
          {isRunning ? '⏳ Running…' : submitting ? 'Starting…' : '▶ Start Benchmark'}
        </button>
      </div>
    </div>
  )
}
