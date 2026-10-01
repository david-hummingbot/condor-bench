import { useEffect, useRef } from 'react'
import { saveFormState } from './api.js'

/**
 * Latest fields for this browser tab. A page writes here immediately, and the
 * other page reads it on open, so a navigation that outruns the .env save
 * cannot reload the previous values and write them back.
 */
let memory = null

export function currentForm() {
  return memory
}

export function seedForm(state) {
  if (memory || !state) return
  memory = JSON.parse(JSON.stringify(state))
  delete memory.warning
}

function writeMemory(serialized) {
  const patch = JSON.parse(serialized)
  const base = memory || JSON.parse(JSON.stringify(EMPTY_FORM))
  memory = {
    providers: patch.providers !== undefined ? patch.providers : (base.providers || {}),
    benchmark: patch.benchmark || base.benchmark || EMPTY_FORM.benchmark,
    prompt: patch.prompt || base.prompt || EMPTY_FORM.prompt,
  }
}

export const EMPTY_FORM = {
  providers: {},
  benchmark: { layers: [], domain: '', category: '', riskLevels: [], coreOnly: false },
  prompt: { question: '', turns: [], expectedTools: '', agentSlug: '' },
}

export const SAVED_FIELDS_HINT =
  'Anything you type here is saved to .env and restored on Benchmark and Prompt. Clear a field to forget it.'

/**
 * Fields worth restoring for one provider. Untouched providers (still on the
 * catalog defaults, and switched off) are omitted so a later save drops them.
 */
export function providerFromSaved(provider, saved) {
  const entry = saved?.providers?.[provider.id]
  if (!entry) {
    return {
      enabled: false,
      apiKey: '',
      baseUrl: provider.default_url || '',
      selectedModel: provider.models?.[0] || '',
    }
  }
  return {
    enabled: !!entry.enabled,
    apiKey: entry.apiKey || '',
    baseUrl: entry.baseUrl ?? (provider.default_url || ''),
    selectedModel: entry.selectedModel ?? (provider.models?.[0] || ''),
  }
}

export function sparseProviders(providers, cfg) {
  const out = {}
  for (const provider of providers) {
    const state = cfg[provider.id]
    if (!state) continue
    const fresh = providerFromSaved(provider, {})
    const entry = {
      enabled: !!state.enabled,
      apiKey: state.apiKey || '',
      baseUrl: state.baseUrl ?? '',
      selectedModel: state.selectedModel || '',
    }
    if (
      entry.enabled === fresh.enabled
      && entry.apiKey === fresh.apiKey
      && entry.baseUrl === fresh.baseUrl
      && entry.selectedModel === fresh.selectedModel
    ) {
      continue
    }
    out[provider.id] = entry
  }
  return out
}

/**
 * Write the durable slice of the open page. The first snapshot after hydration
 * is what was just loaded, so it is not written back. Leaving the page flushes
 * a pending edit so the other page reloads it.
 */
function persist(serialized) {
  return saveFormState(JSON.parse(serialized))
}

export function useSavedForm(section, sectionValue, providers, cfg, hydrated, onError) {
  const serialized = JSON.stringify({
    providers: sparseProviders(providers, cfg),
    [section]: sectionValue,
  })
  const last = useRef(null)
  const serializedRef = useRef(serialized)
  const hydratedRef = useRef(hydrated)
  const onErrorRef = useRef(onError)
  serializedRef.current = serialized
  hydratedRef.current = hydrated
  onErrorRef.current = onError

  useEffect(() => {
    const flush = () => {
      if (!hydratedRef.current || last.current === null) return
      const pending = serializedRef.current
      if (last.current === pending) return
      last.current = pending
      fetch('/api/form-state', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: pending,
        keepalive: true,
      }).catch(() => {})
    }
    window.addEventListener('pagehide', flush)
    return () => window.removeEventListener('pagehide', flush)
  }, [])

  useEffect(() => {
    if (!hydrated) return undefined
    writeMemory(serialized)
    if (last.current === null) {
      last.current = serialized
      return undefined
    }
    if (last.current === serialized) return undefined
    const pending = serialized
    const timer = setTimeout(() => {
      last.current = pending
      persist(pending).then(() => onErrorRef.current?.('')).catch((e) => {
        onErrorRef.current?.(e.message || 'Could not save fields to .env.')
      })
    }, 400)
    return () => {
      clearTimeout(timer)
      if (last.current !== pending) {
        last.current = pending
        persist(pending).then(() => onErrorRef.current?.('')).catch((e) => {
          onErrorRef.current?.(e.message || 'Could not save fields to .env.')
        })
      }
    }
  }, [hydrated, serialized])
}
