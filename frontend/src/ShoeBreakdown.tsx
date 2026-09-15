import { useMemo, useState } from 'react'
import type { Locale, Translate } from './i18n'
import type { Run, RunId, Shoe } from './types'
import './shoe-breakdown.css'

export type ShoeBreakdownProps = {
  shoe: Shoe
  runs: Run[]
  locale: Locale
  t: Translate
}

type MonthBucket = {
  key: string
  label: string
  distanceKm: number
  runCount: number
  runs: Run[]
}

type MonthSummary = Omit<MonthBucket, 'runs'> & {
  types: TypeBucket[]
}

type TypeBucket = {
  key: string
  label: string
  distanceKm: number
  runCount: number
}

const typeTones = ['green', 'amber', 'blue', 'purple', 'red'] as const

function localeTag(locale: Locale) {
  return locale === 'zh' ? 'zh-CN' : 'en-US'
}

function numericDistance(run: Run) {
  const distance = Number(run.distance_km)
  return Number.isFinite(distance) ? distance : 0
}

/**
 * Parse a run date in the user's local calendar. Date-only ISO values need an
 * explicit local construction because `new Date('YYYY-MM-DD')` is UTC based.
 */
function localDate(value: string | null | undefined) {
  if (!value) return null
  const trimmed = value.trim()
  const dateOnly = trimmed.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (dateOnly) {
    const year = Number(dateOnly[1])
    const month = Number(dateOnly[2])
    const day = Number(dateOnly[3])
    const date = new Date(year, month - 1, day, 12)
    if (date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day) return date
    return null
  }
  const parsed = new Date(trimmed)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function runLocalDate(run: Pick<Run, 'started_at' | 'local_date'>) {
  return localDate(run.local_date) ?? localDate(run.started_at)
}

function monthKey(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`
}

function monthLabel(key: string, locale: Locale) {
  return key
}

function typeKey(value: string | null | undefined) {
  const normalized = (value ?? '').trim().toLowerCase().replace(/[\s-]+/g, '_')
  return normalized || 'run'
}

function fallbackTypeLabel(key: string) {
  return key
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function trainingTypeLabel(key: string, t: Translate) {
  const translated = t(`types.${key}`)
  return translated === `types.${key}` ? fallbackTypeLabel(key) : translated
}

function runCountLabel(count: number, t: Translate) {
  return count === 1 ? t('shoeBreakdown.oneRun') : t('shoeBreakdown.manyRuns', { count })
}

function monthCountLabel(count: number, t: Translate) {
  return count === 1 ? t('shoeBreakdown.oneMonth') : t('shoeBreakdown.manyMonths', { count })
}

function formatDistance(distanceKm: number, locale: Locale) {
  return `${distanceKm.toLocaleString(localeTag(locale), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} km`
}

function sameId(left: RunId | null | undefined, right: RunId) {
  return left != null && String(left) === String(right)
}

function typeTone(key: string) {
  if (key === 'easy') return 'green'
  if (key === 'long') return 'amber'
  if (key === 'tempo') return 'blue'
  if (key === 'interval') return 'purple'
  if (key === 'race') return 'red'
  let hash = 0
  for (const character of key) hash = (hash * 31 + character.charCodeAt(0)) | 0
  return typeTones[Math.abs(hash) % typeTones.length]
}

function buildTypeBuckets(source: Run[], t: Translate) {
  const typeMap = new Map<string, TypeBucket>()
  source.forEach((run) => {
    const key = typeKey(run.run_type)
    const current = typeMap.get(key) ?? { key, label: trainingTypeLabel(key, t), distanceKm: 0, runCount: 0 }
    current.distanceKm += numericDistance(run)
    current.runCount += 1
    typeMap.set(key, current)
  })
  return Array.from(typeMap.values()).sort((left, right) => right.distanceKm - left.distanceKm || right.runCount - left.runCount || left.label.localeCompare(right.label))
}

export function buildShoeBreakdown(shoe: Shoe, runs: Run[], locale: Locale, t: Translate) {
  const shoeRuns = runs.filter((run) => sameId(run.shoe_id, shoe.id))

  const monthMap = new Map<string, MonthBucket>()
  shoeRuns.forEach((run) => {
    const date = runLocalDate(run)
    if (!date) return
    const key = monthKey(date)
    const current = monthMap.get(key) ?? { key, label: monthLabel(key, locale), distanceKm: 0, runCount: 0, runs: [] }
    current.distanceKm += numericDistance(run)
    current.runCount += 1
    current.runs.push(run)
    monthMap.set(key, current)
  })

  const months: MonthSummary[] = Array.from(monthMap.values())
    .map(({ runs: monthRuns, ...bucket }) => ({ ...bucket, types: buildTypeBuckets(monthRuns, t) }))
    .sort((left, right) => right.key.localeCompare(left.key))

  const types = buildTypeBuckets(shoeRuns, t)

  return { shoeRuns, months, types }
}

export function ShoeBreakdown({ shoe, runs, locale, t }: ShoeBreakdownProps) {
  const [expandedMonth, setExpandedMonth] = useState<string | null>(null)
  const { shoeRuns, months, types } = useMemo(() => buildShoeBreakdown(shoe, runs, locale, t), [locale, runs, shoe, t])
  const maxMonthDistance = Math.max(...months.map((month) => month.distanceKm), 0)
  const maxTypeDistance = Math.max(...types.map((type) => type.distanceKm), 0)
  const headingId = `shoe-breakdown-${String(shoe.id).replace(/[^a-zA-Z0-9_-]/g, '-')}`

  return (
    <section className="shoe-breakdown" aria-labelledby={headingId}>
      <div className="shoe-breakdown-heading">
        <div>
          <span className="section-eyebrow">{t('shoeBreakdown.eyebrow')}</span>
          <h4 id={headingId}>{t('shoeBreakdown.title')}</h4>
        </div>
        <span className="shoe-breakdown-record-count">{runCountLabel(shoeRuns.length, t)}</span>
      </div>

      {shoeRuns.length === 0 ? (
        <p className="shoe-breakdown-empty">{t('shoeBreakdown.noRecordedRuns')}</p>
      ) : (
        <div className="shoe-breakdown-grid">
          <section className="shoe-breakdown-panel" aria-labelledby={`${headingId}-months`}>
            <div className="shoe-breakdown-panel-heading">
              <div>
                <h5 id={`${headingId}-months`}>{t('shoeBreakdown.mileageByMonth')}</h5>
                <span>{months.length ? monthCountLabel(months.length, t) : t('shoeBreakdown.noMonthlyData')}</span>
              </div>
              <span className="shoe-breakdown-unit">km</span>
            </div>
            {months.length > 0 ? (
              <div className="shoe-breakdown-month-list">
                {months.map((month) => {
                  const isExpanded = expandedMonth === month.key
                  const width = maxMonthDistance > 0 ? (month.distanceKm / maxMonthDistance) * 100 : 0
                  return (
                    <div className="shoe-breakdown-month" key={month.key}>
                      <button
                        className="shoe-breakdown-month-trigger"
                        type="button"
                        aria-expanded={isExpanded}
                        onClick={() => setExpandedMonth(isExpanded ? null : month.key)}
                      >
                        <span className="shoe-breakdown-month-label">
                          <time dateTime={`${month.key}-01`}>{month.label}</time>
                          <small>{runCountLabel(month.runCount, t)}</small>
                        </span>
                        <span className="shoe-breakdown-bar" aria-hidden="true"><span style={{ width: `${width}%` }} /></span>
                        <strong>{formatDistance(month.distanceKm, locale)}</strong>
                        <span className="shoe-breakdown-chevron" aria-hidden="true">{isExpanded ? '−' : '+'}</span>
                      </button>
                      {isExpanded && (
                        <div className="shoe-breakdown-month-type-list">
                          {month.types.map((type) => (
                            <div className="shoe-breakdown-month-type" key={type.key}>
                              <span className={`shoe-breakdown-type-name shoe-breakdown-tone-${typeTone(type.key)}`}><span />{type.label}</span>
                              <small>{runCountLabel(type.runCount, t)}</small>
                              <strong>{formatDistance(type.distanceKm, locale)}</strong>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="shoe-breakdown-no-months">{t('shoeBreakdown.noMonthlyData')}</p>
            )}
            <p className="shoe-breakdown-note">{t('shoeBreakdown.recordedOnly')}</p>
          </section>

          <section className="shoe-breakdown-panel" aria-labelledby={`${headingId}-types`}>
            <div className="shoe-breakdown-panel-heading">
              <div>
                <h5 id={`${headingId}-types`}>{t('shoeBreakdown.mileageByType')}</h5>
                <span>{t('shoeBreakdown.typeSummary')}</span>
              </div>
              <span className="shoe-breakdown-unit">km</span>
            </div>
            <div className="shoe-breakdown-type-list">
              {types.map((type) => {
                const width = maxTypeDistance > 0 ? (type.distanceKm / maxTypeDistance) * 100 : 0
                return (
                  <div className="shoe-breakdown-type" key={type.key}>
                    <div className="shoe-breakdown-type-topline">
                      <span className={`shoe-breakdown-type-name shoe-breakdown-tone-${typeTone(type.key)}`}><span />{type.label}</span>
                      <strong>{formatDistance(type.distanceKm, locale)}</strong>
                    </div>
                    <div className="shoe-breakdown-bar shoe-breakdown-type-bar" aria-hidden="true"><span style={{ width: `${width}%` }} /></div>
                    <small>{runCountLabel(type.runCount, t)}</small>
                  </div>
                )
              })}
            </div>
          </section>
        </div>
      )}
    </section>
  )
}
