import { useEffect, useMemo, useState } from 'react'
import { Icon } from './icons'
import { localeTag, type Locale, type Translate } from './i18n'
import { request } from './lib/api'
import type { Run, RunId, ShareKind } from './types'
import './share-image.css'

export type ShareImageTarget = {
  kind: ShareKind
  runId?: RunId
  date?: string
}

export type ShareImageProps = {
  target: ShareImageTarget
  runs: Run[]
  locale: Locale
  t: Translate
}

type Weather = {
  status: string
  reason?: string
  temperature_c?: number | null
  humidity_percent?: number | null
  weather_code?: number | null
}

type WeatherState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; data: Weather }
  | { status: 'error' }

type LocalDate = {
  date: Date
  key: string
}

type PeriodBucket = {
  date: Date
  key: string
  distance: number
  count: number
}

type TypeCount = {
  type: string
  count: number
}

type RunModel = {
  kind: 'run'
  run: Run
  weather: Weather | null
}

type PeriodModel = {
  kind: 'week' | 'month'
  start: Date
  end: Date
  runs: Run[]
  totalDistance: number
  totalDuration: number
  runCount: number
  runningDays: number
  averagePace: number | null
  typeCounts: TypeCount[]
  buckets: PeriodBucket[]
}

type EmptyModel = { kind: 'empty' }
type CardModel = RunModel | PeriodModel | EmptyModel

const COLORS = {
  paper: '#f5f3ec',
  white: '#fffdf8',
  forest: '#1f5037',
  forestDeep: '#163b2b',
  mint: '#dbeadf',
  mintStrong: '#88b596',
  ink: '#203229',
  muted: '#718077',
  line: '#dfe6dd',
  amber: '#ca9250',
  amberSoft: '#f8ead6',
  blue: '#7093a3',
  blueSoft: '#e7f0f3',
  purple: '#8b78a6',
  purpleSoft: '#eeeaf4',
}

const FONT = 'Manrope, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif'
const MONO_FONT = '"DM Mono", "SFMono-Regular", Consolas, monospace'
const CANVAS_WIDTH = 1200

function isSyntheticRun(run: Partial<Run>) {
  return /demo|sample/i.test(String(run.source ?? ''))
}

function parseDate(value: string | undefined | null): Date | null {
  if (!value) return null
  const dateOnly = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(value)
  if (dateOnly) {
    const year = Number(dateOnly[1])
    const month = Number(dateOnly[2])
    const day = Number(dateOnly[3])
    const parsed = new Date(year, month - 1, day, 12)
    return parsed.getFullYear() === year && parsed.getMonth() === month - 1 && parsed.getDate() === day ? parsed : null
  }
  const instant = new Date(value)
  if (Number.isNaN(instant.getTime())) return null
  return new Date(instant.getFullYear(), instant.getMonth(), instant.getDate())
}

function runLocalDate(run: Pick<Run, 'started_at' | 'local_date'>) {
  return parseDate(run.local_date) ?? parseDate(run.started_at)
}

function dateKey(date: Date) {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function copyDate(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

function addDays(date: Date, days: number) {
  const result = copyDate(date)
  result.setDate(result.getDate() + days)
  return result
}

function startOfWeek(date: Date) {
  const mondayOffset = (date.getDay() + 6) % 7
  return addDays(date, -mondayOffset)
}

function startOfMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth(), 1)
}

function endOfMonth(date: Date) {
  return new Date(date.getFullYear(), date.getMonth() + 1, 0)
}

function isWithin(date: Date, start: Date, end: Date) {
  return date.getTime() >= start.getTime() && date.getTime() <= end.getTime()
}

function exportableRuns(runs: Run[]) {
  return runs.filter((run) => !isSyntheticRun(run) && Boolean(runLocalDate(run)))
}

function latestRun(runs: Run[]) {
  return [...exportableRuns(runs)].sort((first, second) => {
    const firstTime = new Date(first.started_at).getTime()
    const secondTime = new Date(second.started_at).getTime()
    if (secondTime !== firstTime) return secondTime - firstTime
    return String(second.id).localeCompare(String(first.id))
  })[0] ?? null
}

