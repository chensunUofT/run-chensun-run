import { useCallback, useEffect, useMemo, useState, type CSSProperties, type FormEvent } from 'react'
import { localeTag, type Translate } from './i18n'
import { request } from './lib/api'
import type { Run, RunId } from './types'
import './training.css'
import type {
  CoachingData,
  TrainingGoal,
  TrainingGoalPayload,
  TrainingPaces,
  TrainingPrediction,
  TrainingRunType,
  TrainingSession,
  TrainingSessionPatch,
  WeeklyScheduleItem,
} from './trainingTypes'

export type TrainingViewProps = {
  runs: Run[]
  locale: 'en' | 'zh'
  onSelectRun: (run: Run) => void
  t: Translate
}

export type TrainingCalendarProps = TrainingViewProps & {
  period: 'week' | 'month'
  date: string
  sessions?: TrainingSession[]
  loading?: boolean
  error?: string | null
  onRetry?: () => void
  onDateChange?: (date: string) => void
}

type LoadStatus = 'idle' | 'loading' | 'success' | 'error'

type Resource<T> = {
  status: LoadStatus
  data: T | null
  error: string | null
}

type GoalFormState = {
  raceDate: string
  distancePreset: string
  customDistance: string
  hours: string
  minutes: string
  seconds: string
  days: number[]
  roles: Record<number, TrainingRunType>
}

type SessionFormState = {
  date: string
  runType: TrainingRunType | string
  distance: string
  pace: string
  description: string
}

const KNOWN_DISTANCES = ['5', '10', '21.0975', '42.195']
const DEFAULT_DAYS = [1, 3, 5, 6]
const DAY_PREFERENCE = [1, 3, 5, 6, 0, 2, 4]
const DEFAULT_ROLES: Record<number, TrainingRunType> = {
  0: 'easy',
  1: 'easy',
  2: 'easy',
  3: 'quality',
  4: 'easy',
  5: 'easy',
  6: 'long',
}

const EMPTY_PACES: TrainingPaces = { easy: null, tempo: null, interval: null, long: null }

function emptyResource<T>(): Resource<T> {
  return { status: 'idle', data: null, error: null }
}

function asFiniteNumber(value: unknown): number | null {
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) ? number : null
}

function normalizeCoaching(value: Partial<CoachingData> | null | undefined): CoachingData {
  const goal = value?.goal ?? null
  const prediction = value?.prediction ?? null
  const sessions = Array.isArray(value?.sessions) ? value.sessions : []
  const paces = value?.paces ?? EMPTY_PACES
  return {
    goal,
    prediction,
    sessions: sessions.filter(Boolean).map((session) => ({
      ...session,
      id: session.id,
      date: session.date,
      run_type: session.run_type || 'easy',
      distance_km: asFiniteNumber(session.distance_km) ?? 0,
      target_pace_seconds: asFiniteNumber(session.target_pace_seconds),
      description: session.description ?? '',
      completed_run_id: session.completed_run_id ?? null,
      manually_edited: Boolean(session.manually_edited),
    })),
    paces: {
      easy: asFiniteNumber(paces.easy),
      tempo: asFiniteNumber(paces.tempo),
      interval: asFiniteNumber(paces.interval),
      long: asFiniteNumber(paces.long),
    },
  }
}

function responseHasCoachingData(value: unknown): value is CoachingData {
  return Boolean(value && typeof value === 'object' && ('goal' in value || 'sessions' in value || 'paces' in value))
}

