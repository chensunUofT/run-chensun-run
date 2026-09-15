import { useCallback, useEffect, useMemo, useState, type PointerEvent as ReactPointerEvent } from 'react'
import type { Translate } from './i18n'
import type { RunLap, RunStreamSample } from './types'
import './telemetry.css'

type MetricKey = 'pace' | 'heartRate' | 'elevation'

type TimedSample = {
  t: number
  sample: RunStreamSample
  sourceIndex: number
  distanceKm: number | null
}

type DisplayPoint = {
  t: number
  sourceIndex: number
  distanceKm: number | null
  pace: number | null
  heartRate: number | null
  elevation: number | null
  gapBefore: boolean
  event: boolean
}

type LapBand = {
  startSeconds: number
  endSeconds: number
  startDistanceKm: number
  endDistanceKm: number
  label: string
  sourceIndex: number
}

type DistanceScale = {
  min: number
  max: number
  span: number
}

type ValueScale = {
  min: number
  max: number
  span: number
}

type RunTelemetryProps = {
  samples: RunStreamSample[]
  laps: RunLap[]
  locale: 'en' | 'zh'
  t: Translate
}

const MAX_RENDER_POINTS = 700
const PACE_WINDOW_KM = 0.1
const MIN_PACE_COVERAGE_KM = 0.025
const GAP_SECONDS = 30
const MIN_PACE_SECONDS = 90
const MAX_PACE_SECONDS = 1800
const VIEWBOX_WIDTH = 920
const VIEWBOX_HEIGHT = 400
const PLOT_LEFT = 58
const PLOT_RIGHT = 862
const PLOT_TOP = 42
const PLOT_BOTTOM = 322
const PLOT_WIDTH = PLOT_RIGHT - PLOT_LEFT
const PLOT_HEIGHT = PLOT_BOTTOM - PLOT_TOP
const HEART_AXIS_X = PLOT_RIGHT
const PACE_AXIS_X = PLOT_LEFT

const METRICS: MetricKey[] = ['pace', 'heartRate', 'elevation']

