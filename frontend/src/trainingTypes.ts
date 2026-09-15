import type { RunId } from './types'

export type TrainingRunType = 'easy' | 'quality' | 'long'

export type WeeklyScheduleItem = {
  weekday: number
  run_type: TrainingRunType
}

export type TrainingGoal = {
  race_date: string
  distance_km: number
  target_seconds: number
  weekly_schedule: WeeklyScheduleItem[]
}

export type TrainingPrediction = {
  seconds: number
  low_seconds: number
  high_seconds: number
  method: string
  /** Backends may return a numeric fraction/percentage or a qualitative level. */
  confidence: number | string
  reference_run_id: RunId | null
}

export type TrainingSession = {
  id: RunId
  date: string
  run_type: TrainingRunType | string
  distance_km: number
  target_pace_seconds: number | null
  description: string
  completed_run_id: RunId | null
  manually_edited: boolean
}

export type TrainingPaces = {
  easy: number | null
  tempo: number | null
  interval: number | null
  long: number | null
}

export type CoachingData = {
  goal: TrainingGoal | null
  prediction: TrainingPrediction | null
  sessions: TrainingSession[]
  paces: TrainingPaces
}

export type TrainingGoalPayload = TrainingGoal

export type TrainingSessionPatch = Partial<
  Pick<TrainingSession, 'date' | 'run_type' | 'distance_km' | 'target_pace_seconds' | 'description'>
>

export type CoachingRequest = {
  method?: string
  body?: string
  headers?: HeadersInit
}
