import type {
  Analysis,
  AuthConfig,
  GoogleHealthInspection,
  ImportResult,
  Integration,
  PublicShareSnapshot,
  Run,
  RunId,
  RunPayload,
  RunStreams,
  Share,
  ShareKind,
  Shoe,
  ShoeCatalogItem,
  ShoeInference,
  ShoePayload,
  Stats,
  UserProfile,
} from '../types'

const configuredBase = (import.meta.env.VITE_API_BASE as string | undefined)?.replace(/\/$/, '')
const API_BASE = configuredBase ?? ''

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(message: string, status: number, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type RequestOptions = RequestInit & { auth?: boolean }
type AccessTokenProvider = () => Promise<string | null>
let accessTokenProvider: AccessTokenProvider | null = null

export function setAccessTokenProvider(provider: AccessTokenProvider | null) {
  accessTokenProvider = provider
}

function readableError(value: unknown) {
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && 'msg' in item) return String((item as { msg?: unknown }).msg)
        return JSON.stringify(item)
      })
      .filter(Boolean)
      .join('; ')
  }
  if (value && typeof value === 'object' && 'msg' in value) return String((value as { msg?: unknown }).msg)
  if (value && typeof value === 'object') {
    const detail = value as Record<string, unknown>
    if (Array.isArray(detail.missing_columns)) return `Missing columns: ${detail.missing_columns.join(', ')}`
    if (Array.isArray(detail.unknown_columns)) return `Unsupported columns: ${detail.unknown_columns.join(', ')}`
    if (Array.isArray(detail.rows)) return `CSV validation failed: ${detail.rows.map(row => JSON.stringify(row)).join('; ')}`
    return JSON.stringify(value)
  }
  return undefined
}

export async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { auth = true, ...requestInit } = init ?? {}
  const token = auth && accessTokenProvider ? await accessTokenProvider() : null
  const headers = new Headers(requestInit.headers)
  if (!(requestInit.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...requestInit, headers, credentials: 'include' })

  if (!response.ok) {
    let detail: unknown
    let message = `Request failed (${response.status})`
    try {
      const body = (await response.json()) as { detail?: unknown; message?: unknown }
      detail = body.detail ?? body.message
      message = readableError(detail) ?? message
    } catch {
      // The status message is enough when the server returns a non-JSON error.
    }
    throw new ApiError(message, response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

async function requestBlob(path: string, init?: RequestOptions) {
  const { auth = true, ...requestInit } = init ?? {}
  const token = auth && accessTokenProvider ? await accessTokenProvider() : null
  const headers = new Headers(requestInit.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(`${API_BASE}${path}`, { ...requestInit, headers, credentials: 'include' })
  if (!response.ok) throw new ApiError(`Export failed (${response.status})`, response.status)
  return response.blob()
}

function asArray<T>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[]
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    for (const key of ['items', 'catalog', 'shoes', 'predictions', 'assignments', 'matches']) {
      if (Array.isArray(record[key])) return record[key] as T[]
    }
  }
  return []
}

export const api = {
  getConfig: () => request<AuthConfig>('/api/config', { auth: false }),
  getMe: () => request<UserProfile>('/api/me'),
  getRuns: () => request<Run[]>('/api/runs'),
  createRun: (payload: RunPayload) =>
    request<Run>('/api/runs', { method: 'POST', body: JSON.stringify(payload) }),
  updateRun: (id: RunId, payload: Partial<RunPayload>) =>
    request<Run>(`/api/runs/${encodeURIComponent(String(id))}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteRun: (id: RunId) =>
    request<void>(`/api/runs/${encodeURIComponent(String(id))}`, { method: 'DELETE' }),
  seedDemo: () => request<{ inserted?: number; message?: string }>('/api/demo/seed', { method: 'POST' }),
  getStats: (period: 'week' | 'month', date?: string) => {
    const query = new URLSearchParams({ period })
    if (date) query.set('date', date)
    return request<Stats>(`/api/stats?${query.toString()}`)
  },
  getShoes: () => request<Shoe[]>('/api/shoes'),
  createShoe: (payload: ShoePayload) =>
    request<Shoe>('/api/shoes', { method: 'POST', body: JSON.stringify(payload) }),
  updateShoe: (id: RunId, payload: Partial<ShoePayload>) =>
    request<Shoe>(`/api/shoes/${encodeURIComponent(String(id))}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  getIntegrations: () => request<Integration[]>('/api/integrations'),
  getAnalysis: (id: RunId) =>
    request<Analysis>(`/api/runs/${encodeURIComponent(String(id))}/analysis`),
  getRunStreams: (id: RunId) =>
    request<RunStreams>(`/api/runs/${encodeURIComponent(String(id))}/streams`),
  putRunStreams: (id: RunId, streams: RunStreams) =>
    request<RunStreams>(`/api/runs/${encodeURIComponent(String(id))}/streams`, {
      method: 'PUT',
      body: JSON.stringify(streams),
    }),
  getShoeCatalog: async () => {
    const value = await request<unknown>('/api/shoe-catalog')
    return asArray<ShoeCatalogItem>(value)
  },
  inferShoes: (payload: { apply: boolean }) =>
    request<ShoeInference>('/api/shoes/infer', { method: 'POST', body: JSON.stringify(payload) }),
  createShare: (payload: { kind: ShareKind; run_id?: RunId; date?: string; expires_days?: number }) =>
    request<Share>('/api/shares', { method: 'POST', body: JSON.stringify(payload) }),
  listShares: () => request<Share[]>('/api/shares'),
  revokeShare: (id: RunId | string) =>
    request<void>(`/api/shares/${encodeURIComponent(String(id))}`, { method: 'DELETE' }),
  getPublicShare: (token: string) =>
    request<PublicShareSnapshot>(`/api/public/shares/${encodeURIComponent(token)}`, { auth: false }),
  connectGoogleHealth: () => request<{ url: string }>('/api/integrations/google-health/connect', { method: 'POST' }),
  syncGoogleHealth: (payload: { start_date?: string; full_history?: boolean }) =>
    request<Record<string, unknown>>(`/api/integrations/google-health/sync?${new URLSearchParams({ ...(payload.start_date ? { start_date: payload.start_date } : {}), full_history: String(Boolean(payload.full_history)) }).toString()}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  getGoogleHealthInspection: () =>
    request<GoogleHealthInspection>('/api/integrations/google-health/data-inspection'),
  importCsv: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return request<ImportResult>('/api/import/csv', { method: 'POST', body })
  },
  exportData: () => requestBlob('/api/export'),
}
