import { useEffect, useState } from 'react'
import { request } from './lib/api'
import './weather.css'

type Weather = { status: string; reason?: string; temperature_c?: number | null; humidity_percent?: number | null; weather_code?: number | null }
type Translate = (key: string, values?: Record<string, string | number>) => string

function condition(code: number | null | undefined) {
  if (code == null) return 'unknown'
  if (code === 0) return 'clear'
  if (code <= 3) return 'cloudy'
  if (code <= 48) return 'fog'
  if (code <= 67 || (code >= 80 && code <= 82)) return 'rain'
  if (code <= 77 || code === 85 || code === 86) return 'snow'
  return 'storm'
}

export function RunWeather({ runId, t }: { runId: string | number; t: Translate }) {
  const [state, setState] = useState<{ id: string | number; data: Weather } | null>(null)
  useEffect(() => {
    let active = true
    request<Weather>(`/api/runs/${encodeURIComponent(runId)}/weather`).then(data => {
      if (active) setState({ id: runId, data })
    }).catch(() => { if (active) setState({ id: runId, data: { status: 'unavailable', reason: 'provider_unavailable' } }) })
    return () => { active = false }
  }, [runId])
  const data = state?.id === runId ? state.data : null
  if (!data) return <div className="run-weather-pending" role="status">{t('weather.loading')}</div>
  if (data.status !== 'available') return <div className="run-weather-pending">{t(data.reason === 'missing_location' ? 'weather.noLocation' : 'weather.unavailable')}</div>
  const kind = condition(data.weather_code)
  return <section className={`run-weather weather-${kind}`} aria-label={t('weather.title')}>
    <div className="weather-condition"><svg viewBox="0 0 64 64" aria-hidden="true"><circle cx="27" cy="24" r="11" className="weather-sun" />{kind !== 'clear' && <path className="weather-cloud" d="M16 43h31c15 0 13-21 0-21-5-15-26-12-28 1-14-1-18 20-3 20Z" />}{['rain','storm'].includes(kind) && <path d="m23 49-3 6m15-6-3 6m15-6-3 6" className="weather-rain" />}{kind === 'snow' && <path d="M22 50v8m-4-4h8m15-4v8m-4-4h8" className="weather-rain" />}</svg><div><small>{t('weather.title')}</small><strong>{t(`weather.${kind}`)}</strong></div></div>
    <div className="weather-reading"><strong>{data.temperature_c == null ? '—' : `${Math.round(data.temperature_c)}°`}</strong><small>{t('weather.temperature')}</small></div>
    <div className="weather-reading"><strong>{data.humidity_percent == null ? '—' : `${Math.round(data.humidity_percent)}%`}</strong><small>{t('weather.humidity')}</small></div>
    <div className="weather-credit">{t('weather.estimate')} · <a href="https://open-meteo.com/" target="_blank" rel="noreferrer">Open-Meteo</a></div>
  </section>
}
