import { useEffect, useMemo, useState } from 'react'
import { Icon } from './icons'
import { type Locale, type Translate } from './i18n'
import './stats-period-picker.css'

export type StatsPeriod = 'week' | 'month'

export type StatsPeriodPickerProps = {
  period: StatsPeriod
  date: string
  onPeriodChange: (period: StatsPeriod) => void
  onDateChange: (date: string) => void
  locale: Locale
  t: Translate
}

type IsoWeekParts = {
  weekYear: number
  week: number
}

const DAY_MS = 86_400_000

function cloneLocalDate(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), 12)
}

export function parseLocalDate(value: string | undefined | null): Date | null {
  if (!value) return null
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (dateOnly) {
    const year = Number(dateOnly[1])
    const month = Number(dateOnly[2])
    const day = Number(dateOnly[3])
    const date = new Date(year, month - 1, day, 12)
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null
    return date
  }
  const instant = new Date(value)
  if (Number.isNaN(instant.getTime())) return null
  return cloneLocalDate(instant)
}

export function localDateKey(date: Date) {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function addLocalDays(date: Date, days: number) {
  const result = cloneLocalDate(date)
  result.setDate(result.getDate() + days)
  return result
}

function mondayOfWeek(date: Date) {
  return addLocalDays(date, -((date.getDay() + 6) % 7))
}

function monthStart(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), 1)
}

export function getIsoWeekParts(value: Date): IsoWeekParts {
  const local = cloneLocalDate(value)
  const utcValue = Date.UTC(local.getFullYear(), local.getMonth(), local.getDate())
  const isoWeekday = new Date(utcValue).getUTCDay() || 7
  const thursdayUtc = utcValue + (4 - isoWeekday) * DAY_MS
  const thursday = new Date(thursdayUtc)
  const weekYear = thursday.getUTCFullYear()
  const firstThursday = Date.UTC(weekYear, 0, 4)
  const week = 1 + Math.round((thursdayUtc - firstThursday) / (7 * DAY_MS))
  return { weekYear, week }
}

export function isoWeekValue(value: Date) {
  const { weekYear, week } = getIsoWeekParts(value)
  return `${String(weekYear).padStart(4, '0')}-W${String(week).padStart(2, '0')}`
}

export function dateFromIsoWeek(value: string): Date | null {
  const match = /^(\d{4})-W(\d{2})$/.exec(value)
  if (!match) return null
  const weekYear = Number(match[1])
  const week = Number(match[2])
  if (week < 1 || week > 53) return null
  const januaryFourth = new Date(weekYear, 0, 4)
  const monday = addLocalDays(januaryFourth, -((januaryFourth.getDay() + 6) % 7) + (week - 1) * 7)
  const parts = getIsoWeekParts(monday)
  if (parts.weekYear !== weekYear || parts.week !== week) return null
  return monday
}

export function monthValue(value: Date) {
  return `${String(value.getFullYear()).padStart(4, '0')}-${String(value.getMonth() + 1).padStart(2, '0')}`
}

export function dateFromMonth(value: string): Date | null {
  const match = /^(\d{4})-(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  if (month < 1 || month > 12) return null
  return new Date(year, month - 1, 1)
}

export function periodInputValue(period: StatsPeriod, date: string) {
  const value = parseLocalDate(date) ?? new Date()
  return period === 'week' ? isoWeekValue(value) : monthValue(value)
}

export function dateFromPeriodInput(period: StatsPeriod, value: string) {
  return period === 'week' ? dateFromIsoWeek(value) : dateFromMonth(value)
}

export function shiftPeriodDate(period: StatsPeriod, date: string, amount: number) {
  const value = parseLocalDate(date) ?? new Date()
  const shifted = period === 'week'
    ? addLocalDays(value, amount * 7)
    : new Date(value.getFullYear(), value.getMonth() + amount, 1)
  return localDateKey(shifted)
}

function periodCaption(period: StatsPeriod, date: string, _locale: Locale, t: Translate) {
  const value = parseLocalDate(date) ?? new Date()
  if (period === 'month') {
    return t('statsPeriodPicker.monthRange', { date: monthValue(monthStart(value)) })
  }
  const start = mondayOfWeek(value)
  const end = addLocalDays(start, 6)
  const startText = localDateKey(start)
  const endText = localDateKey(end)
  return t('statsPeriodPicker.weekRange', { date: `${startText} – ${endText}` })
}

export function StatsPeriodPicker({ period, date, onPeriodChange, onDateChange, locale, t }: StatsPeriodPickerProps) {
  const [invalidInput, setInvalidInput] = useState<StatsPeriod | null>(null)
  const inputValue = useMemo(() => periodInputValue(period, date), [date, period])
  const caption = useMemo(() => periodCaption(period, date, locale, t), [date, locale, period, t])

  useEffect(() => {
    setInvalidInput(null)
  }, [date, period])

  const choosePeriod = (next: StatsPeriod) => {
    setInvalidInput(null)
    onPeriodChange(next)
  }

  const chooseInput = (value: string) => {
    const nextDate = dateFromPeriodInput(period, value)
    if (!nextDate) {
      setInvalidInput(period)
      return
    }
    setInvalidInput(null)
    onDateChange(localDateKey(nextDate))
  }

  const move = (amount: number) => {
    setInvalidInput(null)
    onDateChange(shiftPeriodDate(period, date, amount))
  }

  const inputLabel = period === 'week' ? t('statsPeriodPicker.chooseWeek') : t('statsPeriodPicker.chooseMonth')
  const invalidLabel = invalidInput === 'week' ? t('statsPeriodPicker.invalidWeek') : t('statsPeriodPicker.invalidMonth')

  return <section className="stats-period-picker" aria-label={t('statsPeriodPicker.period')}>
    <div className="stats-period-switch" role="group" aria-label={t('statsPeriodPicker.period')}>
      <button type="button" className={period === 'week' ? 'stats-period-active' : ''} aria-pressed={period === 'week'} onClick={() => choosePeriod('week')}>{t('statsPeriodPicker.week')}</button>
      <button type="button" className={period === 'month' ? 'stats-period-active' : ''} aria-pressed={period === 'month'} onClick={() => choosePeriod('month')}>{t('statsPeriodPicker.month')}</button>
    </div>
    <div className="stats-period-select-row">
      <button className="stats-period-arrow" type="button" aria-label={t('statsPeriodPicker.previous')} onClick={() => move(-1)}><Icon name="chevron-left" size={17} /></button>
      <label className="stats-period-input-label">
        <span>{inputLabel}</span>
        <input type={period === 'week' ? 'week' : 'month'} value={inputValue} aria-label={inputLabel} onChange={(event) => chooseInput(event.target.value)} />
      </label>
      <button className="stats-period-arrow" type="button" aria-label={t('statsPeriodPicker.next')} onClick={() => move(1)}><Icon name="chevron-right" size={17} /></button>
    </div>
    <div className="stats-period-caption" aria-live="polite"><Icon name="calendar" size={15} /><span>{caption}</span></div>
    {invalidInput && <span className="stats-period-error" role="alert">{invalidLabel}</span>}
  </section>
}
