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
