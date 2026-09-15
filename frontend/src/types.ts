export type RunId = number | string

export type Run = {
  id: RunId
  title: string
  started_at: string
  /** Activity date in the source's recorded local timezone, when available. */
  local_date?: string | null
  distance_km: number
  duration_seconds: number
  moving_seconds?: number | null
  elapsed_seconds?: number | null
  stopped_seconds?: number | null
  run_type: string
  avg_hr: number | null
  shoe_id: RunId | null
  notes: string
  rpe: number | null
  source: string
  shoe_assignment?: 'manual' | 'inferred' | 'unassigned' | string
  shoe_confidence?: number | null
  shoe_reason?: string | null
  /** Backend calls this `stream_available`; keep the plural alias for older API fixtures. */
  stream_available?: boolean
  streams_available?: boolean
}

export type RunPayload = Omit<Run, 'id'>

export type StatsBucket = {
  label: string
  distance_km: number
  run_count: number
}

export type Stats = {
  total_distance_km: number
  total_duration_seconds: number
  run_count: number
  average_pace_seconds: number | null
  buckets: StatsBucket[]
  start_date?: string | null
  end_date?: string | null
}

export type AuthConfig = {
  auth_required: boolean
  supabase_url: string | null
  supabase_publishable_key: string | null
  personal_mode?: boolean
}

export type UserProfile = {
  id: string
  user_id: string
  email?: string | null
  role?: string | null
}

export type ShoeRules = {
  min_distance_km?: number | null
  max_distance_km?: number | null
  min_pace_seconds?: number | null
  max_pace_seconds?: number | null
  run_types?: string[]
  priority?: number
}

export type Shoe = {
  id: RunId
  name: string
  brand: string
  initial_distance_km: number
  status: string
  total_distance_km: number
  purchase_date?: string | null
  image_url?: string | null
  rules?: ShoeRules | null
}

export type ShoePayload = Omit<Shoe, 'id' | 'total_distance_km'>

export type Integration = {
  id: string
  name: string
  status: string
  message: string
}

export type ShoeCatalogItem = {
  colorway?: string | null
  id: string
  name: string
  brand: string
  image_url?: string | null
  source_url?: string | null
  source?: string | null
  attribution?: string | null
}

export type ShoePrediction = {
  run_id: RunId
  shoe_id: RunId | null
  shoe_name?: string | null
  run_title?: string | null
  run_started_at?: string | null
  score?: number | null
  confidence?: number | null
  candidates?: Array<Record<string, unknown>> | null
  applied?: boolean
  reason?: string | null
}

export type ShoeInference = {
  apply?: boolean
  predictions?: ShoePrediction[]
  assignments?: ShoePrediction[]
  matches?: ShoePrediction[]
  count?: number
  updated_count?: number
  updated?: number
  skipped_manual?: number
}

export type ShareKind = 'run' | 'week' | 'month'

export type Share = {
  id?: RunId
  kind: ShareKind
  run_id?: RunId | null
  date?: string | null
  url?: string
  token?: string
  expires_at?: string
  created_at?: string
}

export type PublicShareSnapshot = {
  kind: ShareKind
  run?: Partial<Run> | null
  runs?: Partial<Run>[]
  stats?: Partial<Stats> | null
  date?: string | null
  expires_at?: string | null
  created_at?: string | null
  [key: string]: unknown
}

export type RunStreamSample = {
  altitude_m?: number | null
  elapsed_seconds?: number | null
  distance_m?: number | null
  distance_km?: number | null
  pace_seconds?: number | null
  heart_rate?: number | null
  latitude?: number | null
  longitude?: number | null
  lat?: number | null
  lon?: number | null
  cadence?: number | null
  elevation_m?: number | null
  [key: string]: unknown
}

export type RunLap = {
  start_seconds?: number | null
  end_seconds?: number | null
  lap?: number | null
  split?: number | null
  label?: string | null
  distance_m?: number | null
  distance_km?: number | null
  elapsed_seconds?: number | null
  moving_seconds?: number | null
  active_duration_seconds?: number | null
  duration_seconds?: number | null
  pace_seconds?: number | null
  [key: string]: unknown
}

export type RunStreams = {
  samples?: RunStreamSample[] | null
  laps?: RunLap[] | null
  analysis?: Record<string, unknown> | null
  moving_seconds?: number | null
  elapsed_seconds?: number | null
  stopped_seconds?: number | null
  quality_flags?: string[] | null
  intervals?: Array<Record<string, unknown>> | null
  splits?: Array<Record<string, unknown>> | null
  distance_m?: number | null
  moving_pace_seconds?: number | null
  method?: string | null
  run_id?: RunId
  stream_available?: boolean
  available?: boolean
  [key: string]: unknown
}

export type GoogleHealthInspection = {
  raw_example?: Record<string, unknown> | null
  available_types?: string[]
  earliest_imported_date?: string | null
  source_coverage_note?: string | null
  earliest_accessed_date?: string | null
  coverage_start?: string | null
  coverage_end?: string | null
  coverage?: string | null
  data?: unknown
  source_json?: unknown
  [key: string]: unknown
}

export type Analysis = {
  kind: 'rules' | string
  summary: string
  observations: string[]
}

export type ImportResult = {
  imported: number
  skipped: number
}
