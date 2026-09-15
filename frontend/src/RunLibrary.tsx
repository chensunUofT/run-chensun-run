import { useEffect, useMemo, useState } from 'react'
import { localeTag, type Translate } from './i18n'
import type { Run, RunId, Shoe } from './types'

const RUN_TYPE_VALUES = ['easy', 'long', 'tempo', 'interval', 'race'] as const
const BULK_UNCHANGED = '__unchanged__'
const BULK_NO_SHOE = '__no_shoe__'
const MAX_BULK_RUNS = 500

type BulkRunType = typeof BULK_UNCHANGED | (typeof RUN_TYPE_VALUES)[number]
type BulkShoe = typeof BULK_UNCHANGED | typeof BULK_NO_SHOE | string
type BulkPatch = { run_type?: string; shoe_id?: RunId | null }

export type RunLibraryProps = {
  runs: Run[]
  shoes: Shoe[]
  selectedRunId: RunId | null
  onSelectRun: (run: Run) => void
  onBulkUpdate: (ids: RunId[], patch: BulkPatch) => Promise<void>
  locale: 'en' | 'zh'
  t: Translate
}

function idKey(value: RunId) {
  return String(value)
}

function isDemoRun(run: Run) {
  const source = String(run.source ?? '').toLowerCase()
  const title = String(run.title ?? '').toLowerCase()
  return source.includes('demo') || source.includes('sample') || title.includes('demo') || title.includes('sample')
}

function normalizedType(value: string | null | undefined) {
  return String(value ?? '').trim().toLowerCase()
}

// Provider records often use the generic `run` type. Infer a more specific
// type only when the title contains an explicit repeat prescription.
function displayType(run: Run) {
  const type = normalizedType(run.run_type)
  if (type && type !== 'run') return type
  const title = String(run.title ?? '').trim().replace(/\s+/g, ' ')
  return /(?:\d+(?:\.\d+)?|n)\s*[x×]\s*\d+(?:\.\d+)?\s*(?:m|km)\b|\d+(?:\.\d+)?\s*(?:m|km)\s*[x×]\s*(?:\d+(?:\.\d+)?|n)\b/i.test(title)
    ? 'interval'
    : 'run'
}

function translatedType(type: string, t: Translate) {
  const key = normalizedType(type) || 'run'
  const translated = t(`types.${key}`)
  return translated === `types.${key}` ? (key === 'run' ? t('types.run') : key) : translated
}

function displayTitle(run: Run, type: string, t: Translate) {
  const title = String(run.title ?? '').trim()
  if (title && !/^(run|running|google health run|treadmill run)$/i.test(title)) return title
  return translatedType(type, t)
}

function markerType(type: string) {
  const normalized = normalizedType(type)
  return RUN_TYPE_VALUES.includes(normalized as (typeof RUN_TYPE_VALUES)[number]) ? normalized : 'run'
}

function dateValue(value: string) {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

function localDateValue(value: string | null | undefined) {
  if (!value) return null
  const trimmed = value.trim()
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed)
  if (match) {
    const year = Number(match[1])
    const month = Number(match[2])
    const day = Number(match[3])
    const parsed = new Date(year, month - 1, day)
    if (parsed.getFullYear() === year && parsed.getMonth() === month - 1 && parsed.getDate() === day) return parsed
    return null
  }
  return dateValue(trimmed)
}

function runLocalDateValue(run: Pick<Run, 'started_at' | 'local_date'>) {
  return localDateValue(run.local_date) ?? dateValue(run.started_at)
}

function formatRunDate(run: Pick<Run, 'started_at' | 'local_date'>, locale: 'en' | 'zh') {
  const parsed = runLocalDateValue(run)
  if (!parsed) return String(run.local_date ?? run.started_at ?? '').slice(0, 10) || '—'
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`
}

function formatDistance(value: number | null | undefined, locale: 'en' | 'zh') {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value.toLocaleString(localeTag(locale), { maximumFractionDigits: 1 })} km`
}

function formatPace(value: number | null | undefined, t: Translate) {
  if (value == null || !Number.isFinite(value) || value <= 0) return '—'
  const rounded = Math.max(0, Math.round(value))
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')} ${t('time.perKm')}`
}

function shoeLabel(shoe: Shoe | undefined, t: Translate) {
  if (!shoe) return t('library.noShoe')
  const name = String(shoe.name ?? '').trim() || t('shoes.nameUnset')
  const brand = String(shoe.brand ?? '').trim()
  return brand ? `${brand} · ${name}` : name
}

