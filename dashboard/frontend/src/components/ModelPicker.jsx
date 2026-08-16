import { useEffect, useMemo, useRef, useState } from 'react'

function asOption(m) {
  if (typeof m === 'string') return { id: m, name: m, description: '' }
  const id = m.id || m.key
  return {
    id,
    name: m.name || m.label || id,
    description: m.description || '',
  }
}

/**
 * Searchable model dropdown for every provider.
 *
 * A native <select> only shows every option for small lists; past a handful
 * of ids the previous picker switched to <datalist>, and Chromium then
 * prefix-filters against the current value — so OpenRouter/Ollama/LM Studio
 * after "Load models" collapsed to the two ids sharing models[0]'s prefix.
 * This menu always lists every match, filtered by substring as you type.
 */
export default function ModelPicker({
  models,
  value,
  onChange,
  placeholder,
  allowEmpty = false,
  emptyLabel = 'None',
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const rootRef = useRef(null)
  const searchRef = useRef(null)
  const selectedRef = useRef(null)

  const options = useMemo(
    () => (models || []).map(asOption).filter(o => o.id),
    [models],
  )

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return options
    return options.filter(o =>
      o.id.toLowerCase().includes(q)
      || o.name.toLowerCase().includes(q)
      || o.description.toLowerCase().includes(q)
    )
  }, [options, query])

  useEffect(() => {
    if (!open) return
    const onDoc = (e) => {
      if (!rootRef.current?.contains(e.target)) {
        setOpen(false)
        setQuery('')
      }
    }
    const onKey = (e) => {
      if (e.key === 'Escape') {
        setOpen(false)
        setQuery('')
      }
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    searchRef.current?.focus()
    selectedRef.current?.scrollIntoView({ block: 'nearest' })
  }, [open])

  const commit = (id) => {
    onChange(id)
    setOpen(false)
    setQuery('')
  }

  if (!options.length) {
    return (
      <input
        type="text"
        className="input"
        placeholder={placeholder || 'model name'}
        value={value}
        onChange={e => onChange(e.target.value)}
      />
    )
  }

  const selected = options.find(o => o.id === value)
  const label = selected
    ? selected.name
    : (value || emptyLabel || placeholder || 'Select model')

  return (
    <div className={`model-picker ${open ? 'open' : ''}`} ref={rootRef}>
      <button
        type="button"
        className="model-picker-trigger"
        onClick={() => setOpen(o => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <span className="model-picker-value">{label}</span>
        <span className="model-picker-caret" aria-hidden>▾</span>
      </button>
      {open && (
        <div className="model-picker-menu">
          <input
            ref={searchRef}
            type="text"
            className="input"
            placeholder={`Filter ${options.length} model${options.length === 1 ? '' : 's'}…`}
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => {
              if (e.key !== 'Enter') return
              const typed = query.trim()
              if (typed && !options.some(o => o.id === typed)) {
                commit(typed)
                return
              }
              if (filtered[0]) commit(filtered[0].id)
            }}
          />
          <div className="model-picker-list" role="listbox">
            {allowEmpty && (
              <button
                type="button"
                className={`model-picker-option ${!value ? 'active' : ''}`}
                onClick={() => commit('')}
              >
                {emptyLabel}
              </button>
            )}
            {filtered.map(o => (
              <button
                key={o.id}
                type="button"
                ref={o.id === value ? selectedRef : null}
                data-selected={o.id === value ? 'true' : undefined}
                className={`model-picker-option ${o.id === value ? 'active' : ''}`}
                onClick={() => commit(o.id)}
                title={o.description || o.id}
              >
                <span className="model-picker-option-name">{o.name}</span>
                {o.name !== o.id && (
                  <span className="model-picker-option-id">{o.id}</span>
                )}
              </button>
            ))}
            {!filtered.length && (
              <div className="model-picker-empty">No matches</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
