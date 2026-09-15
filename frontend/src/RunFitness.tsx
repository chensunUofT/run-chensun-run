import { useEffect, useState } from 'react'
import { request } from './lib/api'
import type { Translate } from './i18n'
import './fitness.css'

type FitnessConfidence = 'high' | 'medium' | 'low' | 'insufficient'

type FitnessFactors = {
  evidence_class?: string | null
  score_kind?: string | null
  weather_time_penalty_percent?: number | null
  elevation_time_penalty_percent?: number | null
  weather_available?: boolean | null
  ascent_source?: string | null
  training_run_not_all_out?: boolean | null
}

type FitnessResult = {
  score: number | null
  method: string
  confidence: FitnessConfidence
  factors: FitnessFactors
}

type FitnessState = {
  id: string | number
  status: 'loading' | 'success' | 'error'
  data: FitnessResult | null
}

function finiteNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function normalizeFitness(value: unknown): FitnessResult | null {
  if (!value || typeof value !== 'object') return null
  const record = value as Record<string, unknown>
  const rawFactors = record.factors
  const factors = rawFactors && typeof rawFactors === 'object' ? rawFactors as Record<string, unknown> : {}
  const confidence = record.confidence
  const safeConfidence: FitnessConfidence = confidence === 'high' || confidence === 'medium' || confidence === 'low' || confidence === 'insufficient' ? confidence : 'insufficient'
  return {
    score: finiteNumber(record.score),
    method: typeof record.method === 'string' ? record.method : 'insufficient_data',
    confidence: safeConfidence,
    factors: {
      evidence_class: typeof factors.evidence_class === 'string' ? factors.evidence_class : null,
      score_kind: typeof factors.score_kind === 'string' ? factors.score_kind : null,
      weather_time_penalty_percent: finiteNumber(factors.weather_time_penalty_percent),
      elevation_time_penalty_percent: finiteNumber(factors.elevation_time_penalty_percent),
      weather_available: typeof factors.weather_available === 'boolean' ? factors.weather_available : null,
      ascent_source: typeof factors.ascent_source === 'string' ? factors.ascent_source : null,
      training_run_not_all_out: typeof factors.training_run_not_all_out === 'boolean' ? factors.training_run_not_all_out : null,
    },
  }
}

function methodLabel(method: string, t: Translate) {
  const normalized = method.toLowerCase()
  if (normalized.includes('race') || normalized.includes('vdot_daniels')) return t('fitness.methodRace')
  if (normalized.includes('time_trial')) return t('fitness.methodTimeTrial')
  if (normalized.includes('interval')) return t('fitness.methodInterval')
  if (normalized.includes('tempo')) return t('fitness.methodTempo')
  if (normalized.includes('long')) return t('fitness.methodLong')
  if (normalized.includes('easy')) return t('fitness.methodEasy')
  if (normalized.includes('quality')) return t('fitness.methodQuality')
  if (normalized.includes('insufficient')) return t('fitness.methodInsufficient')
  return t('fitness.methodGeneral')
}

function confidenceLabel(confidence: FitnessConfidence, t: Translate) {
  return t(`fitness.confidence${confidence.charAt(0).toUpperCase()}${confidence.slice(1)}`)
}

function penaltyLabel(value: number) {
  if (value < 0.1) return value.toFixed(2)
  return value.toFixed(1)
}

export function RunFitness({ runId, t }: { runId: string | number; t: Translate }) {
  const [state, setState] = useState<FitnessState>({ id: runId, status: 'loading', data: null })

  useEffect(() => {
    let active = true
    setState({ id: runId, status: 'loading', data: null })
    request<unknown>(`/api/runs/${encodeURIComponent(runId)}/fitness`)
      .then((payload) => {
        const data = normalizeFitness(payload)
        if (!data) throw new Error('Invalid fitness response')
        if (active) setState({ id: runId, status: 'success', data })
      })
      .catch(() => {
        if (active) setState({ id: runId, status: 'error', data: null })
      })
    return () => { active = false }
  }, [runId])

  const current = state.id === runId ? state : { id: runId, status: 'loading' as const, data: null }
  if (current.status === 'loading') return <div className="run-fitness run-fitness-pending" role="status"><span className="run-fitness-spinner" />{t('fitness.loading')}</div>
  if (current.status === 'error' || !current.data) return <section className="run-fitness run-fitness-unavailable" aria-label={t('fitness.title')}><div className="run-fitness-unavailable-mark" aria-hidden="true">—</div><div><strong>{t('fitness.title')}</strong><span>{t('fitness.unavailable')}</span></div></section>

  const result = current.data
  const factors = result.factors
  const trainingEstimate = factors.training_run_not_all_out === true || factors.score_kind !== 'vdot'
  const scoreLabel = trainingEstimate ? t('fitness.effortEstimate') : t('fitness.raceVdot')
  const score = result.score == null ? '—' : result.score.toFixed(1)
  const chips: string[] = []
  const heatPenalty = factors.weather_time_penalty_percent
  const ascentPenalty = factors.elevation_time_penalty_percent
  if (heatPenalty != null && heatPenalty > 0) chips.push(t('fitness.heatPenalty', { value: penaltyLabel(heatPenalty) }))
  if (ascentPenalty != null && ascentPenalty > 0) chips.push(t('fitness.ascentPenalty', { value: penaltyLabel(ascentPenalty) }))

  return <section className={`run-fitness run-fitness-${result.confidence}`} aria-label={t('fitness.title')}>
    <div className="run-fitness-main">
      <div className="run-fitness-heading"><span className="run-fitness-eyebrow">{t('fitness.eyebrow')}</span><strong>{t('fitness.title')}</strong></div>
      <div className="run-fitness-summary"><span>{scoreLabel}</span><small>{methodLabel(result.method, t)} · {confidenceLabel(result.confidence, t)}</small></div>
      {chips.length > 0 && <div className="run-fitness-factors">{chips.map((chip) => <span key={chip}>{chip}</span>)}</div>}
      <p className="run-fitness-note">{trainingEstimate ? t('fitness.trainingNote') : t('fitness.raceNote')}</p>
    </div>
    <div className="run-fitness-score"><strong>{score}</strong><span>{t('fitness.score')}</span></div>
  </section>
}