function updateError(error: unknown, t: Translate) {
  if (error instanceof Error && error.message && !/^Request failed \(/i.test(error.message)) return error.message
  return t('library.applyError')
}

export function RunLibrary({ runs, shoes, selectedRunId, onSelectRun, onBulkUpdate, locale, t }: RunLibraryProps) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set())
  const [bulkType, setBulkType] = useState<BulkRunType>(BULK_UNCHANGED)
  const [bulkShoe, setBulkShoe] = useState<BulkShoe>(BULK_UNCHANGED)
  const [submitting, setSubmitting] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)

  const shoeById = useMemo(() => new Map(shoes.map((shoe) => [idKey(shoe.id), shoe])), [shoes])
  const filteredRuns = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase(localeTag(locale))
    return runs
      .filter((run) => !isDemoRun(run))
      .filter((run) => {
        const type = displayType(run)
        if (filter !== 'all' && normalizedType(type) !== filter) return false
        if (!normalizedQuery) return true
        const shoe = run.shoe_id == null ? undefined : shoeById.get(idKey(run.shoe_id))
        const searchText = [
          run.title,
          run.notes,
          run.distance_km,
          translatedType(type, t),
          type,
          shoe?.name,
          shoe?.brand,
        ]
          .filter((value) => value != null)
          .join(' ')
          .toLocaleLowerCase(localeTag(locale))
        return searchText.includes(normalizedQuery)
      })
      .sort((left, right) => {
        const leftTime = dateValue(left.started_at)?.getTime() ?? 0
        const rightTime = dateValue(right.started_at)?.getTime() ?? 0
        if (rightTime !== leftTime) return rightTime - leftTime
        return idKey(right.id).localeCompare(idKey(left.id), undefined, { numeric: true })
      })
  }, [filter, locale, query, runs, shoeById, t])

  const selectableRuns = useMemo(() => filteredRuns.slice(0, MAX_BULK_RUNS), [filteredRuns])
  const filteredKeys = useMemo(() => new Set(filteredRuns.map((run) => idKey(run.id))), [filteredRuns])
  const selectedRuns = useMemo(() => filteredRuns.filter((run) => selectedKeys.has(idKey(run.id))), [filteredRuns, selectedKeys])
  const selectedVisibleCount = selectableRuns.reduce((count, run) => count + (selectedKeys.has(idKey(run.id)) ? 1 : 0), 0)
  const allVisibleSelected = selectableRuns.length > 0 && selectedVisibleCount === selectableRuns.length
  const hasSelection = selectedRuns.length > 0
  const canApply = hasSelection && (bulkType !== BULK_UNCHANGED || bulkShoe !== BULK_UNCHANGED) && !submitting

  // A new query or type scope must never leave an old, hidden selection armed.
  useEffect(() => {
    setSelectedKeys(new Set())
    setApplyError(null)
    setBulkType(BULK_UNCHANGED)
    setBulkShoe(BULK_UNCHANGED)
  }, [filter, query])

  useEffect(() => {
    setSelectedKeys((current) => {
      const next = new Set([...current].filter((key) => filteredKeys.has(key)))
      return next.size === current.size ? current : next
    })
  }, [filteredKeys])

  const toggleRun = (run: Run) => {
    if (submitting) return
    const key = idKey(run.id)
    setSelectedKeys((current) => {
      const next = new Set(current)
      if (next.has(key)) next.delete(key)
      else if (next.size < MAX_BULK_RUNS) next.add(key)
      return next
    })
  }

  const selectAllFiltered = () => {
    if (submitting) return
    setSelectedKeys(allVisibleSelected ? new Set() : new Set(selectableRuns.map((run) => idKey(run.id))))
  }

  const clearSelection = () => {
    if (submitting) return
    setSelectedKeys(new Set())
    setApplyError(null)
  }

  const applyBulkUpdate = async () => {
    if (!canApply) return
    const patch: BulkPatch = {}
    if (bulkType !== BULK_UNCHANGED) patch.run_type = bulkType
    if (bulkShoe !== BULK_UNCHANGED) {
      patch.shoe_id = bulkShoe === BULK_NO_SHOE ? null : (shoeById.get(bulkShoe)?.id ?? bulkShoe)
    }
    setSubmitting(true)
    setApplyError(null)
    try {
      await onBulkUpdate(selectedRuns.map((run) => run.id), patch)
      setSelectedKeys(new Set())
      setBulkType(BULK_UNCHANGED)
      setBulkShoe(BULK_UNCHANGED)
    } catch (error) {
      // The parent callback is atomic; retain every selected row so the user
      // can correct the patch and retry without rebuilding the selection.
      setApplyError(updateError(error, t))
    } finally {
      setSubmitting(false)
    }
  }

  const hasFilters = Boolean(query.trim()) || filter !== 'all'
  return (
    <div className="run-library card">
      <div className="run-library-toolbar">
        <label className="run-library-search">
          <span className="run-library-sr-only">{t('library.searchLabel')}</span>
          <input
            aria-label={t('library.searchLabel')}
            placeholder={t('library.searchPlaceholder')}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            disabled={submitting}
          />
        </label>
        <label className="run-library-filter">
          <span>{t('library.filterLabel')}</span>
          <select aria-label={t('library.filterLabel')} value={filter} onChange={(event) => setFilter(event.target.value)} disabled={submitting}>
            <option value="all">{t('library.allTypes')}</option>
            {RUN_TYPE_VALUES.map((type) => <option value={type} key={type}>{translatedType(type, t)}</option>)}
          </select>
        </label>
        <div className="run-library-select-actions">
          <span className="run-library-selection-status" aria-live="polite">
            {hasSelection ? t('library.selectedCount', { count: selectedRuns.length }) : t('library.selectionHint')}
          </span>
          <button className="button button-secondary" type="button" onClick={selectAllFiltered} disabled={submitting || selectableRuns.length === 0}>
            {allVisibleSelected ? t('library.clearVisible') : t('library.selectAllFiltered')}
          </button>
          <button className="run-library-clear-button" type="button" onClick={clearSelection} disabled={submitting || !hasSelection}>{t('library.clearSelection')}</button>
        </div>
      </div>

      {filteredRuns.length > MAX_BULK_RUNS && <p className="run-library-limit-note">{t('library.selectionLimit', { count: MAX_BULK_RUNS })}</p>}

      {hasSelection && <section className="run-library-bulkbar" aria-label={t('library.bulkActions')}>
        <div className="run-library-bulk-summary">
          <strong>{t('library.selectedCount', { count: selectedRuns.length })}</strong>
          <span>{t('library.bulkSummary')}</span>
        </div>
        <label className="run-library-bulk-field">
          <span>{t('library.runType')}</span>
          <select value={bulkType} onChange={(event) => setBulkType(event.target.value as BulkRunType)} disabled={submitting}>
            <option value={BULK_UNCHANGED}>{t('library.keepUnchanged')}</option>
            {RUN_TYPE_VALUES.map((type) => <option value={type} key={type}>{translatedType(type, t)}</option>)}
          </select>
        </label>
        <label className="run-library-bulk-field">
          <span>{t('library.shoe')}</span>
          <select value={bulkShoe} onChange={(event) => setBulkShoe(event.target.value)} disabled={submitting}>
            <option value={BULK_UNCHANGED}>{t('library.keepUnchanged')}</option>
            <option value={BULK_NO_SHOE}>{t('library.noShoe')}</option>
            {shoes.map((shoe) => <option value={idKey(shoe.id)} key={idKey(shoe.id)}>{shoeLabel(shoe, t)}</option>)}
          </select>
        </label>
        <button className="button button-primary run-library-apply-button" type="button" onClick={() => void applyBulkUpdate()} disabled={!canApply}>
          {submitting ? t('library.applying') : t('library.applyToSelected')}
        </button>
        {applyError && <p className="run-library-apply-error" role="alert">{applyError}</p>}
      </section>}

      <div className="run-library-columns" aria-hidden="true">
        <span />
        <span>{t('library.run')}</span>
        <span>{t('library.distancePace')}</span>
        <span>{t('library.shoe')}</span>
      </div>
      <div className="run-library-list" role="list">
        {filteredRuns.map((run) => {
          const type = displayType(run)
          const key = idKey(run.id)
          const shoe = run.shoe_id == null ? undefined : shoeById.get(idKey(run.shoe_id))
          const selected = selectedKeys.has(key)
          const current = String(selectedRunId) === key
          const pace = run.distance_km > 0 ? (run.moving_seconds ?? run.duration_seconds) / run.distance_km : null
          return (
            <article className={`run-library-row${current ? ' run-library-row-current' : ''}${selected ? ' run-library-row-selected' : ''}`} key={key} role="listitem">
              <label className="run-library-checkbox">
                <input type="checkbox" checked={selected} onChange={() => toggleRun(run)} disabled={submitting} aria-label={t('library.selectRun', { title: displayTitle(run, type, t) })} />
                <span aria-hidden="true" />
              </label>
              <button className="run-library-row-button" type="button" onClick={() => onSelectRun(run)} aria-current={current ? 'true' : undefined}>
                <span className={`run-library-type-marker run-library-marker-${markerType(type)}`} aria-hidden="true" />
                <span className="run-library-row-copy">
                  <strong>{translatedType(type, t)}</strong>
                  <small>{formatRunDate(run, locale)}</small>
                </span>
              </button>
              <div className="run-library-metrics">
                <strong>{formatDistance(run.distance_km, locale)}</strong>
                <small>{formatPace(pace, t)}</small>
              </div>
              <div className={`run-library-shoe${shoe ? '' : ' run-library-shoe-empty'}`} title={shoe ? shoeLabel(shoe, t) : undefined}>{shoe ? shoeLabel(shoe, t) : t('library.noShoe')}</div>
            </article>
          )
        })}
        {filteredRuns.length === 0 && <div className="run-library-empty">
          <span className="run-library-empty-mark" aria-hidden="true" />
          <strong>{hasFilters ? t('library.noMatch') : t('library.noRuns')}</strong>
          <p>{hasFilters ? t('library.noMatchCopy') : t('library.emptyCopy')}</p>
        </div>}
      </div>
    </div>
  )
}