const METRIC_CONFIG: Record<MetricKey, { labelKey: string; color: string; unitKey: string }> = {
  pace: { labelKey: 'telemetry.pace', color: '#397bb8', unitKey: 'telemetry.units.perKm' },
  heartRate: { labelKey: 'telemetry.heartRate', color: '#c54b68', unitKey: 'telemetry.units.bpm' },
  elevation: { labelKey: 'telemetry.elevation', color: '#3c7f70', unitKey: 'telemetry.units.meters' },
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function sampleDistanceKm(sample: RunStreamSample): number | null {
  const distanceKm = finiteNumber(sample.distance_km)
  if (distanceKm !== null) return distanceKm
  const distanceM = finiteNumber(sample.distance_m)
  return distanceM === null ? null : distanceM / 1000
}

function timedSamples(samples: RunStreamSample[]): TimedSample[] {
  return samples
    .map((sample, sourceIndex) => ({
      t: finiteNumber(sample.elapsed_seconds),
      sample,
      sourceIndex,
      distanceKm: sampleDistanceKm(sample),
    }))
    .filter((item): item is TimedSample & { t: number } => item.t !== null)
    .sort((a, b) => a.t - b.t || a.sourceIndex - b.sourceIndex)
}

function weightedMedian(items: Array<{ value: number; weight: number }>): number | null {
  if (items.length === 0) return null
  const sorted = items.filter((item) => item.weight > 0).sort((a, b) => a.value - b.value)
  if (sorted.length === 0) return null
  const totalWeight = sorted.reduce((total, item) => total + item.weight, 0)
  let runningWeight = 0
  for (const item of sorted) {
    runningWeight += item.weight
    if (runningWeight >= totalWeight / 2) return item.value
  }
  return sorted[sorted.length - 1].value
}

/**
 * Estimate pace over distance rather than elapsed time. Segment weights are
 * proportional to the distance they contribute, with a small recency bias so
 * interval changes remain visible. A weighted median and MAD filter keep one
 * bad GPS segment from flattening the useful part of the chart.
 */
function distanceWindowPace(points: TimedSample[], index: number): number | null {
  if (index <= 0) return null
  const current = points[index]
  if (current.distanceKm === null) return null

  const currentDistance = current.distanceKm
  const windowStart = currentDistance - PACE_WINDOW_KM
  const segments: Array<{ pace: number; weight: number }> = []
  let coveredDistance = 0

  for (let cursor = index; cursor > 0; cursor -= 1) {
    const previous = points[cursor - 1]
    const next = points[cursor]

    // A pause or a missing distance value makes the pace before it unrelated
    // to the current point. Never smooth across that boundary.
    if (next.t - previous.t > GAP_SECONDS) break
    if (previous.distanceKm === null || next.distanceKm === null) break

    const distanceDelta = next.distanceKm - previous.distanceKm
    const elapsed = next.t - previous.t
    if (distanceDelta <= 0 || elapsed <= 0) break

    const overlapStart = Math.max(previous.distanceKm, windowStart)
    const overlapEnd = Math.min(next.distanceKm, currentDistance)
    const overlap = overlapEnd - overlapStart
    if (overlap <= 0) continue

    const pace = elapsed / distanceDelta
    if (pace >= MIN_PACE_SECONDS && pace <= MAX_PACE_SECONDS) {
      const segmentMiddle = (overlapStart + overlapEnd) / 2
      const age = Math.max(0, Math.min(1, (currentDistance - segmentMiddle) / PACE_WINDOW_KM))
      const recencyWeight = 1.45 - age * 0.45
      segments.push({ pace, weight: overlap * recencyWeight })
    }
    coveredDistance += overlap
    if (previous.distanceKm <= windowStart) break
  }

  if (segments.length === 0 || coveredDistance < MIN_PACE_COVERAGE_KM) return null

  const median = weightedMedian(segments.map((segment) => ({ value: segment.pace, weight: segment.weight })))
  if (median === null) return null
  const deviations = segments.map((segment) => ({ value: Math.abs(segment.pace - median), weight: segment.weight }))
  const mad = weightedMedian(deviations) ?? 0
  const tolerance = Math.max(12, mad * 3)
  const inliers = segments.filter((segment) => Math.abs(segment.pace - median) <= tolerance)
  const weightedTotal = inliers.reduce((total, segment) => total + segment.weight, 0)
  if (weightedTotal <= 0) return median
  const pace = inliers.reduce((total, segment) => total + segment.pace * segment.weight, 0) / weightedTotal
  return pace >= MIN_PACE_SECONDS && pace <= MAX_PACE_SECONDS ? pace : null
}

function customLapLabel(lap: RunLap): string | null {
  if (typeof lap.label !== 'string') return null
  const label = lap.label.trim()
  if (!label || label.toUpperCase() === 'DISTANCE') return null
  return label
}

function distanceAtTime(points: TimedSample[], targetTime: number): number | null {
  const known = points.filter((point): point is TimedSample & { distanceKm: number } => point.distanceKm !== null)
  if (known.length === 0) return null
  if (targetTime <= known[0].t) return known[0].distanceKm
  const last = known[known.length - 1]
  if (targetTime >= last.t) return last.distanceKm

  for (let index = 1; index < known.length; index += 1) {
    const previous = known[index - 1]
    const current = known[index]
    if (targetTime > current.t) continue
    const duration = current.t - previous.t
    if (duration <= 0) return current.distanceKm
    const ratio = Math.max(0, Math.min(1, (targetTime - previous.t) / duration))
    return previous.distanceKm + (current.distanceKm - previous.distanceKm) * ratio
  }
  return last.distanceKm
}

function lapBands(laps: RunLap[], points: TimedSample[]): LapBand[] {
  return laps
    .map((lap, sourceIndex) => {
      const label = customLapLabel(lap)
      const startSeconds = finiteNumber(lap.start_seconds)
      const endSeconds = finiteNumber(lap.end_seconds)
      if (!label || startSeconds === null || endSeconds === null || endSeconds <= startSeconds) return null
      const startDistanceKm = distanceAtTime(points, startSeconds)
      const endDistanceKm = distanceAtTime(points, endSeconds)
      if (startDistanceKm === null || endDistanceKm === null || endDistanceKm <= startDistanceKm) return null
      return { startSeconds, endSeconds, startDistanceKm, endDistanceKm, label, sourceIndex }
    })
    .filter((band): band is LapBand => band !== null)
}

function nearestPointIndexByTime(points: DisplayPoint[], time: number): number {
  let nearest = 0
  let difference = Number.POSITIVE_INFINITY
  points.forEach((point, index) => {
    const candidate = Math.abs(point.t - time)
    if (candidate < difference) {
      nearest = index
      difference = candidate
    }
  })
  return nearest
}

function nearestPointIndexByDistance(points: DisplayPoint[], distance: number): number {
  let nearest = -1
  let difference = Number.POSITIVE_INFINITY
  points.forEach((point, index) => {
    if (point.distanceKm === null) return
    const candidate = Math.abs(point.distanceKm - distance)
    if (candidate < difference) {
      nearest = index
      difference = candidate
    }
  })
  return nearest >= 0 ? nearest : nearestPointIndexByTime(points, 0)
}

function buildDisplayPoints(points: TimedSample[], bands: LapBand[]): DisplayPoint[] {
  const displayPoints = points.map((item, index): DisplayPoint => {
    const gapBefore = index > 0 && item.t - points[index - 1].t > GAP_SECONDS
    const heartRate = finiteNumber(item.sample.heart_rate)
    const elevation = finiteNumber(item.sample.altitude_m) ?? finiteNumber(item.sample.elevation_m)
    const previousHeartRate = index > 0 ? finiteNumber(points[index - 1].sample.heart_rate) : null
    const previousElevation = index > 0
      ? finiteNumber(points[index - 1].sample.altitude_m) ?? finiteNumber(points[index - 1].sample.elevation_m)
      : null
    return {
      t: item.t,
      sourceIndex: item.sourceIndex,
      distanceKm: item.distanceKm,
      pace: gapBefore ? null : distanceWindowPace(points, index),
      heartRate,
      elevation,
      gapBefore,
      event:
        index === 0 ||
        index === points.length - 1 ||
        gapBefore ||
        item.distanceKm === null ||
        (index > 0 && (item.distanceKm === null) !== (points[index - 1].distanceKm === null)) ||
        (index > 0 && (heartRate === null) !== (previousHeartRate === null)) ||
        (index > 0 && (elevation === null) !== (previousElevation === null)),
    }
  })

  displayPoints.forEach((point, index) => {
    if (index > 0 && (point.gapBefore || (point.pace === null) !== (displayPoints[index - 1].pace === null))) {
      point.event = true
      displayPoints[index - 1].event = true
    }
  })

  bands.forEach((band) => {
    if (displayPoints.length === 0) return
    displayPoints[nearestPointIndexByTime(displayPoints, band.startSeconds)].event = true
    displayPoints[nearestPointIndexByTime(displayPoints, band.endSeconds)].event = true
  })
  return displayPoints
}

function metricValue(point: DisplayPoint, metric: MetricKey): number | null {
  if (metric === 'pace') return point.pace
  if (metric === 'heartRate') return point.heartRate
  return point.elevation
}

function addBucketExtrema(
  points: DisplayPoint[],
  bucket: number[],
  metric: MetricKey,
  candidates: Set<number>,
) {
  let minIndex: number | null = null
  let maxIndex: number | null = null
  bucket.forEach((index) => {
    const value = metricValue(points[index], metric)
    if (value === null) return
    if (minIndex === null || value < (metricValue(points[minIndex], metric) ?? Number.POSITIVE_INFINITY)) minIndex = index
    if (maxIndex === null || value > (metricValue(points[maxIndex], metric) ?? Number.NEGATIVE_INFINITY)) maxIndex = index
  })
  if (minIndex !== null) candidates.add(minIndex)
  if (maxIndex !== null) candidates.add(maxIndex)
}

/** Reduce only after marking boundaries and extrema, always retaining both timeline endpoints. */
function reducePoints(points: DisplayPoint[]): DisplayPoint[] {
  if (points.length <= MAX_RENDER_POINTS) return points

  const mandatory = new Set<number>()
  points.forEach((point, index) => {
    if (point.event) mandatory.add(index)
  })

  const mandatoryIndices = [...mandatory].sort((a, b) => a - b)
  const selected = new Set<number>()
  if (mandatoryIndices.length <= MAX_RENDER_POINTS) {
    mandatoryIndices.forEach((index) => selected.add(index))
  } else {
    // Keep the complete timeline even when a provider emits more event markers
    // than the rendering budget permits.
    selected.add(0)
    selected.add(points.length - 1)
    const slots = MAX_RENDER_POINTS - selected.size
    for (let slot = 0; slot < slots; slot += 1) {
      const source = mandatoryIndices[Math.floor((slot / Math.max(1, slots - 1)) * (mandatoryIndices.length - 1))]
      selected.add(source)
    }
  }

  const ordinary = points.map((_, index) => index).filter((index) => !selected.has(index))
  const remaining = MAX_RENDER_POINTS - selected.size
  if (remaining > 0 && ordinary.length > 0) {
    const bucketCount = Math.min(Math.max(1, Math.floor(remaining / 8)), ordinary.length)
    const buckets: number[][] = Array.from({ length: bucketCount }, () => [])
    ordinary.forEach((index, ordinal) => {
      buckets[Math.min(bucketCount - 1, Math.floor((ordinal * bucketCount) / ordinary.length))].push(index)
    })

    const candidates = new Set<number>()
    buckets.forEach((bucket) => {
      if (bucket.length === 0) return
      candidates.add(bucket[0])
      candidates.add(bucket[bucket.length - 1])
      addBucketExtrema(points, bucket, 'pace', candidates)
      addBucketExtrema(points, bucket, 'heartRate', candidates)
      addBucketExtrema(points, bucket, 'elevation', candidates)
    })

    ;[...candidates]
      .sort((a, b) => a - b)
      .forEach((index) => {
        if (selected.size < MAX_RENDER_POINTS) selected.add(index)
      })
  }

  if (selected.size < MAX_RENDER_POINTS) {
    const unused = ordinary.filter((index) => !selected.has(index))
    const slots = Math.min(MAX_RENDER_POINTS - selected.size, unused.length)
    for (let slot = 0; slot < slots; slot += 1) {
      selected.add(unused[Math.floor((slot / Math.max(1, slots - 1)) * (unused.length - 1))])
    }
  }

  // Filtering in source order makes the x-axis and hover order stable. The
  // endpoints were marked mandatory above, so no tail of the activity is lost.
  return points.filter((_, index) => selected.has(index))
}

function distanceScale(points: DisplayPoint[]): DistanceScale | null {
  const values = points.map((point) => point.distanceKm).filter((value): value is number => value !== null)
  if (values.length === 0) return null
  const min = Math.min(...values)
  const max = Math.max(...values)
  if (max === min) return { min, max: min + 0.1, span: 0.1 }
  return { min, max, span: max - min }
}

function valueScale(points: DisplayPoint[], metric: MetricKey): ValueScale | null {
  const values = points.map((point) => metricValue(point, metric)).filter((value): value is number => value !== null)
  if (values.length === 0) return null

  const sorted = [...values].sort((a, b) => a - b)
  const rawMin = Math.min(...sorted)
  const rawMax = Math.max(...sorted)
  const padding = rawMin === rawMax ? Math.max(1, Math.abs(rawMin) * 0.04) : Math.max(2, (rawMax - rawMin) * 0.08)
  const min = metric === 'pace' ? Math.max(MIN_PACE_SECONDS, rawMin - padding) : rawMin - padding
  const max = Math.max(min + 1, rawMax + padding)
  return { min, max, span: max - min }
}

function clampRatio(value: number): number {
  return Math.max(0, Math.min(1, value))
}

function xForDistance(value: number, scale: DistanceScale): number {
  return PLOT_LEFT + clampRatio((value - scale.min) / scale.span) * PLOT_WIDTH
}

function yForValue(value: number, scale: ValueScale, metric: MetricKey): number {
  const ratio = clampRatio((value - scale.min) / scale.span)
  // Pace is inverted so a faster pace (lower seconds/km) appears higher.
  const visualRatio = metric === 'pace' ? 1 - ratio : ratio
  return PLOT_BOTTOM - visualRatio * PLOT_HEIGHT
}

function formatElapsed(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const remainder = rounded % 60
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
  return `${minutes}:${String(remainder).padStart(2, '0')}`
}

function formatDistance(value: number, locale: 'en' | 'zh'): string {
  return new Intl.NumberFormat(locale === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits: 2 }).format(value)
}

