/**
 * Settings — edit .env-backed config from the dashboard (trusted-local).
 */
import { useEffect, useMemo, useState } from 'react'
import { getSettings, updateSettings } from '../api.js'

export default function Settings({ onSaved }) {
  const [fields, setFields] = useState([])
  const [envPath, setEnvPath] = useState('')
  const [note, setNote] = useState('')
  const [draft, setDraft] = useState({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  const load = () =>
    getSettings()
      .then((d) => {
        setFields(d.fields || [])
        setEnvPath(d.env_path || '')
        setNote(d.note || '')
        const init = {}
        for (const f of d.fields || []) {
          init[f.key] = f.value || ''
        }
        setDraft(init)
      })
      .catch((e) => setError(String(e.message || e)))

  useEffect(() => {
    load()
  }, [])

  const groups = useMemo(() => {
    const order = []
    const map = {}
    for (const f of fields) {
      if (!map[f.group]) {
        map[f.group] = []
        order.push(f.group)
      }
      map[f.group].push(f)
    }
    return order.map((g) => ({ name: g, fields: map[g] }))
  }, [fields])

  const handleSave = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    setSaved(false)
    try {
      const updates = {}
      for (const f of fields) {
        const next = draft[f.key] ?? ''
        const prev = f.value || ''
        if (f.secret) {
          // Empty or still-masked → keep existing secret (avoid accidental clear).
          if (!next || next.startsWith('••••')) continue
          updates[f.key] = next
          continue
        }
        if (next === prev) continue
        updates[f.key] = next
      }
      const d = await updateSettings(updates)
      setFields(d.fields || [])
      const init = {}
      for (const f of d.fields || []) init[f.key] = f.value || ''
      setDraft(init)
      setSaved(true)
      onSaved?.()
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <div className="section-header">
        <span className="section-title">Settings</span>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <p className="muted">{note}</p>
        <p className="muted" style={{ marginTop: 6 }}>
          Writing to <code>{envPath || '.env'}</code>
        </p>
      </div>

      {error && (
        <div className="card" style={{ borderColor: 'var(--red)', marginBottom: 12, color: 'var(--red)' }}>
          {error}
        </div>
      )}
      {saved && !error && (
        <div className="card" style={{ borderColor: 'var(--green)', marginBottom: 12, color: 'var(--green)' }}>
          Saved. Process env updated for this server session.
        </div>
      )}

      <form onSubmit={handleSave}>
        {groups.map((g) => (
          <div className="card" key={g.name} style={{ marginBottom: 16 }}>
            <div className="card-title">{g.name}</div>
            <div className="settings-grid">
              {g.fields.map((f) => (
                <label key={f.key} className="settings-field">
                  <span className="settings-label">
                    {f.label}
                    {f.secret && f.has_value && (
                      <span className="muted"> · configured</span>
                    )}
                  </span>
                  {f.choices ? (
                    <select
                      value={draft[f.key] || ''}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d, [f.key]: e.target.value }))
                      }
                    >
                      <option value="">(unset)</option>
                      {f.choices.map((c) => (
                        <option key={c} value={c}>
                          {c}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <input
                      type={f.secret ? 'password' : 'text'}
                      value={draft[f.key] || ''}
                      placeholder={f.secret ? (f.has_value ? '•••• leave blank to keep' : '') : ''}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d, [f.key]: e.target.value }))
                      }
                      onFocus={() => {
                        // Clear masked placeholder so typing replaces the secret.
                        if (f.secret && (draft[f.key] || '').startsWith('••••')) {
                          setDraft((d) => ({ ...d, [f.key]: '' }))
                        }
                      }}
                      autoComplete="off"
                    />
                  )}
                  {f.hint && <span className="muted settings-hint">{f.hint}</span>}
                </label>
              ))}
            </div>
          </div>
        ))}

        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? 'Saving…' : 'Save settings'}
        </button>
      </form>
    </div>
  )
}