function dateFromKey(value: string | null | undefined): Date | null {
  if (!value) return null
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (match) {
    const year = Number(match[1])
    const month = Number(match[2])
    const day = Number(match[3])
    const date = new Date(year, month - 1, day, 12)
    return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function dateKey(value: string | Date | null | undefined): string | null {
  const date = typeof value === 'string' ? dateFromKey(value) : value
  if (!date || Number.isNaN(date.getTime())) return null
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function runDateKey(run: Pick<Run, 'started_at' | 'local_date'>) {
  return dateKey(run.local_date) ?? dateKey(run.started_at)
}

function todayKey() {
  return dateKey(new Date()) ?? ''
}

function parseTargetTime(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return { hours: '', minutes: '', seconds: '' }
  const rounded = Math.round(seconds)
  return {
    hours: String(Math.floor(rounded / 3600)),
    minutes: String(Math.floor((rounded % 3600) / 60)).padStart(2, '0'),
    seconds: String(rounded % 60).padStart(2, '0'),
  }
}

function createGoalForm(goal: TrainingGoal | null): GoalFormState {
  if (!goal) {
    return {
      raceDate: '',
      distancePreset: '',
      customDistance: '',
      hours: '',
      minutes: '',
      seconds: '',
      days: [...DEFAULT_DAYS],
      roles: { ...DEFAULT_ROLES },
    }
  }
  const targetTime = parseTargetTime(goal.target_seconds)
  const days = Array.from(
    new Set(
      (goal.weekly_schedule ?? [])
        .map((item) => Number(item.weekday))
        .filter((weekday) => Number.isInteger(weekday) && weekday >= 0 && weekday <= 6),
    ),
  ).sort((left, right) => left - right)
  const safeDays = days.length >= 2 ? days : [...DEFAULT_DAYS]
  const roles = { ...DEFAULT_ROLES }
  ;(goal.weekly_schedule ?? []).forEach((item) => {
    const weekday = Number(item.weekday)
    if (weekday >= 0 && weekday <= 6 && (item.run_type === 'easy' || item.run_type === 'quality' || item.run_type === 'long')) {
      roles[weekday] = item.run_type
    }
  })
  const distance = String(goal.distance_km)
  return {
    raceDate: goal.race_date?.slice(0, 10) ?? '',
    distancePreset: KNOWN_DISTANCES.includes(distance) ? distance : 'custom',
    customDistance: KNOWN_DISTANCES.includes(distance) ? '' : distance,
    hours: targetTime.hours,
    minutes: targetTime.minutes,
    seconds: targetTime.seconds,
    days: safeDays,
    roles,
  }
}

function formatDate(value: string | Date | null | undefined, _locale: 'en' | 'zh') {
  const date = typeof value === 'string' ? dateFromKey(value) : value
  if (!date || Number.isNaN(date.getTime())) return value ? String(value).slice(0, 10) : '—'
  return dateKey(date) ?? '—'
}

function formatMonth(value: Date, _locale: 'en' | 'zh') {
  return value.getFullYear() + '-' + String(value.getMonth() + 1).padStart(2, '0')
}

function formatTime(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const remainder = rounded % 60
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
}

function formatPace(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) return '—'
  const rounded = Math.round(seconds)
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`
}

function formatDistance(distance: number | null | undefined, locale: 'en' | 'zh') {
  if (distance == null || !Number.isFinite(distance)) return '—'
  return distance.toLocaleString(localeTag(locale), { maximumFractionDigits: 1 })
}

function isDemoRun(run: Run) {
  const source = String(run.source ?? '').toLowerCase()
  const title = String(run.title ?? '').toLowerCase()
  return source.includes('demo') || source.includes('sample') || title.includes('demo') || title.includes('sample')
}

function realRuns(runs: Run[]) {
  return runs.filter((run) => !isDemoRun(run))
}

function typeKey(value: string | null | undefined): 'easy' | 'quality' | 'long' | 'run' {
  const normalized = String(value ?? '').toLowerCase()
  if (normalized === 'long') return 'long'
  if (normalized === 'easy') return 'easy'
  if (normalized === 'quality' || normalized === 'tempo' || normalized === 'interval') return 'quality'
  return 'run'
}

function runVisualType(run: Run) {
  const normalized = String(run.run_type ?? '').toLowerCase()
  if (normalized !== 'run' && normalized !== '') return run.run_type
  // Provider imports sometimes use the generic `run` type. Only infer an
  // intensity chip when the title explicitly contains a repeat prescription.
  const title = String(run.title ?? '').trim().replace(/\s+/g, ' ')
  return /(?:\d+(?:\.\d+)?|n)\s*x\s*\d+(?:\.\d+)?\s*(?:m|km)\b|\d+(?:\.\d+)?\s*(?:m|km)\s*x\s*(?:\d+(?:\.\d+)?|n)\b/i.test(title) ? 'quality' : 'run'
}

function typeLabel(value: string | null | undefined, t: Translate) {
  const normalized = String(value ?? '').toLowerCase()
  if (normalized === 'long') return t('training.long')
  if (normalized === 'quality') return t('training.quality')
  if (normalized === 'tempo') return t('training.paceTempo')
  if (normalized === 'interval') return t('training.paceInterval')
  if (normalized === 'easy') return t('training.easy')
  if (normalized === 'race') return t('training.raceDay')
  if (normalized === 'rest') return t('training.rest')
  return t('training.legendRun')
}

function localizeTiming(value: string, t: Translate, emptyKey = 'briefly') {
  const trimmed = value.trim()
  if (!trimmed || /^briefly$/i.test(trimmed)) return t(`training.${emptyKey}`)
  const match = trimmed.match(/^(\d+(?:\.\d+)?)\s*(min|mins|minute|minutes|sec|secs|second|seconds)$/i)
  if (!match) return trimmed
  const amount = Number(match[1])
  const unit = /^sec/i.test(match[2]) ? (amount === 1 ? t('training.secondUnit') : t('training.secondsUnit')) : (amount === 1 ? t('training.minuteUnit') : t('training.minutesUnit'))
  return `${amount} ${unit}`
}

function localizeRecovery(value: string, t: Translate) {
  const trimmed = value.trim()
  if (/^easy$/i.test(trimmed)) return t('training.easyRecovery')
  const match = trimmed.match(/^(\d+(?:\.\d+)?)\s*(min|mins|minute|minutes|sec|secs|second|seconds)\s+easy$/i)
  if (!match) return trimmed
  return `${localizeTiming(`${match[1]} ${match[2]}`, t)} ${t('training.easyRecovery')}`
}

function sessionDescription(session: TrainingSession, t: Translate) {
  if (session.manually_edited) return session.description || typeLabel(session.run_type, t)
  const description = String(session.description ?? '')
  const limited = /limited history/i.test(description)
  const normalized = description.toLowerCase()
  if (normalized.includes('easy aerobic')) return t(limited ? 'training.generatedEasyLimited' : 'training.generatedEasy')
  if (normalized.includes('long relaxed')) return t(limited ? 'training.generatedLongLimited' : 'training.generatedLong')
  if (normalized.includes('warm up') && normalized.includes('stay within the planned distance')) {
    const segments = description.split(';').map((segment) => segment.trim())
    const warmup = segments[0]?.replace(/^warm up\s+/i, '').trim() ?? ''
    const workout = segments[1] ?? ''
    const workoutMatch = workout.match(/^(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*min at\s+(.+?)\s+pace with\s+(.+?)\s+recovery$/i)
    const cooldownSegment = segments.find((segment) => /^cool down/i.test(segment)) ?? ''
    const cooldown = cooldownSegment.replace(/^cool down\s*/i, '').trim()
    if (warmup && workoutMatch) {
      const kind = /interval/i.test(workoutMatch[3]) ? t('training.paceInterval') : /tempo/i.test(workoutMatch[3]) ? t('training.paceTempo') : t('training.quality')
      return t('training.generatedQualityDynamic', {
        warmup: localizeTiming(warmup, t),
        reps: Number(workoutMatch[1]),
        work: localizeTiming(`${workoutMatch[2]} min`, t),
        kind,
        recovery: localizeRecovery(workoutMatch[4], t),
        cooldown: localizeTiming(cooldown, t, 'cooldownBriefly'),
        limited: limited ? t('training.limitedHistory') : '',
      })
    }
  }
  if (normalized.includes('3 x 5 min') && normalized.includes('tempo')) return t(limited ? 'training.generatedQualityTempoLimited' : 'training.generatedQualityTempo')
  if (normalized.includes('6 x 1 min') && normalized.includes('interval')) return t(limited ? 'training.generatedQualityIntervalLimited' : 'training.generatedQualityInterval')
  if (normalized.includes('3 x 2 min')) return t(limited ? 'training.generatedQualityTaperLimited' : 'training.generatedQualityTaper')
  if (normalized.includes('race day')) return t('training.generatedRace')
  if (normalized.includes('rest day')) return t('training.generatedRest')
  return description || typeLabel(session.run_type, t)
}

function runDisplayLabel(run: Run, locale: 'en' | 'zh', t: Translate) {
  const title = String(run.title ?? '').trim()
  if (title && !/^(run|running|google health run|treadmill run)$/i.test(title)) return title
  const visualType = runVisualType(run)
  if (typeKey(visualType) === 'run') return `${formatDistance(run.distance_km, locale)} ${t('training.distanceUnit')}`
  return `${typeLabel(visualType, t)} · ${formatDistance(run.distance_km, locale)} ${t('training.distanceUnit')}`
}

function confidenceLabel(value: number | string, t: Translate) {
  const numeric = Number(value)
  if (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '' && Number.isFinite(numeric))) {
    const percentage = Math.round(numeric <= 1 ? numeric * 100 : numeric)
    return t('training.confidence', { value: percentage })
  }
  const normalized = String(value).toLowerCase()
  if (normalized === 'high') return t('training.confidenceHigh')
  if (normalized === 'medium' || normalized === 'med') return t('training.confidenceMedium')
  if (normalized === 'low') return t('training.confidenceLow')
  return t('training.confidenceUnknown')
}

function dayLabel(weekday: number, t: Translate) {
  return t(
    ['training.weekdayMon', 'training.weekdayTue', 'training.weekdayWed', 'training.weekdayThu', 'training.weekdayFri', 'training.weekdaySat', 'training.weekdaySun'][weekday] ?? 'training.weekdayMon',
  )
}

function errorText(error: unknown, t: Translate, fallback = 'training.loadError') {
  if (error instanceof Error && error.message && !/^Request failed \(/.test(error.message)) return error.message
  return t(fallback)
}

function updateDayCount(current: number[], count: number) {
  const selected = new Set(current)
  if (count > selected.size) {
    DAY_PREFERENCE.forEach((day) => {
      if (selected.size < count) selected.add(day)
    })
  } else if (count < selected.size) {
    DAY_PREFERENCE.slice().reverse().forEach((day) => {
      if (selected.size > count) selected.delete(day)
    })
  }
  return Array.from(selected).sort((left, right) => left - right)
}

function goalPayload(form: GoalFormState): { payload: TrainingGoalPayload | null; error: string | null } {
  if (!form.raceDate) return { payload: null, error: 'training.dateRequired' }
  const distance = form.distancePreset === 'custom' ? Number(form.customDistance) : Number(form.distancePreset)
  if (!Number.isFinite(distance) || distance <= 0) {
    return { payload: null, error: form.distancePreset ? 'training.distanceInvalid' : 'training.distanceRequired' }
  }
  if (!form.hours && !form.minutes && !form.seconds) return { payload: null, error: 'training.timeRequired' }
  const hours = Number(form.hours || 0)
  const minutes = Number(form.minutes || 0)
  const seconds = Number(form.seconds || 0)
  if (!Number.isInteger(hours) || !Number.isInteger(minutes) || !Number.isInteger(seconds) || hours < 0 || minutes < 0 || minutes > 59 || seconds < 0 || seconds > 59) {
    return { payload: null, error: 'training.timeInvalid' }
  }
  const targetSeconds = hours * 3600 + minutes * 60 + seconds
  if (targetSeconds <= 0) return { payload: null, error: 'training.timeInvalid' }
  if (form.days.length < 2) return { payload: null, error: 'training.scheduleRequired' }
  if (form.days.length > 6) return { payload: null, error: 'training.scheduleTooMany' }
  if (form.days.filter((weekday) => form.roles[weekday] === 'quality').length > 1) return { payload: null, error: 'training.qualityTooMany' }
  const weeklySchedule: WeeklyScheduleItem[] = form.days.map((weekday) => ({
    weekday,
    run_type: form.roles[weekday] ?? 'easy',
  }))
  return {
    payload: {
      race_date: form.raceDate,
      distance_km: distance,
      target_seconds: targetSeconds,
      weekly_schedule: weeklySchedule,
    },
    error: null,
  }
}

function TrainingError({ message, onRetry, t }: { message: string; onRetry?: () => void; t: Translate }) {
  return (
    <section className="training-error" role="alert">
      <div className="training-state-mark" aria-hidden="true">!</div>
      <div>
        <strong>{t('errors.generic')}</strong>
        <p>{message}</p>
        {onRetry && <button className="text-button" type="button" onClick={onRetry}>{t('training.retry')} <span aria-hidden="true">→</span></button>}
      </div>
    </section>
  )
}

function TrainingLoader({ t, compact = false }: { t: Translate; compact?: boolean }) {
  return (
    <div className={`training-loader${compact ? ' training-loader-compact' : ''}`} role="status" aria-live="polite">
      <span className="training-loader-dot" />
      <span className="training-loader-dot" />
      <span className="training-loader-dot" />
      <span className="training-loader-label">{t('training.loading')}</span>
    </div>
  )
}

function TypeChip({ type, t, outlined = false }: { type: string; t: Translate; outlined?: boolean }) {
  const kind = typeKey(type)
  return <span className={`training-type-chip training-type-${kind}${outlined ? ' training-type-chip-outlined' : ''}`}><span className="training-type-dot" />{typeLabel(type, t)}</span>
}

function GoalEditor({
  goal,
  saving,
  onSubmit,
  onCancel,
  t,
}: {
  goal: TrainingGoal | null
  saving: boolean
  onSubmit: (payload: TrainingGoalPayload) => Promise<void>
  onCancel?: () => void
  t: Translate
}) {
  const [form, setForm] = useState<GoalFormState>(() => createGoalForm(goal))
  const [error, setError] = useState<string | null>(null)
  const update = <K extends keyof GoalFormState>(key: K, value: GoalFormState[K]) => setForm((current) => ({ ...current, [key]: value }))
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const result = goalPayload(form)
    if (result.error || !result.payload) {
      setError(result.error ? t(result.error) : t('training.goalRequired'))
      return
    }
    setError(null)
    await onSubmit(result.payload)
  }
  const toggleDay = (weekday: number) => {
    setForm((current) => {
      if (current.days.includes(weekday)) {
        if (current.days.length <= 2) return current
        return { ...current, days: current.days.filter((day) => day !== weekday) }
      }
      if (current.days.length >= 6) return current
      return { ...current, days: [...current.days, weekday].sort((left, right) => left - right) }
    })
  }
  const updateRole = (weekday: number, runType: TrainingRunType) => setForm((current) => ({ ...current, roles: { ...current.roles, [weekday]: runType } }))

  return (
    <section className="card training-goal-editor">
      <div className="training-card-heading">
        <div>
          <span className="section-eyebrow">{t('training.setupEyebrow')}</span>
          <h3>{goal ? t('training.editGoal') : t('training.setupTitle')}</h3>
          <p>{t('training.setupCopy')}</p>
        </div>
        {goal && onCancel && <button className="text-button" type="button" onClick={onCancel}>{t('training.cancel')}</button>}
      </div>
      <form className="training-goal-form" onSubmit={(event) => void submit(event)}>
        <div className="training-form-grid training-form-grid-goal">
          <label className="training-field">
            <span>{t('training.raceDate')}</span>
            <input type="date" value={form.raceDate} onChange={(event) => update('raceDate', event.target.value)} required />
            <small>{t('training.raceDateHint')}</small>
          </label>
          <label className="training-field">
            <span>{t('training.distance')}</span>
            <select value={form.distancePreset} onChange={(event) => update('distancePreset', event.target.value)} required>
              <option value="" disabled>{t('training.distancePlaceholder')}</option>
              <option value="5">5 {t('training.distanceUnit')}</option>
              <option value="10">10 {t('training.distanceUnit')}</option>
              <option value="21.0975">21.0975 {t('training.distanceUnit')}</option>
              <option value="42.195">42.195 {t('training.distanceUnit')}</option>
              <option value="custom">{t('training.distanceCustom')}</option>
            </select>
            {form.distancePreset === 'custom' && <span className="training-input-with-unit"><input type="number" min="0.1" step="0.1" value={form.customDistance} placeholder={t('training.distanceCustomPlaceholder')} onChange={(event) => update('customDistance', event.target.value)} aria-label={t('training.distanceCustom')} /><em>{t('training.distanceUnit')}</em></span>}
          </label>
        </div>

        <fieldset className="training-fieldset">
          <legend>{t('training.targetTime')}</legend>
          <div className="training-time-inputs">
            <label className="training-field"><span>{t('training.hours')}</span><input type="number" min="0" max="99" inputMode="numeric" value={form.hours} onChange={(event) => update('hours', event.target.value)} /></label>
            <span className="training-time-separator" aria-hidden="true">:</span>
            <label className="training-field"><span>{t('training.minutes')}</span><input type="number" min="0" max="59" inputMode="numeric" value={form.minutes} onChange={(event) => update('minutes', event.target.value)} /></label>
            <span className="training-time-separator" aria-hidden="true">:</span>
            <label className="training-field"><span>{t('training.seconds')}</span><input type="number" min="0" max="59" inputMode="numeric" value={form.seconds} onChange={(event) => update('seconds', event.target.value)} /></label>
          </div>
        </fieldset>

        <fieldset className="training-fieldset training-days-fieldset">
          <legend>{t('training.trainingDays')}</legend>
          <div className="training-days-heading"><small>{t('training.trainingDaysHint')}</small><strong>{t('training.daysSelected', { count: form.days.length })}</strong></div>
          <input className="training-days-range" type="range" min="2" max="6" step="1" value={form.days.length} onChange={(event) => update('days', updateDayCount(form.days, Number(event.target.value)))} aria-label={t('training.trainingDays')} />
          <div className="training-day-options">
            {[0, 1, 2, 3, 4, 5, 6].map((weekday) => {
              const selected = form.days.includes(weekday)
              return (
                <div className={`training-day-option${selected ? ' training-day-option-selected' : ''}`} key={weekday}>
                  <label className="training-day-toggle">
                    <input type="checkbox" checked={selected} onChange={() => toggleDay(weekday)} />
                    <span>{dayLabel(weekday, t)}</span>
                  </label>
                  <label className="training-role-field">
                    <span className="sr-only">{t('training.sessionType')}</span>
                    <select value={form.roles[weekday] ?? 'easy'} disabled={!selected} onChange={(event) => updateRole(weekday, event.target.value as TrainingRunType)}>
                      <option value="easy">{t('training.easy')}</option>
                      <option value="quality">{t('training.quality')}</option>
                      <option value="long">{t('training.long')}</option>
                    </select>
                  </label>
                </div>
              )
            })}
          </div>
        </fieldset>

        {error && <p className="training-form-error" role="alert">{error}</p>}
        <div className="training-form-actions">
          {onCancel && <button className="button button-secondary" type="button" onClick={onCancel}>{t('training.cancel')}</button>}
          <button className="button button-primary" type="submit" disabled={saving}>{saving ? <><span className="button-spinner" />{t('actions.loading')}</> : goal ? t('training.updateGoal') : t('training.saveGoal')}</button>
        </div>
      </form>
    </section>
  )
}

function GoalSummary({ goal, locale, t, onEdit }: { goal: TrainingGoal; locale: 'en' | 'zh'; t: Translate; onEdit: () => void }) {
  const schedule = [...(goal.weekly_schedule ?? [])].sort((left, right) => left.weekday - right.weekday)
  return (
    <section className="card training-goal-summary">
      <div className="training-goal-summary-main">
        <span className="section-eyebrow">{t('training.goalSummary', { distance: formatDistance(goal.distance_km, locale), date: formatDate(goal.race_date, locale) })}</span>
        <h3>{formatDistance(goal.distance_km, locale)} {t('training.distanceUnit')} <span aria-hidden="true">·</span> {formatTime(goal.target_seconds)}</h3>
        <p>{t('training.target')} {formatTime(goal.target_seconds)} · {formatDate(goal.race_date, locale)}</p>
      </div>
      <div className="training-goal-summary-side">
        <span className="training-summary-label">{t('training.daysPerWeek', { count: schedule.length })}</span>
        <div className="training-schedule-chips">{schedule.map((item) => <span className="training-schedule-chip" key={item.weekday}><strong>{dayLabel(item.weekday, t)}</strong><TypeChip type={item.run_type} t={t} /></span>)}</div>
        <button className="button button-secondary" type="button" onClick={onEdit}>{t('training.editGoal')}</button>
      </div>
    </section>
  )
}

function PredictionCard({ prediction, runs, locale, t, onSelectRun }: { prediction: TrainingPrediction | null; runs: Run[]; locale: 'en' | 'zh'; t: Translate; onSelectRun: (run: Run) => void }) {
  if (!prediction) {
    return <article className="card training-prediction-card training-prediction-empty"><span className="section-eyebrow">{t('training.predictionEyebrow')}</span><h3>{t('training.predictionTitle')}</h3><p>{t('training.noPrediction')}</p></article>
  }
  const confidenceValue = Number(prediction.confidence)
  const confidence = Number.isFinite(confidenceValue) ? Math.round(confidenceValue <= 1 ? confidenceValue * 100 : confidenceValue) : 50
  const referenceRun = prediction.reference_run_id == null ? null : runs.find((run) => String(run.id) === String(prediction.reference_run_id))
  const low = Math.min(prediction.low_seconds, prediction.high_seconds)
  const high = Math.max(prediction.low_seconds, prediction.high_seconds)
  return (
    <article className="card training-prediction-card">
      <div className="training-card-heading"><div><span className="section-eyebrow">{t('training.predictionEyebrow')}</span><h3>{t('training.predictionTitle')}</h3></div><span className="training-confidence">{confidenceLabel(prediction.confidence, t)}</span></div>
      <div className="training-prediction-time"><span>{t('training.predictionTime')}</span><strong>{formatTime(prediction.seconds)}</strong></div>
      <div className="training-prediction-range"><span>{t('training.predictionRange', { low: formatTime(low), high: formatTime(high) })}</span><div className="training-range-track"><span style={{ left: `${Math.max(4, Math.min(92, confidence))}%` }} /></div></div>
      <p className="training-caution">{t('training.predictionCaution', { confidence: confidenceLabel(prediction.confidence, t) })}</p>
      <div className="training-prediction-meta"><span>{t('training.predictionMethod', { method: prediction.method || '—' })}</span>{referenceRun && <button className="text-button" type="button" onClick={() => onSelectRun(referenceRun)}>{t('training.referenceRun', { run: referenceRun.title || formatDate(referenceRun.started_at, locale) })}</button>}</div>
    </article>
  )
}

function PaceGuide({ paces, t }: { paces: TrainingPaces; t: Translate }) {
  const items: Array<{ key: keyof TrainingPaces; label: string; color: string }> = [
    { key: 'easy', label: t('training.paceEasy'), color: 'easy' },
    { key: 'tempo', label: t('training.paceTempo'), color: 'quality' },
    { key: 'interval', label: t('training.paceInterval'), color: 'interval' },
    { key: 'long', label: t('training.paceLong'), color: 'long' },
  ]
  return (
    <article className="card training-pace-card">
      <div className="training-card-heading"><div><span className="section-eyebrow">{t('training.paceEyebrow')}</span><h3>{t('training.paceTitle')}</h3></div></div>
      <p>{t('training.paceCopy')}</p>
      <div className="training-pace-list">{items.map((item) => <div className="training-pace-row" key={item.key}><span className={`training-pace-mark training-type-${item.color}`} /><span>{item.label}</span><strong>{formatPace(paces[item.key])}<small>{paces[item.key] != null ? t('training.perKm') : ''}</small></strong></div>)}</div>
    </article>
  )
}

function sessionForm(session: TrainingSession): SessionFormState {
  const rawType = String(session.run_type ?? 'easy').toLowerCase()
  const runType = ['easy', 'quality', 'long', 'race', 'rest'].includes(rawType) ? rawType : 'easy'
  return {
    date: session.date?.slice(0, 10) ?? '',
    runType,
    distance: session.distance_km == null ? '' : String(session.distance_km),
    pace: session.target_pace_seconds == null ? '' : String(session.target_pace_seconds),
    description: session.description ?? '',
  }
}

function SessionEditor({ session, saving, onSave, onCancel, t }: { session: TrainingSession; saving: boolean; onSave: (patch: TrainingSessionPatch) => Promise<void>; onCancel: () => void; t: Translate }) {
  const [form, setForm] = useState<SessionFormState>(() => sessionForm(session))
  const [error, setError] = useState<string | null>(null)
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const distance = Number(form.distance)
    const pace = form.pace.trim() ? Number(form.pace) : null
    if (!form.date) {
      setError(t('training.dateRequired'))
      return
    }
    if (form.runType === 'rest') {
      setError(null)
      await onSave({ date: form.date, run_type: 'rest', description: form.description })
      return
    }
    if (!Number.isFinite(distance) || distance <= 0 || (pace != null && (!Number.isFinite(pace) || pace <= 0))) {
      setError(t('training.distanceInvalid'))
      return
    }
    setError(null)
    await onSave({ date: form.date, run_type: form.runType, distance_km: distance, target_pace_seconds: pace, description: form.description })
  }
  return (
    <form className="training-session-editor" onSubmit={(event) => void submit(event)}>
      <div className="training-session-editor-grid">
        <label className="training-field"><span>{t('training.date')}</span><input type="date" value={form.date} onChange={(event) => setForm((current) => ({ ...current, date: event.target.value }))} /></label>
        <label className="training-field"><span>{t('training.sessionType')}</span><select value={form.runType} onChange={(event) => setForm((current) => ({ ...current, runType: event.target.value }))}><option value="easy">{t('training.easy')}</option><option value="quality">{t('training.quality')}</option><option value="long">{t('training.long')}</option><option value="race">{t('training.raceDay')}</option><option value="rest">{t('training.rest')}</option></select></label>
        {form.runType !== 'rest' && <label className="training-field"><span>{t('training.distanceShort')} ({t('training.distanceUnit')})</span><input type="number" min="0.1" step="0.1" value={form.distance} onChange={(event) => setForm((current) => ({ ...current, distance: event.target.value }))} /></label>}
        {form.runType !== 'rest' && <label className="training-field"><span>{t('training.pace')} ({t('training.perKm')})</span><input type="number" min="1" step="1" value={form.pace} onChange={(event) => setForm((current) => ({ ...current, pace: event.target.value }))} placeholder="—" /></label>}
      </div>
      <label className="training-field"><span>{t('training.description')}</span><textarea rows={2} value={form.description} onChange={(event) => setForm((current) => ({ ...current, description: event.target.value }))} /></label>
      {error && <p className="training-form-error" role="alert">{error}</p>}
      <div className="training-session-editor-actions"><button className="button button-secondary" type="button" onClick={onCancel}>{t('training.cancel')}</button><button className="button button-primary" type="submit" disabled={saving}>{saving ? <><span className="button-spinner" />{t('training.savingSession')}</> : t('training.saveSession')}</button></div>
    </form>
  )
}

function PlanList({ sessions, runs, locale, t, onSelectRun, onSaveSession }: { sessions: TrainingSession[]; runs: Run[]; locale: 'en' | 'zh'; t: Translate; onSelectRun: (run: Run) => void; onSaveSession: (id: RunId, patch: TrainingSessionPatch) => Promise<void> }) {
  const [editingId, setEditingId] = useState<RunId | null>(null)
  const [savingId, setSavingId] = useState<RunId | null>(null)
  const actual = realRuns(runs)
  const actualById = useMemo(() => new Map(actual.map((run) => [String(run.id), run])), [actual])
  const sortedSessions = useMemo(() => [...sessions].sort((left, right) => String(left.date).localeCompare(String(right.date))), [sessions])
  const save = async (id: RunId, patch: TrainingSessionPatch) => {
    setSavingId(id)
    try {
      await onSaveSession(id, patch)
      setEditingId(null)
    } catch {
      // The parent keeps the editor open and exposes the translated error notice.
    } finally {
      setSavingId(null)
    }
  }
  return (
    <section className="card training-plan-card">
      <div className="training-card-heading"><div><span className="section-eyebrow">{t('training.planEyebrow')}</span><h3>{t('training.planTitle')}</h3><p>{t('training.planCopy')}</p></div><span className="training-plan-count">{t('training.sessionCount', { count: sortedSessions.length })}</span></div>
      {sortedSessions.length === 0 && <div className="training-inline-empty"><strong>{t('training.noSessions')}</strong><span>{t('training.noSessionsCopy')}</span></div>}
      {sortedSessions.length > 0 && <div className="training-session-list"><div className="training-session-list-caption">{t('training.allSessions')}</div>{sortedSessions.map((session) => {
        const completedRun = session.completed_run_id == null ? null : actualById.get(String(session.completed_run_id))
        const isEditing = String(editingId) === String(session.id)
        return (
          <div className={`training-session-item${isEditing ? ' training-session-item-editing' : ''}`} key={String(session.id)}>
            <div className="training-session-main"><time dateTime={session.date}>{formatDate(session.date, locale)}</time><TypeChip type={session.run_type} t={t} />{String(session.run_type).toLowerCase() === 'rest' ? <strong>{t('training.rest')}</strong> : <><strong>{formatDistance(session.distance_km, locale)} {t('training.distanceUnit')}</strong><span>{formatPace(session.target_pace_seconds)}{session.target_pace_seconds != null && t('training.perKm')}</span></>}</div>
            <div className="training-session-copy"><p>{sessionDescription(session, t)}</p>{session.manually_edited && <small className="training-manual-badge">{t('training.manualEdit')}</small>}</div>
            <div className="training-session-status">{completedRun ? <button className="training-recorded-link" type="button" onClick={() => onSelectRun(completedRun)}><span className="training-status-dot training-status-complete" />{t('training.recorded')}</button> : <span><span className="training-status-dot training-status-pending" />{t('training.pending')}</span>}</div>
            <button className="training-session-edit" type="button" onClick={() => setEditingId(isEditing ? null : session.id)} aria-label={`${t('training.editSession')}: ${formatDate(session.date, locale)}`}>{isEditing ? '×' : '✎'}</button>
            {isEditing && <SessionEditor session={session} saving={String(savingId) === String(session.id)} onSave={(patch) => save(session.id, patch)} onCancel={() => setEditingId(null)} t={t} />}
          </div>
        )
      })}</div>}
    </section>
  )
}

function GoalActions({ onRegenerate, busy, t }: { onRegenerate: () => void; busy: boolean; t: Translate }) {
  return <div className="training-regenerate-action"><button className="button button-secondary" type="button" onClick={onRegenerate} disabled={busy}>{busy ? <><span className="button-spinner" />{t('training.regenerating')}</> : t('training.regenerate')}</button><small>{t('training.regenerateCopy')}</small></div>
}

export function CoachDashboard({ runs, locale, onSelectRun, t }: TrainingViewProps) {
  const [resource, setResource] = useState<Resource<CoachingData>>(emptyResource)
  const [goalEditorOpen, setGoalEditorOpen] = useState(false)
  const [goalSaving, setGoalSaving] = useState(false)
  const [regenerating, setRegenerating] = useState(false)
  const [notice, setNotice] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)

  const load = useCallback(async () => {
    setResource((current) => ({ ...current, status: 'loading', error: null }))
    try {
      const data = normalizeCoaching(await request<CoachingData>('/api/coaching'))
      setResource({ status: 'success', data, error: null })
      if (!data.goal) setGoalEditorOpen(true)
    } catch (error) {
      setResource((current) => ({ ...current, status: 'error', error: errorText(error, t) }))
    }
  }, [t])

  useEffect(() => { void load() }, [load])

  const saveGoal = async (payload: TrainingGoalPayload) => {
    setGoalSaving(true)
    setNotice(null)
    try {
      const result = await request<unknown>('/api/coaching/goal', { method: 'PUT', body: JSON.stringify(payload) })
      const data = responseHasCoachingData(result) ? normalizeCoaching(result) : normalizeCoaching(await request<CoachingData>('/api/coaching'))
      setResource({ status: 'success', data, error: null })
      setGoalEditorOpen(false)
      setNotice({ tone: 'success', text: t('training.goalSaved') })
    } catch (error) {
      setNotice({ tone: 'error', text: errorText(error, t, 'training.goalError') })
    } finally {
      setGoalSaving(false)
    }
  }

  const regenerate = async () => {
    setRegenerating(true)
    setNotice(null)
    try {
      const result = await request<unknown>('/api/coaching/generate', { method: 'POST' })
      const data = responseHasCoachingData(result) ? normalizeCoaching(result) : normalizeCoaching(await request<CoachingData>('/api/coaching'))
      setResource({ status: 'success', data, error: null })
      setNotice({ tone: 'success', text: t('training.goalSaved') })
    } catch (error) {
      setNotice({ tone: 'error', text: errorText(error, t) })
    } finally {
      setRegenerating(false)
    }
  }

  const saveSession = async (id: RunId, patch: TrainingSessionPatch) => {
    try {
      const result = await request<unknown>(`/api/coaching/sessions/${encodeURIComponent(String(id))}`, { method: 'PATCH', body: JSON.stringify(patch) })
      if (responseHasCoachingData(result)) {
        setResource({ status: 'success', data: normalizeCoaching(result), error: null })
      } else if (result && typeof result === 'object' && 'deleted' in result && Boolean((result as { deleted?: unknown }).deleted)) {
        setResource((current) => {
          if (!current.data) return current
          return { ...current, data: { ...current.data, sessions: current.data.sessions.filter((session) => String(session.id) !== String(id)) } }
        })
      } else {
        setResource((current) => {
          if (!current.data) return current
          return { ...current, data: { ...current.data, sessions: current.data.sessions.map((session) => String(session.id) === String(id) ? { ...session, ...patch, manually_edited: true } : session) } }
        })
      }
      setNotice({ tone: 'success', text: t('training.sessionSaved') })
    } catch (error) {
      setNotice({ tone: 'error', text: errorText(error, t, 'training.sessionError') })
      throw error
    }
  }

  const data = resource.data
  const goal = data?.goal ?? null
  const visibleRuns = useMemo(() => realRuns(runs), [runs])
  return (
    <div className="view-stack training-dashboard">
      <section className="page-intro-row training-page-intro">
        <div><div className="eyebrow">{t('training.eyebrow')}</div><h2>{t('training.headline')}</h2><p className="lede">{t('training.lede')}</p></div>
        {goal && !goalEditorOpen && <GoalActions onRegenerate={() => void regenerate()} busy={regenerating} t={t} />}
      </section>

      {notice && <div className={`training-notice training-notice-${notice.tone}`} role="status">{notice.text}</div>}
      {resource.status === 'loading' && !data && <><TrainingLoader t={t} /><div className="training-top-grid"><div className="skeleton training-panel-skeleton" /><div className="skeleton training-panel-skeleton" /></div></>}
      {resource.status === 'error' && !data && <TrainingError message={resource.error ?? t('training.loadError')} onRetry={() => void load()} t={t} />}
      {data && <>
        {!goal && goalEditorOpen && <GoalEditor goal={null} saving={goalSaving} onSubmit={saveGoal} t={t} />}
        {goal && goalEditorOpen && <GoalEditor goal={goal} saving={goalSaving} onSubmit={saveGoal} onCancel={() => setGoalEditorOpen(false)} t={t} />}
        {goal && !goalEditorOpen && <>
          <GoalSummary goal={goal} locale={locale} t={t} onEdit={() => setGoalEditorOpen(true)} />
          <div className="training-top-grid"><PredictionCard prediction={data.prediction} runs={visibleRuns} locale={locale} t={t} onSelectRun={onSelectRun} /><PaceGuide paces={data.paces} t={t} /></div>
          <PlanList sessions={data.sessions} runs={visibleRuns} locale={locale} t={t} onSelectRun={onSelectRun} onSaveSession={saveSession} />
          <TrainingCalendar runs={visibleRuns} locale={locale} onSelectRun={onSelectRun} period="month" date={todayKey()} sessions={data.sessions} t={t} />
        </>}
      </>}
    </div>
  )
}

type CalendarDayData = {
  key: string
  date: Date
  inMonth: boolean
}

function mondayIndex(date: Date) {
  return (date.getDay() + 6) % 7
}

function buildMonthDays(anchor: Date): CalendarDayData[] {
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1)
  const last = new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0)
  const start = new Date(anchor.getFullYear(), anchor.getMonth(), 1 - mondayIndex(first))
  const count = Math.ceil((mondayIndex(last) + last.getDate()) / 7) * 7
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index)
    return { key: dateKey(date) ?? '', date, inMonth: date.getMonth() === anchor.getMonth() }
  })
}

function buildWeekDays(anchor: Date): CalendarDayData[] {
  const start = new Date(anchor.getFullYear(), anchor.getMonth(), anchor.getDate() - mondayIndex(anchor))
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index)
    return { key: dateKey(date) ?? '', date, inMonth: true }
  })
}

function circleSize(distance: number | null | undefined, maxDistance: number) {
  const value = Number(distance)
  if (!Number.isFinite(value) || value <= 0) return 19
  const ratio = Math.max(0, Math.min(1, value / Math.max(maxDistance, 1)))
  return Math.round(18 + ratio * 18)
}

function CalendarCircle({ run, session, planned, size, locale, t, onSelectRun }: { run?: Run; session?: TrainingSession; planned?: boolean; size: number; locale: 'en' | 'zh'; t: Translate; onSelectRun: (run: Run) => void }) {
  const type = run ? runVisualType(run) : session?.run_type ?? 'run'
  const distance = run?.distance_km ?? session?.distance_km ?? null
  const label = planned ? (session?.completed_run_id != null ? t('training.completedLabel', { type: typeLabel(type, t), distance: formatDistance(distance, locale) }) : t('training.plannedLabel', { type: typeLabel(type, t), distance: formatDistance(distance, locale) })) : t('training.runLabel', { type: typeLabel(type, t), distance: formatDistance(distance, locale) })
  const style = { '--training-circle-size': `${size}px` } as CSSProperties
  if (run) return <button className={`training-calendar-circle training-circle-${typeKey(type)}`} style={style} type="button" onClick={() => onSelectRun(run)} aria-label={label} title={run.title || label}>{formatDistance(distance, locale)}</button>
  return <span className={`training-calendar-circle training-circle-${typeKey(type)} training-calendar-circle-planned`} style={style} aria-label={label} title={label}>{formatDistance(distance, locale)}</span>
}

function CalendarLegend({ t }: { t: Translate }) {
  return <div className="training-calendar-legend" aria-label={t('training.calendarTitle')}><span><i className="training-legend-swatch training-circle-easy" />{t('training.legendEasy')}</span><span><i className="training-legend-swatch training-circle-quality" />{t('training.legendQuality')}</span><span><i className="training-legend-swatch training-circle-long" />{t('training.legendLong')}</span><span><i className="training-legend-swatch training-circle-run" />{t('training.legendRun')}</span><span><i className="training-legend-swatch training-legend-planned" />{t('training.planned')}</span></div>
}

export function TrainingCalendar({ runs, locale, onSelectRun, period, date, sessions: providedSessions, loading: providedLoading, error: providedError, onRetry: providedRetry, onDateChange, t }: TrainingCalendarProps) {
  const [anchorKey, setAnchorKey] = useState(() => dateKey(date) ?? todayKey())
  const shouldLoad = providedSessions === undefined
  const [resource, setResource] = useState<Resource<CoachingData>>(() => shouldLoad ? { status: 'loading', data: null, error: null } : emptyResource())

  const load = useCallback(async () => {
    if (!shouldLoad) return
    setResource((current) => ({ ...current, status: 'loading', error: null }))
    try {
      setResource({ status: 'success', data: normalizeCoaching(await request<CoachingData>('/api/coaching')), error: null })
    } catch (error) {
      setResource((current) => ({ ...current, status: 'error', error: errorText(error, t) }))
    }
  }, [shouldLoad, t])

  useEffect(() => {
    const next = dateKey(date)
    if (next) setAnchorKey(next)
  }, [date])
  useEffect(() => { if (shouldLoad) void load() }, [load, shouldLoad])

  const anchor = dateFromKey(anchorKey) ?? new Date()
  const sessions = providedSessions ?? resource.data?.sessions ?? []
  const loading = providedLoading ?? (shouldLoad && resource.status !== 'success' && !resource.error)
  const error = providedError ?? (shouldLoad ? resource.error : null)
  const retry = providedRetry ?? (shouldLoad ? () => void load() : undefined)
  const visibleRuns = useMemo(() => realRuns(runs), [runs])
  const actualByDate = useMemo(() => {
    const map = new Map<string, Run[]>()
    visibleRuns.forEach((run) => {
      const key = runDateKey(run)
      if (!key) return
      const list = map.get(key) ?? []
      list.push(run)
      map.set(key, list)
    })
    map.forEach((list) => list.sort((left, right) => String(left.started_at).localeCompare(String(right.started_at))))
    return map
  }, [visibleRuns])
  const actualIds = useMemo(() => new Set(visibleRuns.map((run) => String(run.id))), [visibleRuns])
  const pendingByDate = useMemo(() => {
    const map = new Map<string, TrainingSession[]>()
    sessions.forEach((session) => {
      if (String(session.run_type ?? '').toLowerCase() === 'rest') return
      if (session.completed_run_id != null && actualIds.has(String(session.completed_run_id))) return
      const key = dateKey(session.date)
      if (!key) return
      const list = map.get(key) ?? []
      list.push(session)
      map.set(key, list)
    })
    map.forEach((list) => list.sort((left, right) => String(left.id).localeCompare(String(right.id))))
    return map
  }, [actualIds, sessions])
  const maxDistance = useMemo(() => Math.max(1, ...visibleRuns.map((run) => Number(run.distance_km) || 0), ...sessions.map((session) => Number(session.distance_km) || 0)), [sessions, visibleRuns])
  const dayItems = (key: string) => ({ actual: actualByDate.get(key) ?? [], planned: pendingByDate.get(key) ?? [] })
  const monthDays = useMemo(() => buildMonthDays(anchor), [anchorKey])
  const weekDays = useMemo(() => buildWeekDays(anchor), [anchorKey])
  const monthTitle = period === 'month' ? formatMonth(anchor, locale) : t('training.weekOf', { date: formatDate(weekDays[0]?.date, locale) })
  const displayedKeys = useMemo(() => new Set((period === 'month' ? monthDays.filter((day) => day.inMonth) : weekDays).map((day) => day.key)), [monthDays, period, weekDays])
  const recordedCount = visibleRuns.filter((run) => {
    const key = runDateKey(run)
    return key != null && displayedKeys.has(key)
  }).length
  const plannedCount = Array.from(pendingByDate.entries()).reduce((count, [key, entries]) => count + (displayedKeys.has(key) ? entries.length : 0), 0)

  return (
    <section className="card training-calendar-card">
      <div className="training-card-heading training-calendar-heading"><div><h3>{monthTitle}</h3>{(recordedCount > 0 || plannedCount > 0) && <p>{t('training.calendarSummary', { runs: recordedCount, planned: plannedCount })}</p>}</div></div>
      {loading && <TrainingLoader t={t} compact />}
      {error && !loading && <TrainingError message={error} onRetry={retry} t={t} />}
      {!loading && !error && <>
        <CalendarLegend t={t} />
        {period === 'month' && <div className="training-month-grid" role="grid" aria-label={monthTitle}><div className="training-month-weekdays" role="row">{[0, 1, 2, 3, 4, 5, 6].map((weekday) => <span role="columnheader" key={weekday}>{dayLabel(weekday, t)}</span>)}</div><div className="training-month-cells">{monthDays.map((day) => {
          const items = dayItems(day.key)
          const allItems = [...items.actual.map((run) => t('training.runLabel', { type: typeLabel(run.run_type, t), distance: formatDistance(run.distance_km, locale) })), ...items.planned.map((session) => t('training.plannedLabel', { type: typeLabel(session.run_type, t), distance: formatDistance(session.distance_km, locale) }))]
          const label = t('training.calendarDateLabel', { date: formatDate(day.date, locale), items: allItems.length ? allItems.join(', ') : t('training.noRuns') })
          return <div className={`training-month-day${day.inMonth ? '' : ' training-month-day-muted'}${day.key === todayKey() ? ' training-month-day-today' : ''}`} role="gridcell" aria-label={label} key={day.key}><time dateTime={day.key}>{day.date.getDate()}</time><div className="training-month-circles">{items.actual.map((run) => <CalendarCircle key={`run-${String(run.id)}`} run={run} size={circleSize(run.distance_km, maxDistance)} locale={locale} t={t} onSelectRun={onSelectRun} />)}{items.planned.map((session) => <CalendarCircle key={`session-${String(session.id)}`} session={session} planned size={circleSize(session.distance_km, maxDistance)} locale={locale} t={t} onSelectRun={onSelectRun} />)}</div></div>
        })}</div></div>}
        {period === 'week' && <div className="training-week-grid">{weekDays.map((day) => { const items = dayItems(day.key); return <section className={`training-week-day${day.key === todayKey() ? ' training-week-day-today' : ''}`} key={day.key} aria-label={formatDate(day.date, locale)}><header><span>{dayLabel(mondayIndex(day.date), t)}</span><time dateTime={day.key}>{day.date.getDate()}</time></header><div className="training-week-entries">{items.actual.map((run) => <button className="training-week-entry training-week-entry-actual" type="button" key={`run-${String(run.id)}`} onClick={() => onSelectRun(run)}><TypeChip type={runVisualType(run)} t={t} /><strong>{formatDistance(run.distance_km, locale)} {t('training.distanceUnit')}</strong><small>{runDisplayLabel(run, locale, t)}</small></button>)}{items.planned.map((session) => <div className="training-week-entry training-week-entry-planned" key={`session-${String(session.id)}`}><TypeChip type={session.run_type} t={t} outlined />{String(session.run_type).toLowerCase() !== 'rest' && <strong>{formatDistance(session.distance_km, locale)} {t('training.distanceUnit')}</strong>}<small>{sessionDescription(session, t)}</small></div>)}{items.actual.length === 0 && items.planned.length === 0 && <span className="training-week-empty">·</span>}</div></section> })}</div>}
        {visibleRuns.length === 0 && sessions.length === 0 && <p className="training-calendar-empty">{t('training.calendarEmpty')}</p>}
      </>}
    </section>
  )
}