function formatNumber(value: number | null | undefined, locale: Locale, digits = 1) {
  if (value == null || Number.isNaN(value)) return '—'
  return value.toLocaleString(localeTag(locale), { maximumFractionDigits: digits, minimumFractionDigits: digits })
}

function formatDistance(value: number | null | undefined, locale: Locale) {
  return value == null || Number.isNaN(value) ? '—' : `${formatNumber(value, locale)} km`
}

function formatClock(seconds: number | null | undefined) {
  if (seconds == null || Number.isNaN(seconds)) return '—'
  const rounded = Math.max(0, Math.round(seconds))
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`
}

function formatDuration(seconds: number | null | undefined, locale: Locale) {
  if (seconds == null || Number.isNaN(seconds)) return '—'
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const remainder = rounded % 60
  if (hours > 0) return locale === 'zh' ? `${hours}时 ${String(minutes).padStart(2, '0')}分` : `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return locale === 'zh' ? `${minutes}分 ${String(remainder).padStart(2, '0')}秒` : `${minutes}m ${String(remainder).padStart(2, '0')}s`
  return locale === 'zh' ? `${remainder}秒` : `${remainder}s`
}

function formatPace(seconds: number | null | undefined, t: Translate) {
  if (seconds == null || Number.isNaN(seconds) || seconds <= 0) return '—'
  return `${formatClock(seconds)} ${t('time.perKm')}`
}

function formatDate(date: Date, _locale: Locale) {
  return dateKey(date)
}

function typeKey(value: string | null | undefined) {
  const normalized = String(value ?? '').trim().toLowerCase()
  return normalized || 'run'
}

function typeLabel(type: string, t: Translate) {
  const key = typeKey(type)
  const translated = t(`types.${key}`)
  return translated === `types.${key}` ? key.replace(/[-_]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) : translated
}

function condition(code: number | null | undefined) {
  if (code == null) return 'unknown'
  if (code === 0) return 'clear'
  if (code <= 3) return 'cloudy'
  if (code <= 48) return 'fog'
  if (code <= 67 || (code >= 80 && code <= 82)) return 'rain'
  if (code <= 77 || code === 85 || code === 86) return 'snow'
  return 'storm'
}

function weatherLabel(weather: Weather | null, t: Translate) {
  const kind = condition(weather?.weather_code)
  return t(`weather.${kind}`)
}

function weatherTemperature(weather: Weather | null) {
  return weather?.status === 'available' && weather.temperature_c != null ? `${Math.round(weather.temperature_c)}°` : '—'
}

function weatherHumidity(weather: Weather | null) {
  return weather?.status === 'available' && weather.humidity_percent != null ? `${Math.round(weather.humidity_percent)}%` : '—'
}

function buildBuckets(kind: 'week' | 'month', start: Date, end: Date, runs: Run[]) {
  const first = kind === 'week' ? start : startOfWeek(start)
  const buckets: PeriodBucket[] = []
  let cursor = copyDate(first)
  while (cursor.getTime() <= end.getTime()) {
    buckets.push({ date: copyDate(cursor), key: dateKey(cursor), distance: 0, count: 0 })
    cursor = addDays(cursor, kind === 'week' ? 1 : 7)
  }
  const bucketByKey = new Map(buckets.map((bucket) => [bucket.key, bucket]))
  for (const run of runs) {
    const runDate = runLocalDate(run)
    if (!runDate) continue
    const bucketDate = kind === 'week' ? runDate : startOfWeek(runDate)
    const bucket = bucketByKey.get(dateKey(bucketDate))
    if (!bucket) continue
    bucket.distance += Number(run.distance_km) || 0
    bucket.count += 1
  }
  return buckets
}

