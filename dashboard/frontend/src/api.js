async function handle(res) {
  if (!res.ok) {
    let msg
    try { msg = (await res.json()).detail } catch { msg = await res.text() }
    throw new Error(msg || `${res.status} ${res.statusText}`)
  }
  return res.json()
}

export const getConfig = () => fetch('/api/config').then(handle)
export const getProviders = () => fetch('/api/providers').then(handle)

export function getProviderModels(baseUrl, apiKey = '', provider = '') {
  const params = new URLSearchParams({ base_url: baseUrl, api_key: apiKey })
  if (provider) params.set('provider', provider)
  return fetch('/api/provider-models?' + params).then(handle)
}

/** Model ids an ACP bridge (Claude Code, Gemini CLI) will accept. */
export function getAcpModels(provider) {
  return fetch('/api/acp-models?' + new URLSearchParams({ provider })).then(handle)
}

export const createRun = (body) =>
  fetch('/api/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(handle)

export const createCustomPrompt = (body) =>
  fetch('/api/custom-prompt', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(handle)

export const streamCustomPromptUrl = (id) => `/api/custom-prompt/${id}/stream`

export const cancelRun = (id) =>
  fetch(`/api/runs/${id}`, { method: 'DELETE' }).then(handle)

/** Stop after the in-flight case finishes. Resolves on acceptance, not on pause. */
export const pauseRun = (id) =>
  fetch(`/api/runs/${id}/pause`, { method: 'POST' }).then(handle)

export const resumeRun = (id) =>
  fetch(`/api/runs/${id}/resume`, { method: 'POST' }).then(handle)

export const listRuns = () => fetch('/api/runs').then(handle)
export const getRun = (dir) => fetch(`/api/runs/${dir}`).then(handle)
export const streamUrl = (id) => `/api/runs/${id}/stream`

export const getStaging = () => fetch('/api/staging').then(handle)
export const getDatasets = () => fetch('/api/datasets').then(handle)
export const getModelRegistry = () => fetch('/api/models').then(handle)

export function getMatrix() {
  return fetch('/api/matrix').then(handle)
}

export function getRouting({ minPassRate, minCases, preferLowerTokens } = {}) {
  const params = new URLSearchParams()
  if (minPassRate != null) params.set('min_pass_rate', String(minPassRate))
  if (minCases != null) params.set('min_cases', String(minCases))
  if (preferLowerTokens) params.set('prefer_lower_tokens', 'true')
  return fetch('/api/routing?' + params).then(handle)
}

// ── Suites / Environments ─────────────────────────────────────────────────────
export const listEnvironments = () => fetch('/api/environments').then(handle)
export const createEnvironment = (body) =>
  fetch('/api/environments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)
export const patchEnvironment = (id, body) =>
  fetch(`/api/environments/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)
export const deleteEnvironment = (id) =>
  fetch(`/api/environments/${id}`, { method: 'DELETE' }).then(handle)
export const validateEnvironment = (id) =>
  fetch(`/api/environments/${id}/validate`).then(handle)

export const listSuites = () => fetch('/api/suites').then(handle)
export const getSuite = (id) => fetch(`/api/suites/${id}`).then(handle)
export const createSuite = (body) =>
  fetch('/api/suites', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)
export const patchSuite = (id, body) =>
  fetch(`/api/suites/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)
export const deleteSuite = (id) =>
  fetch(`/api/suites/${id}`, { method: 'DELETE' }).then(handle)

export const createSuiteCase = (suiteId, body) =>
  fetch(`/api/suites/${suiteId}/cases`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)
export const deleteSuiteCase = (suiteId, caseId, version) =>
  fetch(`/api/suites/${suiteId}/cases/${encodeURIComponent(caseId)}?version=${version}`, {
    method: 'DELETE',
  }).then(handle)
export const importSuiteCases = (suiteId, body) =>
  fetch(`/api/suites/${suiteId}/cases/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)

export const listSuiteRuns = (suiteId) =>
  fetch(`/api/suites/${suiteId}/runs`).then(handle)
export const runSuite = (suiteId, body = {}) =>
  fetch(`/api/suites/${suiteId}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(handle)

export function compareRuns({ runGroup, runs } = {}) {
  const params = new URLSearchParams()
  if (runGroup) params.set('run_group', runGroup)
  if (runs?.length) params.set('runs', runs.join(','))
  return fetch('/api/compare?' + params).then(handle)
}

export const getRunGroup = (id) => fetch(`/api/run-groups/${id}`).then(handle)

export const getSettings = () => fetch('/api/settings').then(handle)
export const updateSettings = (updates) =>
  fetch('/api/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  }).then(handle)