function formatPace(seconds: number, t: Translate): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return t('telemetry.noValue')
  const rounded = Math.round(seconds)
  const minutes = Math.floor(rounded / 60)
  const remainder = rounded % 60
  return `${minutes}:${String(remainder).padStart(2, '0')} ${t('telemetry.units.perKm')}`
}

function formatMetricValue(value: number | null, metric: MetricKey, locale: 'en' | 'zh', t: Translate): string {
  if (value === null || !Number.isFinite(value)) return t('telemetry.noValue')
  if (metric === 'pace') return formatPace(value, t)
  if (metric === 'heartRate') return `${Math.round(value)} ${t('telemetry.units.bpm')}`
  return `${new Intl.NumberFormat(locale === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits: 1 }).format(value)} ${t('telemetry.units.meters')}`
}

function formatAxisValue(value: number, metric: MetricKey, locale: 'en' | 'zh'): string {
  if (metric === 'pace') {
    const rounded = Math.max(0, Math.round(value))
    return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`
  }
  return new Intl.NumberFormat(locale === 'zh' ? 'zh-CN' : 'en-US', { maximumFractionDigits: 0 }).format(value)
}

type ChartCoordinate = { x: number; y: number }

function clampToRange(value: number, first: number, second: number, third: number): number {
  return Math.max(Math.min(first, second, third), Math.min(Math.max(first, second, third), value))
}

function chartSegments(points: DisplayPoint[], metric: MetricKey, distance: DistanceScale, values: ValueScale): ChartCoordinate[][] {
  const segments: ChartCoordinate[][] = []
  let segment: ChartCoordinate[] = []
  let previousDistance: number | null = null

  const flush = () => {
    if (segment.length > 0) segments.push(segment)
    segment = []
  }

  points.forEach((point) => {
    const value = metricValue(point, metric)
    if (value === null || point.distanceKm === null) {
      flush()
      previousDistance = null
      return
    }

    const distanceReversed = previousDistance !== null && point.distanceKm < previousDistance
    if (point.gapBefore || distanceReversed) flush()

    segment.push({
      x: xForDistance(point.distanceKm, distance),
      y: yForValue(value, values, metric),
    })
    previousDistance = point.distanceKm
  })
  flush()
  return segments
}

function cubicCommands(segment: ChartCoordinate[]): string {
  if (segment.length < 2) return ''
  let path = ''
  for (let index = 0; index < segment.length - 1; index += 1) {
    const previous = segment[index - 1] ?? segment[index]
    const current = segment[index]
    const next = segment[index + 1]
    const following = segment[index + 2] ?? next
    const controlOne = {
      x: current.x + (next.x - previous.x) / 6,
      y: current.y + (next.y - previous.y) / 6,
    }
    const controlTwo = {
      x: next.x - (following.x - current.x) / 6,
      y: next.y - (following.y - current.y) / 6,
    }
    // Keep the smoothing handles within their neighbouring samples. This
    // preserves the readable curve without inventing peaks between points.
    controlOne.x = clampToRange(controlOne.x, previous.x, current.x, next.x)
    controlOne.y = clampToRange(controlOne.y, previous.y, current.y, next.y)
    controlTwo.x = clampToRange(controlTwo.x, current.x, next.x, following.x)
    controlTwo.y = clampToRange(controlTwo.y, current.y, next.y, following.y)
    path += `C ${controlOne.x.toFixed(2)} ${controlOne.y.toFixed(2)} ${controlTwo.x.toFixed(2)} ${controlTwo.y.toFixed(2)} ${next.x.toFixed(2)} ${next.y.toFixed(2)} `
  }
  return path
}

function smoothSegmentPath(segment: ChartCoordinate[], baseline: number | null = null): string {
  if (segment.length === 0) return ''
  const first = segment[0]
  const last = segment[segment.length - 1]
  let path = baseline === null
    ? `M ${first.x.toFixed(2)} ${first.y.toFixed(2)} `
    : `M ${first.x.toFixed(2)} ${baseline.toFixed(2)} L ${first.x.toFixed(2)} ${first.y.toFixed(2)} `

  if (segment.length === 1) {
    if (baseline !== null) path += `L ${last.x.toFixed(2)} ${baseline.toFixed(2)} Z`
    return path.trim()
  }

  path += cubicCommands(segment)
  if (baseline !== null) path += `L ${last.x.toFixed(2)} ${baseline.toFixed(2)} Z`
  return path.trim()
}

function linePath(points: DisplayPoint[], metric: MetricKey, distance: DistanceScale, values: ValueScale): string {
  return chartSegments(points, metric, distance, values).map((segment) => smoothSegmentPath(segment)).join(' ')
}

function areaPath(points: DisplayPoint[], metric: MetricKey, distance: DistanceScale, values: ValueScale): string {
  return chartSegments(points, metric, distance, values)
    .map((segment) => smoothSegmentPath(segment, PLOT_BOTTOM))
    .join(' ')
}

function LapBands({ bands, distance }: { bands: LapBand[]; distance: DistanceScale }) {
  return (
    <g className="telemetry-lap-bands" aria-hidden="true">
      {bands.map((band, index) => {
        const start = Math.max(distance.min, Math.min(distance.max, band.startDistanceKm))
        const end = Math.max(distance.min, Math.min(distance.max, band.endDistanceKm))
        if (end <= start) return null
        const x = xForDistance(start, distance)
        const width = Math.max(1, xForDistance(end, distance) - x)
        return (
          <g key={`${band.sourceIndex}-${band.startSeconds}-${band.endSeconds}`}>
            <rect className={`telemetry-lap-band telemetry-lap-band-${index % 4}`} x={x} y={PLOT_TOP} width={width} height={PLOT_HEIGHT} rx="5" />
            {width > 46 ? (
              <text className="telemetry-lap-label" x={x + 6} y={PLOT_TOP + 15}>
                {band.label}
              </text>
            ) : null}
          </g>
        )
      })}
    </g>
  )
}

function AxisTicks({ metric, scale, locale, t }: { metric: MetricKey; scale: ValueScale | null; locale: 'en' | 'zh'; t: Translate }) {
  if (scale === null || metric === 'elevation') return null
  const config = METRIC_CONFIG[metric]
  const isPace = metric === 'pace'
  const axisX = isPace ? PACE_AXIS_X : HEART_AXIS_X
  const anchor = isPace ? 'end' : 'start'
  const tickX = isPace ? axisX - 10 : axisX + 10
  const captionX = isPace ? PLOT_LEFT + 8 : PLOT_RIGHT - 8
  const captionAnchor = isPace ? 'start' : 'end'
  const values = isPace
    ? [scale.min, (scale.min + scale.max) / 2, scale.max]
    : [scale.max, (scale.min + scale.max) / 2, scale.min]

  return (
    <g className={`telemetry-axis telemetry-axis-${metric}`} style={{ color: config.color }} aria-hidden="true">
      <text className="telemetry-axis-caption" x={captionX} y={PLOT_TOP - 18} textAnchor={captionAnchor} style={{ fill: config.color }}>
        {t(config.labelKey)} · {t(config.unitKey)}
      </text>
      {values.map((value, index) => {
        const y = PLOT_TOP + (index / 2) * PLOT_HEIGHT
        return (
          <g key={`${metric}-${index}`}>
            <line className="telemetry-axis-tick" x1={axisX} x2={isPace ? axisX - 5 : axisX + 5} y1={y} y2={y} />
            <text x={tickX} y={y + 4} textAnchor={anchor}>{formatAxisValue(value, metric, locale)}</text>
          </g>
        )
      })}
    </g>
  )
}

function MetricLegend({
  visibleMetrics,
  scales,
  t,
  onToggle,
}: {
  visibleMetrics: Record<MetricKey, boolean>
  scales: Record<MetricKey, ValueScale | null>
  t: Translate
  onToggle: (metric: MetricKey) => void
}) {
  return (
    <div className="telemetry-series-legend" aria-label={t('telemetry.legend')}>
      {METRICS.map((metric) => {
        const config = METRIC_CONFIG[metric]
        const hasValues = scales[metric] !== null
        return (
          <button
            key={metric}
            className={`telemetry-series-toggle${visibleMetrics[metric] ? ' is-active' : ''}`}
            type="button"
            aria-pressed={visibleMetrics[metric]}
            aria-label={t('telemetry.toggleSeries', { metric: t(config.labelKey) })}
            disabled={!hasValues}
            onClick={() => onToggle(metric)}
          >
            <i className="telemetry-series-swatch" style={{ backgroundColor: config.color }} aria-hidden="true" />
            <span>{t(config.labelKey)}</span>
            <small>{t(config.unitKey)}</small>
          </button>
        )
      })}
      <span className="telemetry-legend-note"><i className="telemetry-legend-swatch telemetry-legend-gap" aria-hidden="true" />{t('telemetry.gap')}</span>
      <span className="telemetry-legend-note"><i className="telemetry-legend-swatch telemetry-legend-band" aria-hidden="true" />{t('telemetry.customLapLegend')}</span>
    </div>
  )
}

function SharedTooltip({
  point,
  distance,
  visibleMetrics,
  scales,
  locale,
  t,
}: {
  point: DisplayPoint
  distance: DistanceScale
  visibleMetrics: Record<MetricKey, boolean>
  scales: Record<MetricKey, ValueScale | null>
  locale: 'en' | 'zh'
  t: Translate
}) {
  if (point.distanceKm === null) return null
  const x = xForDistance(point.distanceKm, distance)
  const width = 214
  const height = 92
  const tooltipX = Math.max(PLOT_LEFT, Math.min(PLOT_RIGHT - width, x - width / 2))
  const activeMetrics = METRICS.filter((metric) => visibleMetrics[metric] && scales[metric] !== null)
  return (
    <g className="telemetry-focus" pointerEvents="none">
      <line className="telemetry-crosshair" x1={x} x2={x} y1={PLOT_TOP} y2={PLOT_BOTTOM} />
      {activeMetrics.map((metric) => {
        const value = metricValue(point, metric)
        const scale = scales[metric]
        return value !== null && scale !== null
          ? <circle key={metric} className="telemetry-focus-point" style={{ fill: METRIC_CONFIG[metric].color }} cx={x} cy={yForValue(value, scale, metric)} r="4.5" />
          : null
      })}
      <g transform={`translate(${tooltipX} 40)`}>
        <rect className="telemetry-tooltip-box" width={width} height={height} rx="9" />
        <text className="telemetry-tooltip-time" x="11" y="16">
          {formatDistance(point.distanceKm, locale)} {t('telemetry.units.km')} · {formatElapsed(point.t)}
        </text>
        {activeMetrics.map((metric, index) => (
          <text key={metric} className="telemetry-tooltip-value" x="11" y={34 + index * 17} style={{ fill: METRIC_CONFIG[metric].color }}>
            {t(METRIC_CONFIG[metric].labelKey)}: {formatMetricValue(metricValue(point, metric), metric, locale, t)}
          </text>
        ))}
      </g>
    </g>
  )
}

export function RunTelemetry({ samples, laps, locale, t }: RunTelemetryProps) {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null)
  const [visibleMetrics, setVisibleMetrics] = useState<Record<MetricKey, boolean>>({ pace: true, heartRate: true, elevation: true })
  const timed = useMemo(() => timedSamples(samples), [samples])
  const bands = useMemo(() => lapBands(laps, timed), [laps, timed])
  const rawPoints = useMemo(() => buildDisplayPoints(timed, bands), [bands, timed])
  const points = useMemo(() => reducePoints(rawPoints), [rawPoints])
  const distance = useMemo(() => distanceScale(points), [points])
  const scales = useMemo<Record<MetricKey, ValueScale | null>>(() => ({
    pace: valueScale(points, 'pace'),
    heartRate: valueScale(points, 'heartRate'),
    elevation: valueScale(points, 'elevation'),
  }), [points])
  const active = hoverIndex === null ? null : points[hoverIndex] ?? null

  useEffect(() => {
    if (points.length === 0) {
      setHoverIndex(null)
    } else if (hoverIndex !== null && hoverIndex >= points.length) {
      setHoverIndex(points.length - 1)
    }
  }, [hoverIndex, points.length])

  const onHover = useCallback((index: number | null) => setHoverIndex(index), [])
  const onToggleMetric = useCallback((metric: MetricKey) => {
    setVisibleMetrics((current) => ({ ...current, [metric]: !current[metric] }))
  }, [])

  const setHoverFromPointer = (event: ReactPointerEvent<SVGSVGElement>) => {
    if (points.length === 0 || distance === null) return
    const rect = event.currentTarget.getBoundingClientRect()
    const svgX = rect.width > 0 ? ((event.clientX - rect.left) / rect.width) * VIEWBOX_WIDTH : PLOT_LEFT
    const fraction = clampRatio((svgX - PLOT_LEFT) / PLOT_WIDTH)
    const targetDistance = distance.min + fraction * distance.span
    onHover(nearestPointIndexByDistance(points, targetDistance))
  }

  if (points.length === 0) {
    return (
      <section className="telemetry telemetry-empty" aria-label={t('telemetry.title')}>
        <div className="telemetry-empty-icon" aria-hidden="true">⌁</div>
        <h3>{t('telemetry.title')}</h3>
        <p>{t('telemetry.noData')}</p>
      </section>
    )
  }

  const sliderValue = hoverIndex ?? 0
  const activeDistance = active?.distanceKm === null || active?.distanceKm === undefined
    ? t('telemetry.noValue')
    : `${formatDistance(active.distanceKm, locale)} ${t('telemetry.units.km')}`

  return (
    <section className="telemetry" aria-label={t('telemetry.title')}>
      <header className="telemetry-header">
        <div>
          <span className="telemetry-eyebrow">{t('telemetry.eyebrow')}</span>
          <h3>{t('telemetry.title')}</h3>
          <p>{t('telemetry.subtitle')}</p>
        </div>
      </header>

      <div className="telemetry-inspector">
        <label htmlFor="telemetry-scrubber">{t('telemetry.scrub')}</label>
        <input
          id="telemetry-scrubber"
          className="telemetry-scrubber"
          type="range"
          min="0"
          max={Math.max(0, points.length - 1)}
          value={sliderValue}
          onChange={(event) => setHoverIndex(Number(event.target.value))}
          aria-valuetext={active ? `${activeDistance}, ${formatElapsed(active.t)}` : t('telemetry.startInspection')}
        />
        <output className="telemetry-inspector-value" htmlFor="telemetry-scrubber">
          {active ? `${activeDistance} · ${formatElapsed(active.t)}` : t('telemetry.startInspection')}
        </output>
      </div>

      <MetricLegend visibleMetrics={visibleMetrics} scales={scales} t={t} onToggle={onToggleMetric} />

      <div className="telemetry-chart-wrap">
        {distance === null ? (
          <div className="telemetry-chart-no-distance">{t('telemetry.noDistance')}</div>
        ) : (
          <svg
            className="telemetry-chart-svg"
            viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
            role="img"
            aria-label={t('telemetry.ariaCombinedChart')}
            onPointerMove={setHoverFromPointer}
            onPointerEnter={setHoverFromPointer}
            onPointerLeave={() => onHover(null)}
          >
            <title>{t('telemetry.ariaCombinedChart')}</title>
            <LapBands bands={bands} distance={distance} />
            <g className="telemetry-grid" aria-hidden="true">
              {[0, 0.5, 1].map((ratio) => {
                const y = PLOT_TOP + ratio * PLOT_HEIGHT
                return <line key={ratio} x1={PLOT_LEFT} x2={PLOT_RIGHT} y1={y} y2={y} />
              })}
            </g>
            <g className="telemetry-gap-markers" aria-hidden="true">
              {points.map((point) => point.gapBefore && point.distanceKm !== null
                ? <line key={`gap-${point.sourceIndex}`} x1={xForDistance(point.distanceKm, distance)} x2={xForDistance(point.distanceKm, distance)} y1={PLOT_TOP} y2={PLOT_BOTTOM} />
                : null)}
            </g>
            {visibleMetrics.pace ? <line className="telemetry-axis-line" x1={PLOT_LEFT} x2={PLOT_LEFT} y1={PLOT_TOP} y2={PLOT_BOTTOM} aria-hidden="true" /> : null}
            {visibleMetrics.heartRate ? <line className="telemetry-axis-line" x1={PLOT_RIGHT} x2={PLOT_RIGHT} y1={PLOT_TOP} y2={PLOT_BOTTOM} aria-hidden="true" /> : null}
            {visibleMetrics.elevation && scales.elevation !== null ? (
              <>
                <path
                  className="telemetry-elevation-area"
                  d={areaPath(points, 'elevation', distance, scales.elevation)}
                />
                <path
                  className="telemetry-line telemetry-line-elevation"
                  style={{ stroke: METRIC_CONFIG.elevation.color }}
                  d={linePath(points, 'elevation', distance, scales.elevation)}
                />
              </>
            ) : null}
            {(['pace', 'heartRate'] as MetricKey[]).map((metric) => scales[metric] !== null && visibleMetrics[metric]
              ? <path key={metric} className={`telemetry-line telemetry-line-${metric}`} style={{ stroke: METRIC_CONFIG[metric].color }} d={linePath(points, metric, distance, scales[metric] as ValueScale)} />
              : null)}
            {visibleMetrics.heartRate ? <AxisTicks metric="heartRate" scale={scales.heartRate} locale={locale} t={t} /> : null}
            {visibleMetrics.pace ? <AxisTicks metric="pace" scale={scales.pace} locale={locale} t={t} /> : null}
            <g className="telemetry-distance-axis" aria-hidden="true">
              {[0, 0.25, 0.5, 0.75, 1].map((ratio) => {
                const x = PLOT_LEFT + ratio * PLOT_WIDTH
                const value = distance.min + ratio * distance.span
                return (
                  <g key={ratio}>
                    <line x1={x} x2={x} y1={PLOT_BOTTOM} y2={PLOT_BOTTOM + 6} />
                    <text x={x} y={PLOT_BOTTOM + 21} textAnchor={ratio === 0 ? 'start' : ratio === 1 ? 'end' : 'middle'}>
                      {formatDistance(value, locale)}
                    </text>
                  </g>
                )
              })}
              <text className="telemetry-distance-title" x={(PLOT_LEFT + PLOT_RIGHT) / 2} y={VIEWBOX_HEIGHT - 5} textAnchor="middle">
                {t('telemetry.distanceAxis')}
              </text>
            </g>
            {active ? <SharedTooltip point={active} distance={distance} visibleMetrics={visibleMetrics} scales={scales} locale={locale} t={t} /> : null}
            <rect className="telemetry-pointer-target" x={PLOT_LEFT} y={PLOT_TOP} width={PLOT_WIDTH} height={PLOT_HEIGHT} />
            {METRICS.every((metric) => scales[metric] === null) ? (
              <text className="telemetry-no-series" x={(PLOT_LEFT + PLOT_RIGHT) / 2} y={(PLOT_TOP + PLOT_BOTTOM) / 2} textAnchor="middle">
                {t('telemetry.noSeriesAny')}
              </text>
            ) : null}
          </svg>
        )}
      </div>
    </section>
  )
}

export default RunTelemetry