function buildTypeCounts(runs: Run[]) {
  const counts = new Map<string, number>()
  for (const run of runs) {
    const key = typeKey(run.run_type)
    counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  return [...counts.entries()]
    .map(([type, count]) => ({ type, count }))
    .sort((first, second) => second.count - first.count || first.type.localeCompare(second.type))
}

function createPeriodModel(kind: 'week' | 'month', anchorDate: Date, runs: Run[]): PeriodModel {
  const start = kind === 'week' ? startOfWeek(anchorDate) : startOfMonth(anchorDate)
  const end = kind === 'week' ? addDays(start, 6) : endOfMonth(anchorDate)
  const selectedRuns = exportableRuns(runs).filter((run) => {
    const runDate = runLocalDate(run)
    return runDate ? isWithin(runDate, start, end) : false
  })
  const totalDistance = selectedRuns.reduce((sum, run) => sum + (Number(run.distance_km) || 0), 0)
  const totalDuration = selectedRuns.reduce((sum, run) => sum + (Number(run.moving_seconds ?? run.duration_seconds) || 0), 0)
  const days = new Set(selectedRuns.map((run) => runLocalDate(run)).filter((date): date is Date => Boolean(date)).map(dateKey))
  return {
    kind,
    start,
    end,
    runs: selectedRuns,
    totalDistance,
    totalDuration,
    runCount: selectedRuns.length,
    runningDays: days.size,
    averagePace: totalDistance > 0 ? totalDuration / totalDistance : null,
    typeCounts: buildTypeCounts(selectedRuns),
    buckets: buildBuckets(kind, start, end, selectedRuns),
  }
}

function roundRect(context: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, radius: number, fill: string, stroke?: string) {
  const r = Math.min(radius, width / 2, height / 2)
  context.beginPath()
  context.moveTo(x + r, y)
  context.arcTo(x + width, y, x + width, y + height, r)
  context.arcTo(x + width, y + height, x, y + height, r)
  context.arcTo(x, y + height, x, y, r)
  context.arcTo(x, y, x + width, y, r)
  context.closePath()
  context.fillStyle = fill
  context.fill()
  if (stroke) {
    context.strokeStyle = stroke
    context.stroke()
  }
}

function wrapLines(context: CanvasRenderingContext2D, text: string, maxWidth: number) {
  const tokens = /[\u3400-\u9fff]/.test(text) ? Array.from(text) : text.split(/(\s+)/).filter(Boolean)
  const lines: string[] = []
  let line = ''
  for (const token of tokens) {
    const next = line + token
    if (line && context.measureText(next).width > maxWidth) {
      lines.push(line.trim())
      line = token.trimStart()
    } else {
      line = next
    }
  }
  if (line) lines.push(line.trim())
  return lines.length ? lines : ['']
}

function drawWrapped(context: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth: number, lineHeight: number) {
  const lines = wrapLines(context, text, maxWidth)
  lines.forEach((line, index) => context.fillText(line, x, y + index * lineHeight))
  return lines.length
}

function drawHeader(context: CanvasRenderingContext2D, title: string, dateLine: string, periodLabel: string) {
  roundRect(context, 28, 28, CANVAS_WIDTH - 56, 330, 34, COLORS.forest)
  context.save()
  context.globalAlpha = 0.18
  context.strokeStyle = '#d9eedc'
  context.lineWidth = 2
  context.beginPath()
  context.arc(1010, 84, 155, 0, Math.PI * 2)
  context.stroke()
  context.beginPath()
  context.arc(1010, 84, 112, 0, Math.PI * 2)
  context.stroke()
  context.globalAlpha = 0.12
  context.fillStyle = '#a9d3b2'
  context.beginPath()
  context.arc(1060, 190, 90, 0, Math.PI * 2)
  context.fill()
  context.restore()

  context.fillStyle = '#d5ead8'
  context.font = `700 20px ${MONO_FONT}`
  context.letterSpacing = '4px'
  context.fillText('RUNWISE', 74, 86)
  context.letterSpacing = '0px'
  context.fillStyle = '#b7d7bd'
  context.font = `600 18px ${MONO_FONT}`
  context.fillText(periodLabel.toUpperCase(), 74, 130)
  context.fillStyle = COLORS.white
  context.font = `800 58px ${FONT}`
  drawWrapped(context, title, 74, 210, 880, 66)
  context.fillStyle = '#c9e1cc'
  context.font = `500 22px ${FONT}`
  context.fillText(dateLine, 74, 286)
}

function drawMetricCard(context: CanvasRenderingContext2D, x: number, y: number, width: number, label: string, value: string, color: string, fill: string) {
  roundRect(context, x, y, width, 152, 20, COLORS.white, COLORS.line)
  context.fillStyle = color
  context.beginPath()
  context.arc(x + 27, y + 31, 7, 0, Math.PI * 2)
  context.fill()
  context.fillStyle = COLORS.muted
  context.font = `600 18px ${FONT}`
  context.fillText(label, x + 47, y + 38)
  context.fillStyle = COLORS.ink
  context.font = `800 39px ${FONT}`
  const lines = wrapLines(context, value, width - 40)
  lines.slice(0, 2).forEach((line, index) => context.fillText(line, x + 23, y + 94 + index * 40))
  context.fillStyle = fill
  context.fillRect(x + 23, y + 130, Math.min(width - 46, Math.max(24, context.measureText(value).width * 0.35)), 4)
}

function drawWeatherIcon(context: CanvasRenderingContext2D, kind: string, x: number, y: number, scale = 1) {
  context.save()
  context.translate(x, y)
  context.scale(scale, scale)
  context.lineCap = 'round'
  context.lineJoin = 'round'
  context.lineWidth = 4
  if (kind === 'clear') {
    context.strokeStyle = COLORS.amber
    context.beginPath()
    context.arc(34, 31, 18, 0, Math.PI * 2)
    context.stroke()
    for (let index = 0; index < 8; index += 1) {
      const angle = index * Math.PI / 4
      context.beginPath()
      context.moveTo(34 + Math.cos(angle) * 29, 31 + Math.sin(angle) * 29)
      context.lineTo(34 + Math.cos(angle) * 39, 31 + Math.sin(angle) * 39)
      context.stroke()
    }
  } else {
    context.fillStyle = '#fffdf8'
    context.strokeStyle = '#96b4a0'
    context.beginPath()
    context.arc(30, 35, 16, 0, Math.PI * 2)
    context.arc(48, 34, 20, 0, Math.PI * 2)
    context.arc(68, 39, 13, 0, Math.PI * 2)
    context.fill()
    context.stroke()
    context.beginPath()
    context.moveTo(18, 50)
    context.lineTo(79, 50)
    context.stroke()
    if (kind === 'rain' || kind === 'storm') {
      context.strokeStyle = COLORS.blue
      for (let index = 0; index < 3; index += 1) {
        context.beginPath()
        context.moveTo(31 + index * 18, 59)
        context.lineTo(25 + index * 18, 72)
        context.stroke()
      }
    }
    if (kind === 'snow') {
      context.strokeStyle = COLORS.blue
      for (let index = 0; index < 2; index += 1) {
        const centerX = 36 + index * 28
        context.beginPath()
        context.moveTo(centerX - 6, 65)
        context.lineTo(centerX + 6, 65)
        context.moveTo(centerX, 59)
        context.lineTo(centerX, 71)
        context.stroke()
      }
    }
  }
  context.restore()
}

function drawRunCard(context: CanvasRenderingContext2D, model: RunModel, labels: ShareCardLabels, locale: Locale, t: Translate) {
  const { run, weather } = model
  const runDate = runLocalDate(run) ?? new Date()
  const dateLine = formatDate(runDate, locale)
  drawHeader(context, labels.runTitle, dateLine, labels.runType)

  const metricY = 394
  const metricWidth = 254
  const metricGap = 18
  const metricX = 56
  const pace = run.distance_km > 0 ? (run.moving_seconds ?? run.duration_seconds) / run.distance_km : null
  drawMetricCard(context, metricX, metricY, metricWidth, labels.distance, formatDistance(run.distance_km, locale), COLORS.mintStrong, COLORS.mint)
  drawMetricCard(context, metricX + metricWidth + metricGap, metricY, metricWidth, labels.pace, formatPace(pace, t), COLORS.purple, COLORS.purpleSoft)
  drawMetricCard(context, metricX + (metricWidth + metricGap) * 2, metricY, metricWidth, labels.duration, formatDuration(run.duration_seconds, locale), COLORS.blue, COLORS.blueSoft)
  drawMetricCard(context, metricX + (metricWidth + metricGap) * 3, metricY, metricWidth, labels.heartRate, run.avg_hr == null ? '—' : `${Math.round(run.avg_hr)} bpm`, COLORS.amber, COLORS.amberSoft)

  const weatherY = 596
  roundRect(context, 56, weatherY, CANVAS_WIDTH - 112, 277, 24, '#edf5ef', '#d5e5d7')
  context.fillStyle = COLORS.forestDeep
  context.font = `800 25px ${FONT}`
  context.fillText(labels.weather, 90, weatherY + 48)
  const kind = condition(weather?.weather_code)
  drawWeatherIcon(context, kind, 90, weatherY + 76, 1.35)
  context.fillStyle = COLORS.forest
  context.font = `800 31px ${FONT}`
  context.fillText(weatherLabel(weather, t), 235, weatherY + 105)
  context.fillStyle = COLORS.muted
  context.font = `500 19px ${FONT}`
  context.fillText(labels.weatherEstimate, 235, weatherY + 138)
  const readingX = 740
  context.fillStyle = COLORS.muted
  context.font = `600 17px ${FONT}`
  context.fillText(labels.temperature, readingX, weatherY + 53)
  context.fillText(labels.humidity, readingX + 190, weatherY + 53)
  context.fillStyle = COLORS.ink
  context.font = `800 38px ${FONT}`
  context.fillText(weatherTemperature(weather), readingX, weatherY + 99)
  context.fillText(weatherHumidity(weather), readingX + 190, weatherY + 99)
  if (weather?.status !== 'available') {
    context.fillStyle = COLORS.muted
    context.font = `500 18px ${FONT}`
    drawWrapped(context, labels.weatherUnavailable, readingX, weatherY + 147, 350, 28)
  }

  roundRect(context, 56, 932, CANVAS_WIDTH - 112, 102, 20, COLORS.white, COLORS.line)
  context.fillStyle = COLORS.muted
  context.font = `600 17px ${FONT}`
  context.fillText(labels.runTypeLabel, 84, 972)
  context.fillStyle = COLORS.forest
  context.font = `800 25px ${FONT}`
  context.fillText(typeLabel(run.run_type, t), 84, 1008)
  context.fillStyle = COLORS.muted
  context.font = `500 17px ${FONT}`
  context.fillText(labels.generatedFrom, 740, 1008)
  drawFooter(context, labels.footer, 1138)
}

function drawPeriodCard(context: CanvasRenderingContext2D, model: PeriodModel, labels: ShareCardLabels, locale: Locale, t: Translate) {
  const dateLine = labels.periodRange
  drawHeader(context, model.kind === 'week' ? labels.weekTitle : labels.monthTitle, dateLine, model.kind === 'week' ? labels.weekLabel : labels.monthLabel)

  const metricY = 394
  const metricWidth = 254
  const metricGap = 18
  const metricX = 56
  drawMetricCard(context, metricX, metricY, metricWidth, labels.totalDistance, formatDistance(model.totalDistance, locale), COLORS.mintStrong, COLORS.mint)
  drawMetricCard(context, metricX + metricWidth + metricGap, metricY, metricWidth, labels.runCount, String(model.runCount), COLORS.amber, COLORS.amberSoft)
  drawMetricCard(context, metricX + (metricWidth + metricGap) * 2, metricY, metricWidth, labels.runningDays, String(model.runningDays), COLORS.blue, COLORS.blueSoft)
  drawMetricCard(context, metricX + (metricWidth + metricGap) * 3, metricY, metricWidth, labels.averagePace, formatPace(model.averagePace, t), COLORS.purple, COLORS.purpleSoft)

  const chartY = 625
  context.fillStyle = COLORS.ink
  context.font = `800 25px ${FONT}`
  context.fillText(model.kind === 'week' ? labels.dayDistance : labels.weekDistance, 56, chartY)
  context.fillStyle = COLORS.muted
  context.font = `500 17px ${FONT}`
  context.fillText(labels.chartHint, 56, chartY + 31)
  drawBars(context, model.buckets, chartY + 77, labels, locale, model.kind)

  const typeY = model.kind === 'week' ? 1060 : 1100
  context.fillStyle = COLORS.ink
  context.font = `800 25px ${FONT}`
  context.fillText(labels.trainingTypes, 56, typeY)
  if (model.typeCounts.length === 0) {
    context.fillStyle = COLORS.muted
    context.font = `500 19px ${FONT}`
    context.fillText(labels.noTraining, 56, typeY + 53)
  } else {
    drawTypeRows(context, model.typeCounts, typeY + 48, labels, t)
  }
  context.fillStyle = COLORS.muted
  context.font = `500 17px ${FONT}`
  context.fillText(`${labels.duration}: ${formatDuration(model.totalDuration, locale)}`, 56, 1390)
  drawFooter(context, labels.footer, model.kind === 'week' ? 1432 : 1492)
}

function drawBars(context: CanvasRenderingContext2D, buckets: PeriodBucket[], y: number, labels: ShareCardLabels, locale: Locale, kind: 'week' | 'month') {
  const x = 73
  const width = CANVAS_WIDTH - 146
  const height = 265
  const maxDistance = Math.max(...buckets.map((bucket) => bucket.distance), 1)
  context.strokeStyle = COLORS.line
  context.lineWidth = 2
  context.beginPath()
  context.moveTo(x, y + height)
  context.lineTo(x + width, y + height)
  context.stroke()
  const slot = width / Math.max(1, buckets.length)
  const barWidth = Math.min(72, Math.max(18, slot * 0.52))
  buckets.forEach((bucket, index) => {
    const barHeight = bucket.distance > 0 ? Math.max(10, bucket.distance / maxDistance * (height - 34)) : 4
    const barX = x + slot * index + (slot - barWidth) / 2
    roundRect(context, barX, y + height - barHeight, barWidth, barHeight, Math.min(13, barWidth / 3), bucket.distance > 0 ? COLORS.forest : '#e2e8e0')
    context.fillStyle = COLORS.muted
    context.font = `600 ${buckets.length > 6 ? 14 : 16}px ${MONO_FONT}`
    const label = kind === 'week'
      ? formatDate(bucket.date, locale)
      : formatDate(bucket.date, locale)
    const labelWidth = context.measureText(label).width
    context.fillText(label, barX + (barWidth - labelWidth) / 2, y + height + 31)
    if (bucket.count > 0) {
      context.fillStyle = COLORS.forest
      context.font = `700 ${buckets.length > 6 ? 14 : 16}px ${MONO_FONT}`
      const countLabel = String(bucket.count)
      const countWidth = context.measureText(countLabel).width
      context.fillText(countLabel, barX + (barWidth - countWidth) / 2, y + height - barHeight - 15)
    }
  })
  context.fillStyle = COLORS.muted
  context.font = `500 14px ${FONT}`
  context.fillText(labels.runCount, x + width - 100, y + height + 67)
}

function drawTypeRows(context: CanvasRenderingContext2D, types: TypeCount[], y: number, labels: ShareCardLabels, t: Translate) {
  const maxCount = Math.max(...types.map((item) => item.count), 1)
  const visibleTypes = types.slice(0, 6)
  const colors = [COLORS.forest, COLORS.amber, COLORS.blue, COLORS.purple, '#9d8060', '#79a384']
  visibleTypes.forEach((item, index) => {
    const rowY = y + index * 48
    context.fillStyle = COLORS.muted
    context.font = `600 18px ${FONT}`
    context.fillText(typeLabel(item.type, t), 80, rowY + 24)
    roundRect(context, 280, rowY + 8, 690, 15, 8, '#e8eee6')
    roundRect(context, 280, rowY + 8, Math.max(14, item.count / maxCount * 690), 15, 8, colors[index % colors.length])
    context.fillStyle = COLORS.ink
    context.font = `800 19px ${MONO_FONT}`
    context.fillText(String(item.count), 1008, rowY + 24)
    context.fillStyle = COLORS.muted
    context.font = `500 14px ${FONT}`
    context.fillText(item.count === 1 ? labels.countOne : labels.count.replace('{count}', String(item.count)), 1048, rowY + 24)
  })
}

function drawFooter(context: CanvasRenderingContext2D, footer: string, y: number) {
  context.fillStyle = COLORS.muted
  context.font = `500 16px ${FONT}`
  context.fillText(footer, 56, y)
  context.fillStyle = COLORS.mintStrong
  context.fillRect(CANVAS_WIDTH - 160, y - 12, 104, 4)
}

type ShareCardLabels = {
  runTitle: string
  weekTitle: string
  monthTitle: string
  runType: string
  runTypeLabel: string
  weekLabel: string
  monthLabel: string
  periodRange: string
  distance: string
  pace: string
  duration: string
  heartRate: string
  weather: string
  weatherEstimate: string
  weatherUnavailable: string
  temperature: string
  humidity: string
  totalDistance: string
  runCount: string
  runningDays: string
  averagePace: string
  trainingTypes: string
  noTraining: string
  dayDistance: string
  weekDistance: string
  chartHint: string
  count: string
  countOne: string
  generatedFrom: string
  footer: string
}

function drawShareCard(model: RunModel | PeriodModel, labels: ShareCardLabels, locale: Locale, t: Translate) {
  const canvas = document.createElement('canvas')
  canvas.width = CANVAS_WIDTH
  canvas.height = model.kind === 'run' ? 1280 : model.kind === 'week' ? 1510 : 1570
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Canvas is unavailable')
  context.fillStyle = COLORS.paper
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.textBaseline = 'alphabetic'
  context.textAlign = 'left'
  if (model.kind === 'run') drawRunCard(context, model, labels, locale, t)
  else drawPeriodCard(context, model, labels, locale, t)
  return canvas.toDataURL('image/png')
}

function downloadImage(dataUrl: string, filename: string) {
  const link = document.createElement('a')
  link.href = dataUrl
  link.download = filename
  link.rel = 'noreferrer'
  document.body.appendChild(link)
  link.click()
  link.remove()
}

export function ShareImage({ target, runs, locale, t }: ShareImageProps) {
  const selectedRun = useMemo(() => {
    if (target.kind !== 'run') return null
    const available = exportableRuns(runs)
    if (target.runId != null) return available.find((run) => String(run.id) === String(target.runId)) ?? null
    return latestRun(available)
  }, [runs, target.kind, target.runId])

  const periodModel = useMemo(() => {
    if (target.kind === 'run') return null
    const anchor = parseDate(target.date) ?? new Date()
    return createPeriodModel(target.kind, anchor, runs)
  }, [runs, target.date, target.kind])

  const [weather, setWeather] = useState<WeatherState>({ status: 'idle' })
  useEffect(() => {
    if (target.kind !== 'run' || !selectedRun) {
      setWeather({ status: 'idle' })
      return undefined
    }
    let active = true
    setWeather({ status: 'loading' })
    void request<Weather>(`/api/runs/${encodeURIComponent(String(selectedRun.id))}/weather`).then((data) => {
      if (active) setWeather({ status: 'success', data })
    }).catch(() => {
      if (active) setWeather({ status: 'error' })
    })
    return () => { active = false }
  }, [selectedRun, target.kind])

  const labels = useMemo<ShareCardLabels>(() => ({
    runTitle: t('shareImage.runTitle'),
    weekTitle: t('shareImage.weekTitle'),
    monthTitle: t('shareImage.monthTitle'),
    runType: t('shareImage.runType'),
    runTypeLabel: t('shareImage.runType'),
    weekLabel: t('stats.week'),
    monthLabel: t('stats.month'),
    periodRange: periodModel ? t('shareImage.periodRange', {
      start: formatDate(periodModel.start, locale),
      end: formatDate(periodModel.end, locale),
    }) : '',
    distance: t('shareImage.distance'),
    pace: t('shareImage.pace'),
    duration: t('runs.elapsedTime'),
    heartRate: t('shareImage.heartRate'),
    weather: t('shareImage.weather'),
    weatherEstimate: t('weather.estimate'),
    weatherUnavailable: t('shareImage.weatherUnavailable'),
    temperature: t('shareImage.temperature'),
    humidity: t('shareImage.humidity'),
    totalDistance: t('shareImage.totalDistance'),
    runCount: t('shareImage.runCount'),
    runningDays: t('shareImage.runningDays'),
    averagePace: t('shareImage.averagePace'),
    trainingTypes: t('shareImage.trainingTypes'),
    noTraining: t('shareImage.noTraining'),
    dayDistance: t('shareImage.dayDistance'),
    weekDistance: t('shareImage.weekDistance'),
    chartHint: t('shareImage.chartHint'),
    count: t('shareImage.count'),
    countOne: t('shareImage.countOne'),
    generatedFrom: t('shareImage.generatedFrom'),
    footer: t('shareImage.generatedFrom'),
  }), [locale, periodModel, t])

  const model = useMemo<CardModel>(() => {
    if (target.kind === 'run') {
      if (!selectedRun) return { kind: 'empty' }
      return { kind: 'run', run: selectedRun, weather: weather.status === 'success' ? weather.data : null }
    }
    return periodModel ?? { kind: 'empty' }
  }, [periodModel, selectedRun, target.kind, weather])

  const weatherReady = target.kind !== 'run' || weather.status === 'success' || weather.status === 'error'
  const [imageUrl, setImageUrl] = useState<string | null>(null)
  const [renderFailed, setRenderFailed] = useState(false)
  const [downloaded, setDownloaded] = useState(false)
  useEffect(() => {
    setImageUrl(null)
    setRenderFailed(false)
    setDownloaded(false)
    if (model.kind === 'empty' || !weatherReady) return undefined
    try {
      setImageUrl(drawShareCard(model, labels, locale, t))
    } catch {
      setRenderFailed(true)
    }
    return undefined
  }, [labels, locale, model, t, weatherReady])

  const imageAlt = target.kind === 'run' ? t('shareImage.runTitle') : target.kind === 'week' ? t('shareImage.weekTitle') : t('shareImage.monthTitle')
  const downloadName = `runwise-${target.kind}-${target.date ?? (selectedRun ? dateKey(runLocalDate(selectedRun) ?? new Date()) : dateKey(new Date()))}.png`
  const weatherError = target.kind === 'run' && (weather.status === 'error' || (weather.status === 'success' && weather.data.status !== 'available'))

  return <div className="share-image-dialog">
    <p className="share-image-copy">{t('shareImage.copy')}</p>
    <div className="share-image-preview-frame">
      {model.kind === 'empty' && <div className="share-image-state share-image-state-error" role="alert"><Icon name="info" size={19} /><span>{t('shareImage.runUnavailable')}</span></div>}
      {model.kind !== 'empty' && !imageUrl && !renderFailed && <div className="share-image-state" role="status"><span className="share-image-spinner" /><span>{weather.status === 'loading' ? t('shareImage.weatherLoading') : t('shareImage.preparing')}</span></div>}
      {renderFailed && <div className="share-image-state share-image-state-error" role="alert"><Icon name="refresh" size={19} /><span>{t('shareImage.renderError')}</span></div>}
      {imageUrl && <img className="share-image-preview" src={imageUrl} alt={imageAlt} />}
    </div>
    {weatherError && <p className="share-image-weather-note"><Icon name="info" size={15} />{t('shareImage.weatherUnavailable')}</p>}
    <div className="share-image-actions">
      <button className="button button-primary" type="button" disabled={!imageUrl} onClick={() => { if (!imageUrl) return; downloadImage(imageUrl, downloadName); setDownloaded(true) }}><Icon name="download" size={16} />{downloaded ? t('shareImage.downloaded') : t('shareImage.downloadPng')}</button>
      <small>{t('shareImage.downloadHint')}</small>
    </div>
  </div>
}
