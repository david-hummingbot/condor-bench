import { useState, useEffect, useRef } from 'react'
import { getProviders, createCustomPrompt, streamCustomPromptUrl } from '../api.js'

const SCORE_COLOR = (v) => v >= 0.8 ? 'var(--green)' : v >= 0.5 ? 'var(--yellow)' : 'var(--red)'

function ScorePill({ label, value }) {
  if (value == null) return (
    <span style={{ fontSize: 11, color: 'var(--muted)', background: 'var(--panel-2)', borderRadius: 6, padding: '2px 8px' }}>
      {label}: N/A
    </span>
  )
  return (
    <span style={{ fontSize: 11, background: 'var(--panel-2)', borderRadius: 6, padding: '2px 8px', color: SCORE_COLOR(value) }}>
      {label}: {(value * 100).toFixed(0)}%
    </span>
  )
}

function ToolBadge({ name }) {
  const short = name.includes('__') ? name.split('__').pop() : name
  return (
    <span style={{
      display: 'inline-block', fontSize: 11, padding: '2px 7px',
      background: 'rgba(79,140,255,0.12)', color: 'var(--accent)',
      borderRadius: 4, margin: '2px 3px 2px 0',
    }}>
      {short}
    </span>
  )
}

function ModelResult({ model, result }) {
  const [expanded, setExpanded] = useState(true)
  const sc = result.scorecard || {}
  const tools = result.tool_calls || []

  return (
    <div style={{
      border: '1px solid var(--border)', borderRadius: 8,
      marginBottom: 12, overflow: 'hidden',
    }}>
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '10px 14px', background: 'var(--panel-2)',
          cursor: 'pointer', userSelect: 'none',
        }}
        onClick={() => setExpanded(e => !e)}
      >
        <span style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>{model}</span>
        {result.error && (
          <span style={{ fontSize: 11, color: 'var(--red)' }}>error</span>
        )}
        {sc.latency_s != null && (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>{sc.latency_s?.toFixed(1)}s</span>
        )}
        <div style={{ display: 'flex', gap: 4 }}>
          <ScorePill label="Quality" value={sc.answer_quality} />
          <ScorePill label="Tools" value={sc.tool_accuracy} />
          <ScorePill label="Composite" value={sc.composite} />
        </div>
        <span style={{ color: 'var(--muted)', fontSize: 12 }}>{expanded ? '▲' : '▼'}</span>
      </div>

      {expanded && (
        <div style={{ padding: '14px 16px' }}>
          {result.error && (
            <div style={{ color: 'var(--red)', fontSize: 12, marginBottom: 10 }}>
              Error: {result.error}
            </div>
          )}

          {tools.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 5, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Tools called
              </div>
              <div>{tools.map((t, i) => <ToolBadge key={i} name={t.tool || t} />)}</div>
            </div>
          )}

          {result.response && (
            <div>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Response
              </div>
              <div style={{
                fontSize: 13, lineHeight: 1.6, color: 'var(--text)',
                whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                maxHeight: 400, overflowY: 'auto',
                background: 'var(--panel-2)', borderRadius: 6, padding: '10px 12px',
              }}>
                {result.response}
              </div>
            </div>
          )}

          {sc.answer_reason && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                Judge reasoning
              </div>
              <div style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5 }}>
                {sc.answer_reason}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function CustomPrompt() {
  const [providers, setProviders] = useState([])
  const [cfg, setCfg] = useState({})
  const [question, setQuestion] = useState('')
  const [turns, setTurns] = useState([])
  const [expectedTools, setExpectedTools] = useState('')
  const [running, setRunning] = useState(false)
  const [results, setResults] = useState([])  // [{model, response, tool_calls, scorecard, error}]
  const [status, setStatus] = useState('')
  const [error, setError] = useState('')
  const esRef = useRef(null)

  useEffect(() => {
    getProviders()
      .then(d => {
        const ps = (d.providers || []).filter(p => !p.bare_key)
        setProviders(ps)
        const init = {}
        for (const p of ps) {
          init[p.id] = {
            enabled: false,
            apiKey: '',
            baseUrl: p.default_url || '',
            selectedModel: p.models?.[0] || '',
          }
        }
        setCfg(init)
      })
      .catch(() => {})
  }, [])

  const update = (id, patch) =>
    setCfg(prev => ({ ...prev, [id]: { ...prev[id], ...patch } }))

  const toggle = (id) => update(id, { enabled: !cfg[id]?.enabled })

  const enabledModels = () => {
    const out = []
    for (const p of providers) {
      const state = cfg[p.id]
      if (!state?.enabled || !state.selectedModel) continue
      const key = p.id === 'lmstudio'
        ? `lmstudio:${state.selectedModel}`
        : `${p.id}:${state.selectedModel}`
      out.push({ model_key: key, api_key: state.apiKey || null, base_url: state.baseUrl || null })
    }
    return out
  }

  const handleRun = async () => {
    const models = enabledModels()
    if (!question.trim() || !models.length) return

    setRunning(true)
    setResults([])
    setStatus('Starting…')
    setError('')

    const parsedTools = expectedTools.trim()
      ? expectedTools.split(',').map(t => t.trim()).filter(Boolean)
      : []

    try {
      const data = await createCustomPrompt({
        question: question.trim(),
        turns: turns.filter(t => t.trim()),
        expected_tools: parsedTools,
        mock_tools: {},
        models,
      })

      const url = streamCustomPromptUrl(data.run_id)
      const es = new EventSource(url)
      esRef.current = es

      es.onmessage = (e) => {
        const ev = JSON.parse(e.data)
        if (ev.type === 'started') {
          setStatus(`Running on ${ev.total} model${ev.total !== 1 ? 's' : ''}…`)
        } else if (ev.type === 'model_started') {
          setStatus(`Running ${ev.model}…`)
        } else if (ev.type === 'model_done') {
          setResults(prev => [...prev, {
            model: ev.model,
            response: ev.response,
            tool_calls: ev.tool_calls,
            scorecard: ev.scorecard,
            error: ev.error,
          }])
        } else if (ev.type === 'done') {
          setStatus(ev.status === 'completed' ? 'Done' : ev.status)
          setRunning(false)
          es.close()
        }
      }

      es.onerror = () => {
        setError('Stream disconnected')
        setRunning(false)
        es.close()
      }
    } catch (e) {
      setError(e.message)
      setRunning(false)
    }
  }

  const addTurn = () => setTurns(prev => [...prev, ''])
  const updateTurn = (i, v) => setTurns(prev => prev.map((t, j) => j === i ? v : t))
  const removeTurn = (i) => setTurns(prev => prev.filter((_, j) => j !== i))

  const modelCount = enabledModels().length
  const canRun = question.trim() && modelCount > 0 && !running

  // Group providers for display
  const cloudProviders = providers.filter(p => p.kind === 'cloud')
  const localProviders = providers.filter(p => p.kind === 'local')

  return (
    <div>
      <div className="section-header" style={{ marginBottom: 20 }}>
        <span className="section-title">Custom Prompt</span>
        <span style={{ fontSize: 12, color: 'var(--muted)', marginLeft: 12 }}>
          Run a free-form question against any model — compare results across providers or against live Condor
        </span>
      </div>

      {/* Question */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Question / Prompt</div>
        <textarea
          style={{
            width: '100%', minHeight: 100, background: 'var(--panel-2)',
            border: '1px solid var(--border)', borderRadius: 6,
            color: 'var(--text)', fontSize: 13, padding: '10px 12px',
            resize: 'vertical', lineHeight: 1.5, fontFamily: 'inherit',
          }}
          placeholder="e.g. Create a grid trading strategy for BTC-USDT using 80% of my $1,000 portfolio…"
          value={question}
          onChange={e => setQuestion(e.target.value)}
        />

        {/* Extra turns */}
        {turns.map((t, i) => (
          <div key={i} style={{ display: 'flex', gap: 8, marginTop: 10, alignItems: 'flex-start' }}>
            <div style={{ paddingTop: 8, color: 'var(--muted)', fontSize: 11, minWidth: 60 }}>
              Turn {i + 2}
            </div>
            <textarea
              style={{
                flex: 1, minHeight: 60, background: 'var(--panel-2)',
                border: '1px solid var(--border)', borderRadius: 6,
                color: 'var(--text)', fontSize: 13, padding: '8px 10px',
                resize: 'vertical', lineHeight: 1.5, fontFamily: 'inherit',
              }}
              placeholder="Follow-up message…"
              value={t}
              onChange={e => updateTurn(i, e.target.value)}
            />
            <button
              className="btn sm"
              style={{ marginTop: 4, color: 'var(--red)' }}
              onClick={() => removeTurn(i)}
            >✕</button>
          </div>
        ))}

        <div style={{ marginTop: 12, display: 'flex', gap: 10, alignItems: 'center' }}>
          <button className="btn sm" onClick={addTurn}>+ Add turn</button>
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>
            Multi-turn conversations test context retention across follow-ups
          </span>
        </div>

        {/* Expected tools */}
        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 6 }}>
            Expected tools <span style={{ fontSize: 11 }}>(optional — comma-separated, enables tool accuracy scoring)</span>
          </div>
          <input
            type="text"
            className="input"
            placeholder="e.g. get_market_data, manage_executors"
            value={expectedTools}
            onChange={e => setExpectedTools(e.target.value)}
            style={{ maxWidth: 420 }}
          />
        </div>
      </div>

      {/* Model selection */}
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="card-title">Models</div>

        {[{ label: 'Cloud APIs', list: cloudProviders }, { label: 'Local Models', list: localProviders }]
          .filter(g => g.list.length > 0)
          .map(g => (
            <div key={g.label} style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 11, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: '0.06em', marginBottom: 8 }}>
                {g.label}
              </div>
              <div className="provider-list">
                {g.list.map(p => {
                  const state = cfg[p.id]
                  if (!state) return null
                  const allModels = p.models || []
                  return (
                    <div key={p.id} className={`provider-row ${state.enabled ? 'enabled' : ''}`}>
                      <div className="provider-header" onClick={() => toggle(p.id)}>
                        <label className="toggle" onClick={e => e.stopPropagation()}>
                          <input type="checkbox" checked={state.enabled} onChange={() => toggle(p.id)} />
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
                                placeholder={p.key_hint || 'API key…'}
                                value={state.apiKey}
                                onChange={e => update(p.id, { apiKey: e.target.value })}
                              />
                            </div>
                          )}
                          {p.supports_url && (
                            <div className="field">
                              <label>Base URL</label>
                              <input
                                type="text"
                                className="input"
                                placeholder={p.default_url || 'http://host:port'}
                                value={state.baseUrl}
                                onChange={e => update(p.id, { baseUrl: e.target.value })}
                              />
                            </div>
                          )}
                          {allModels.length > 0 && (
                            <div className="field">
                              <label>Model</label>
                              <input
                                type="text"
                                className="input"
                                value={state.selectedModel}
                                onChange={e => update(p.id, { selectedModel: e.target.value })}
                                list={`cp-models-${p.id}`}
                                style={{ maxWidth: 340 }}
                              />
                              <datalist id={`cp-models-${p.id}`}>
                                {allModels.map(m => <option key={m} value={m} />)}
                              </datalist>
                            </div>
                          )}
                          {allModels.length === 0 && (
                            <div className="field">
                              <label>Model name</label>
                              <input
                                type="text"
                                className="input"
                                placeholder="e.g. llama3.1:8b"
                                value={state.selectedModel}
                                onChange={e => update(p.id, { selectedModel: e.target.value })}
                                style={{ maxWidth: 340 }}
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
          ))}
      </div>

      {/* Run bar */}
      <div className="run-summary-bar">
        <div className="run-summary-info">
          {modelCount === 0
            ? 'Select at least one model above'
            : <><strong>{modelCount}</strong> model{modelCount !== 1 ? 's' : ''} selected</>}
          {status && <span style={{ marginLeft: 12, color: 'var(--muted)', fontSize: 12 }}>{status}</span>}
          {error && <span className="error-text" style={{ marginLeft: 12 }}>{error}</span>}
        </div>
        <button className="btn primary" onClick={handleRun} disabled={!canRun}>
          {running ? '⏳ Running…' : '▶ Run Prompt'}
        </button>
      </div>

      {/* Results */}
      {results.length > 0 && (
        <div style={{ marginTop: 24 }}>
          <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12, textTransform: 'uppercase', letterSpacing: '0.06em' }}>
            Results — {results.length} model{results.length !== 1 ? 's' : ''}
          </div>
          {results.map((r, i) => (
            <ModelResult key={i} model={r.model} result={r} />
          ))}
        </div>
      )}
    </div>
  )
}
