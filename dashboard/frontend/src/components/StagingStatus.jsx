import { useState, useEffect, useCallback } from 'react'
import { getStaging } from '../api.js'

/**
 * Staging pre-flight panel.
 *
 * It exists to make one specific failure visible before it happens: condor's
 * .mcp.json declares mcp-hummingbot with no CLI args, so the MCP server can fall
 * back to HUMMINGBOT_API_URL and then to localhost:8000 with admin/admin. On a
 * developer machine that last hop is plausibly the real hummingbot-api. So the
 * checks are shown even when they pass — "which API am I about to trade on" is
 * not a question that should need digging.
 */
export default function StagingStatus({ compact = false, onReport }) {
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(!compact)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await getStaging()
      setReport(data)
      onReport?.(data)
    } catch {
      setReport(null)
    } finally {
      setLoading(false)
    }
  }, [onReport])

  useEffect(() => { load() }, [load])

  if (loading && !report) {
    return <div className="card"><div className="run-meta">Checking staging…</div></div>
  }
  if (!report) return null

  const blocking = (report.checks || []).filter(c => c.blocking)
  const failures = blocking.filter(c => !c.ok)
  const readOnlyFailures = failures.filter(c => !c.mutating_only)
  const mutatingFailures = failures.filter(c => c.mutating_only)

  const state = readOnlyFailures.length ? 'blocked' : (mutatingFailures.length ? 'partial' : 'ready')
  const headline = {
    blocked: 'Runs refused',
    partial: 'Read-only runs allowed',
    ready: 'Staging ready',
  }[state]

  return (
    <div className="card">
      <div className="staging-head">
        <span className={`staging-pill ${state}`}>{headline}</span>
        {report.api_url && <span className="staging-url">{report.api_url}</span>}
        {report.server_name && <span className="run-meta">server “{report.server_name}”</span>}
        <span className={`run-meta ${report.allow_mutating ? 'staging-mutating' : ''}`}>
          {report.allow_mutating ? 'mutating allowed' : 'read-only'}
        </span>
        <button className="btn sm" onClick={() => setExpanded(!expanded)} style={{ marginLeft: 'auto' }}>
          {expanded ? 'Hide checks' : `${blocking.length} checks`}
        </button>
        <button className="btn sm" onClick={load} disabled={loading}>{loading ? '…' : '↻'}</button>
      </div>

      {state === 'blocked' && (
        <div className="error-text" style={{ marginTop: 10 }}>
          A blocking check failed, so a live run will refuse to start. This is deliberate:
          the alternative is benchmarking against whatever happens to answer on localhost.
        </div>
      )}
      {state === 'partial' && (
        <div style={{ marginTop: 10, color: 'var(--yellow)', fontSize: 12 }}>
          Read-only cases can run. Mutating and destructive cases stay blocked until the
          mutating-only checks pass.
        </div>
      )}

      {expanded && (
        <div className="staging-checks">
          {(report.checks || []).map(c => (
            <div key={c.name} className={`staging-check ${c.ok ? 'ok' : (c.blocking ? 'fail' : 'warn')}`}>
              <span className="staging-mark">{c.ok ? '✓' : (c.blocking ? '✗' : '•')}</span>
              <span className="staging-name">
                {c.name}
                {c.mutating_only && <span className="staging-scope">mutating only</span>}
                {!c.blocking && <span className="staging-scope">advisory</span>}
              </span>
              <span className="staging-detail">{c.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
