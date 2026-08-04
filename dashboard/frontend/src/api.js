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

export function getProviderModels(baseUrl, apiKey = '') {
  const params = new URLSearchParams({ base_url: baseUrl, api_key: apiKey })
  return fetch('/api/provider-models?' + params).then(handle)
}

export const createRun = (body) =>
  fetch('/api/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(handle)

export const createCustomPrompt = (body) =>
  fetch('/api/custom-prompt', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }).then(handle)

export const streamCustomPromptUrl = (id) => `/api/custom-prompt/${id}/stream`

export const cancelRun = (id) =>
  fetch(`/api/runs/${id}`, { method: 'DELETE' }).then(handle)

export const listRuns = () => fetch('/api/runs').then(handle)
export const getRun = (dir) => fetch(`/api/runs/${dir}`).then(handle)
export const streamUrl = (id) => `/api/runs/${id}/stream`

export const getStaging = () => fetch('/api/staging').then(handle)
export const getDatasets = () => fetch('/api/datasets').then(handle)
export const getModelRegistry = () => fetch('/api/models').then(handle)

export function getMatrix({ mode } = {}) {
  const params = new URLSearchParams()
  if (mode) params.set('mode', mode)
  return fetch('/api/matrix?' + params).then(handle)
}

export function getRouting({ mode, minPassRate, minCases, preferLowerTokens } = {}) {
  const params = new URLSearchParams()
  if (mode) params.set('mode', mode)
  if (minPassRate != null) params.set('min_pass_rate', String(minPassRate))
  if (minCases != null) params.set('min_cases', String(minCases))
  if (preferLowerTokens) params.set('prefer_lower_tokens', 'true')
  return fetch('/api/routing?' + params).then(handle)
}
