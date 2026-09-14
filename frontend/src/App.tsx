import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent, Dispatch, FormEvent, ReactNode, SetStateAction } from 'react'
import { useAuth } from './auth'
import { Icon, type IconName } from './icons'
import { useI18n, localeTag, type Translate } from './i18n'
import { api, ApiError } from './lib/api'
import type {
  Analysis,
  Checkin,
  GoogleHealthInspection,
  ImportResult,
  Integration,
  PublicShareSnapshot,
  Run,
  RunId,
  RunLap,
  RunPayload,
  RunStreamSample,
  RunStreams,
  Share,
  ShareKind,
  Shoe,
  ShoeCatalogItem,
  ShoeInference,
  ShoePrediction,
  ShoeRules,
  ShoePayload,
  Stats,
} from './types'

type ViewKey = 'overview' | 'stats' | 'runs' | 'shoes' | 'checkin' | 'settings'
type ResourceState<T> = {
  data: T | null
  status: 'idle' | 'loading' | 'success' | 'error'
  error: string | null
}
type Toast = { tone: 'success' | 'error' | 'info'; message: string }
type ShareTarget = { kind: ShareKind; runId?: RunId; date?: string }

const VIEWS: Array<{ key: ViewKey; icon: IconName }> = [
  { key: 'overview', icon: 'activity' },
  { key: 'stats', icon: 'chart' },
  { key: 'runs', icon: 'activity' },
  { key: 'shoes', icon: 'shoe' },
  { key: 'checkin', icon: 'sun' },
  { key: 'settings', icon: 'settings' },
]

const RUN_TYPE_VALUES = ['easy', 'long', 'tempo', 'interval', 'race'] as const
const SHOE_STATUS_VALUES = ['active', 'rotation', 'retired'] as const

const emptyResource = <T,>(): ResourceState<T> => ({ data: null, status: 'idle', error: null })

function errorMessage(error: unknown, t: Translate, fallback = 'errors.generic') {
  if (error instanceof ApiError) {
    if (error.status === 404) return t('errors.notFound')
    if (error.status === 503 && typeof error.detail === 'string' && /Google (OAuth|Health)/i.test(error.detail)) return t('settings.googleNotConfigured')
    if (error.status >= 500) return t('errors.server')
    if (error.status === 401 || error.status === 403) return t('errors.authRequired')
    if (error.message && !/^Request failed \(/.test(error.message)) return error.message
    return t('errors.request', { status: error.status })
  }
  if (error instanceof Error && error.message && !/network|fetch/i.test(error.message)) return error.message
  return t(fallback)
}

async function loadResource<T>(
  loader: () => Promise<T>,
  setState: Dispatch<SetStateAction<ResourceState<T>>>,
  requestRef: { current: number },
  t: Translate,
  fallback: string,
) {
  const requestId = ++requestRef.current
  setState((current) => ({ ...current, status: 'loading', error: null }))
  try {
    const data = await loader()
    if (requestId !== requestRef.current) return null
    setState({ data, status: 'success', error: null })
    return data
  } catch (error) {
    if (requestId !== requestRef.current) return null
    setState((current) => ({ ...current, status: 'error', error: errorMessage(error, t, fallback) }))
    return null
  }
}

function classNames(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(' ')
}

function formatNumber(value: number | null | undefined, locale: 'en' | 'zh', digits = 1) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString(localeTag(locale), { maximumFractionDigits: digits, minimumFractionDigits: digits })
}

function formatDistance(value: number | null | undefined, locale: 'en' | 'zh') {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${formatNumber(value, locale)} km`
}

function formatMinutes(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const rounded = Math.max(0, Math.round(seconds))
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, '0')}`
}

function formatDuration(seconds: number | null | undefined, t: Translate) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const remainder = rounded % 60
  if (hours > 0) return `${t('time.hours', { count: hours })} ${String(minutes).padStart(2, '0')}${t('time.minutes', { count: '' })}`
  if (minutes > 0) return `${t('time.minutes', { count: minutes })} ${String(remainder).padStart(2, '0')}${t('time.seconds', { count: '' })}`
  return t('time.seconds', { count: remainder })
}

function formatPace(seconds: number | null | undefined, t: Translate) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds) || seconds <= 0) return '—'
  return `${formatMinutes(seconds)} ${t('time.perKm')}`
}

function formatDate(value: string, locale: 'en' | 'zh', withWeekday = false) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString(localeTag(locale), {
    month: 'short',
    day: 'numeric',
    ...(withWeekday ? { weekday: 'short' as const } : {}),
  })
}

function formatDateTime(value: string, locale: 'en' | 'zh') {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString(localeTag(locale), { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function toInputDateTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.slice(0, 16)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function todayIso() {
  const date = new Date()
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function shiftDate(dateText: string, amount: number) {
  const date = new Date(`${dateText}T12:00:00`)
  date.setDate(date.getDate() + amount)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function getViewFromHash(): ViewKey {
  const value = window.location.hash.replace(/^#/, '').split('/')[0] as ViewKey
  return VIEWS.some((item) => item.key === value) ? value : 'overview'
}

function getShareToken() {
  const match = window.location.pathname.match(/^\/share\/([^/]+)\/?$/)
  return match ? decodeURIComponent(match[1]) : null
}

function runTypeLabel(type: string, t: Translate) {
  const normalized = type.toLowerCase()
  return t(`types.${normalized === 'run' ? 'run' : normalized}`)
}

function runTypeClass(type: string) {
  const normalized = type.toLowerCase()
  return normalized === 'long' ? 'type-long' : normalized === 'tempo' ? 'type-tempo' : normalized === 'interval' ? 'type-interval' : normalized === 'race' ? 'type-race' : 'type-easy'
}

function sourceLabel(source: string | undefined, t: Translate) {
  const normalized = (source ?? '').toLowerCase()
  if (normalized.includes('demo') || normalized.includes('sample')) return t('runs.sourceDemo')
  if (normalized.includes('garmin')) return t('runs.sourceGarmin')
  if (normalized.includes('strava')) return t('runs.sourceStrava')
  return t('runs.sourceManual')
}

function shoeStatusLabel(status: string, t: Translate) {
  const normalized = status.toLowerCase()
  if (normalized === 'rotation') return t('shoes.statusRotation')
  if (normalized === 'retired') return t('shoes.statusRetired')
  return t('shoes.statusActive')
}

function shoeStatusClass(status: string) {
  const normalized = status.toLowerCase()
  return normalized === 'retired' ? 'status-retired' : normalized === 'rotation' ? 'status-rotation' : 'status-active'
}

function App() {
  const { t } = useI18n()
  const auth = useAuth()
  const shareToken = getShareToken()
  if (shareToken) return <PublicSharePage token={shareToken} />
  if (auth.loading) return <LoadingScreen />
  if (auth.error) return <StandaloneError message={t('errors.config')} />
  if (auth.config?.auth_required && !auth.session && !auth.serverAuthenticated) return <AuthGate />
  return <Dashboard />
}

function Dashboard() {
  const { locale, t, toggleLocale } = useI18n()
  const auth = useAuth()

  const [view, setView] = useState<ViewKey>(() => getViewFromHash())
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [runsState, setRunsState] = useState<ResourceState<Run[]>>(emptyResource)
  const [shoesState, setShoesState] = useState<ResourceState<Shoe[]>>(emptyResource)
  const [statsState, setStatsState] = useState<ResourceState<Stats>>(emptyResource)
  const [statsPeriod, setStatsPeriod] = useState<'week' | 'month'>('week')
  const [statsDate, setStatsDate] = useState(todayIso)
  const [selectedRunId, setSelectedRunId] = useState<RunId | null>(null)
  const [runForm, setRunForm] = useState<{ open: boolean; run: Run | null }>({ open: false, run: null })
  const [shoeForm, setShoeForm] = useState<{ open: boolean; shoe: Shoe | null }>({ open: false, shoe: null })
  const [seedState, setSeedState] = useState<'idle' | 'loading'>('idle')
  const [toast, setToast] = useState<Toast | null>(null)
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)
  const [inferenceOpen, setInferenceOpen] = useState(false)

  const runsRequestRef = useRef(0)
  const shoesRequestRef = useRef(0)
  const statsRequestRef = useRef(0)
  const refreshRuns = useCallback(() => loadResource(api.getRuns, setRunsState, runsRequestRef, t, 'errors.loadRuns'), [t])
  const refreshShoes = useCallback(() => loadResource(api.getShoes, setShoesState, shoesRequestRef, t, 'errors.loadShoes'), [t])
  const refreshStats = useCallback((period = statsPeriod, date = statsDate) => loadResource(() => api.getStats(period, date), setStatsState, statsRequestRef, t, 'errors.loadStats'), [statsDate, statsPeriod, t])

  useEffect(() => {
    const onHashChange = () => setView(getViewFromHash())
    window.addEventListener('hashchange', onHashChange)
    void refreshRuns()
    void refreshShoes()
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [refreshRuns, refreshShoes])

  useEffect(() => { void refreshStats(statsPeriod, statsDate) }, [refreshStats, statsDate, statsPeriod])

  useEffect(() => {
    if (!toast) return undefined
    const timeout = window.setTimeout(() => setToast(null), 4200)
    return () => window.clearTimeout(timeout)
  }, [toast])

  const navigate = useCallback((nextView: ViewKey) => {
    window.location.hash = nextView
    setView(nextView)
    setSidebarOpen(false)
  }, [])
  const showToast = useCallback((message: string, tone: Toast['tone'] = 'success') => setToast({ message, tone }), [])
  const selectedRun = useMemo(() => runsState.data?.find((run) => String(run.id) === String(selectedRunId)) ?? null, [runsState.data, selectedRunId])
  const pageMeta = VIEWS.find((item) => item.key === view) ?? VIEWS[0]

  const handleSeed = async () => {
    if (seedState === 'loading') return
    setSeedState('loading')
    try {
      await api.seedDemo()
      await Promise.allSettled([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)])
      showToast(t('overview.sampleNote'), 'info')
    } catch (error) {
      showToast(errorMessage(error, t), 'error')
    } finally {
      setSeedState('idle')
    }
  }

  const handleRunSaved = async (message: string) => {
    setRunForm({ open: false, run: null })
    await Promise.allSettled([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)])
    showToast(message)
  }

  const handleShoeSaved = async (message: string) => {
    setShoeForm({ open: false, shoe: null })
    await Promise.allSettled([refreshShoes(), refreshRuns()])
    showToast(message)
  }

  const handleDeleteRun = async (run: Run) => {
    if (!window.confirm(t('runs.deleteConfirm', { title: run.title || t('runs.unnamed') }))) return
    try {
      await api.deleteRun(run.id)
      setSelectedRunId(null)
      await Promise.allSettled([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)])
      showToast(t('runs.deleted'))
    } catch (error) {
      showToast(errorMessage(error, t), 'error')
    }
  }

  const openShare = useCallback((target: ShareTarget) => setShareTarget(target), [])
  const onInferenceApplied = async () => {
    setInferenceOpen(false)
    await Promise.allSettled([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)])
    showToast(t('shoes.applySuccess'))
  }

  return (
    <div className="app-shell">
      <Sidebar view={view} onNavigate={navigate} open={sidebarOpen} onClose={() => setSidebarOpen(false)} t={t} />
      {sidebarOpen && <button className="sidebar-scrim" aria-label={t('app.closeNavigation')} onClick={() => setSidebarOpen(false)} />}
      <div className="main-shell">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label={t('app.openNavigation')} onClick={() => setSidebarOpen(true)}><Icon name="menu" /></button>
          <div className="topbar-context">
            <span className="topbar-kicker">RUNWISE / {t(`nav.${pageMeta.key}`).toUpperCase()}</span>
            <h1>{t(`nav.${pageMeta.key}`)}</h1>
          </div>
          <div className="topbar-actions">
            <button className="language-toggle" onClick={toggleLocale} aria-label={t('actions.changeLanguage')}>{t('actions.changeLanguage')}</button>
            <span className="mode-badge"><span className="mode-dot" />{auth.config?.auth_required ? t('app.cloudMode') : t('app.localMode')}</span>
            {auth.session && <button className="text-button topbar-signout" onClick={() => void auth.signOut()}>{t('actions.signOut')}</button>}
            <button className="button button-primary topbar-add" onClick={() => setRunForm({ open: true, run: null })}><Icon name="plus" size={17} /><span>{t('actions.recordRun')}</span></button>
          </div>
        </header>

        <main className="page-content">
          {view === 'overview' && <OverviewView runs={runsState} shoes={shoesState} stats={statsState} period={statsPeriod} onPeriodChange={setStatsPeriod} onNavigate={navigate} onSeed={handleSeed} seedState={seedState} onSelectRun={(run) => { setSelectedRunId(run.id); navigate('runs') }} onShareLatest={() => runsState.data?.[0] && openShare({ kind: 'run', runId: runsState.data[0].id })} locale={locale} t={t} />}
          {view === 'stats' && <StatsView stats={statsState} period={statsPeriod} date={statsDate} onDateChange={setStatsDate} onPeriodChange={setStatsPeriod} onRetry={() => void refreshStats(statsPeriod, statsDate)} onShare={() => openShare({ kind: statsPeriod, date: statsDate })} locale={locale} t={t} />}
          {view === 'runs' && <RunsView runs={runsState} shoes={shoesState.data ?? []} selectedRun={selectedRun} onSelectRun={(run) => setSelectedRunId(run.id)} onRetry={() => void refreshRuns()} onNew={() => setRunForm({ open: true, run: null })} onEdit={(run) => setRunForm({ open: true, run })} onDelete={handleDeleteRun} onSeed={handleSeed} seedState={seedState} onShare={(run) => openShare({ kind: 'run', runId: run.id })} locale={locale} t={t} />}
          {view === 'shoes' && <ShoesView shoes={shoesState} runs={runsState.data ?? []} onRetry={() => void refreshShoes()} onNew={() => setShoeForm({ open: true, shoe: null })} onEdit={(shoe) => setShoeForm({ open: true, shoe })} onInfer={() => setInferenceOpen(true)} locale={locale} t={t} />}
          {view === 'checkin' && <CheckinView onToast={showToast} locale={locale} t={t} />}
          {view === 'settings' && <SettingsView onToast={showToast} onDataChanged={async () => { await Promise.allSettled([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)]) }} locale={locale} onToggleLocale={toggleLocale} t={t} />}
        </main>
      </div>

      {runForm.open && <RunFormModal run={runForm.run} shoes={shoesState.data ?? []} onClose={() => setRunForm({ open: false, run: null })} onSaved={handleRunSaved} t={t} />}
      {shoeForm.open && <ShoeFormModal shoe={shoeForm.shoe} onClose={() => setShoeForm({ open: false, shoe: null })} onSaved={handleShoeSaved} t={t} />}
      {inferenceOpen && <InferenceDialog onClose={() => setInferenceOpen(false)} onApplied={onInferenceApplied} locale={locale} t={t} />}
      {shareTarget && <ShareDialog target={shareTarget} onClose={() => setShareTarget(null)} locale={locale} t={t} />}
      {toast && <ToastMessage toast={toast} onClose={() => setToast(null)} t={t} />}
    </div>
  )
}

function Sidebar({ view, onNavigate, open, onClose, t }: { view: ViewKey; onNavigate: (view: ViewKey) => void; open: boolean; onClose: () => void; t: Translate }) {
  return (
    <aside className={classNames('sidebar', open && 'sidebar-open')}>
      <div className="brand-block">
        <div className="brand-mark"><span /><span /><span /></div>
        <div><div className="brand-name">{t('app.name')}</div><div className="brand-caption">{t('app.caption')}</div></div>
        <button className="icon-button sidebar-close" aria-label={t('app.closeNavigation')} onClick={onClose}><Icon name="close" size={18} /></button>
      </div>
      <div className="sidebar-label">{t('app.workspace')}</div>
      <nav className="side-nav" aria-label={t('app.workspace')}>
        {VIEWS.map((item) => <button key={item.key} className={classNames('nav-item', view === item.key && 'nav-item-active')} onClick={() => onNavigate(item.key)}><Icon name={item.icon} size={19} /><span>{t(`nav.${item.key}`)}</span>{item.key === 'overview' && <span className="nav-pulse" />}</button>)}
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-note"><div className="note-icon"><Icon name="spark" size={17} /></div><div><strong>{t('app.mottoTitle')}</strong><span>{t('app.mottoCopy')}</span></div></div>
      <div className="sidebar-footer"><div className="avatar">R</div><div className="profile-copy"><strong>{t('app.localAthlete')}</strong><span>{t('app.savedInWorkspace')}</span></div><Icon name="chevron-down" size={16} /></div>
    </aside>
  )
}

function OverviewView({ runs, shoes, stats, period, onPeriodChange, onNavigate, onSeed, seedState, onSelectRun, onShareLatest, locale, t }: { runs: ResourceState<Run[]>; shoes: ResourceState<Shoe[]>; stats: ResourceState<Stats>; period: 'week' | 'month'; onPeriodChange: (period: 'week' | 'month') => void; onNavigate: (view: ViewKey) => void; onSeed: () => void; seedState: 'idle' | 'loading'; onSelectRun: (run: Run) => void; onShareLatest: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const hasRuns = (runs.data?.length ?? 0) > 0
  const currentStats = stats.data
  return (
    <div className="view-stack overview-view">
      <section className="intro-row"><div><div className="eyebrow">{t('overview.eyebrow')}</div><h2>{hasRuns ? t('overview.headlineWithRuns') : t('overview.headlineEmpty')}</h2><p className="lede">{t('overview.lede')}</p></div><div className="intro-actions"><button className="button button-primary" onClick={() => onNavigate('runs')}><Icon name="plus" size={17} />{t('actions.recordRun')}</button><button className="button button-secondary" onClick={() => onNavigate('checkin')}><Icon name="sun" size={17} />{t('actions.todayStatus')}</button></div></section>
      {runs.status === 'loading' && !runs.data && <OverviewSkeleton />}
      {runs.status === 'error' && !runs.data && <ErrorState message={runs.error ?? t('errors.loadRuns')} onRetry={() => window.location.reload()} t={t} />}
      {runs.status === 'success' && !hasRuns && <EmptyDataHero onSeed={onSeed} loading={seedState === 'loading'} onRecord={() => onNavigate('runs')} t={t} />}
      {hasRuns && <>
        <section className="hero-panel"><div className="hero-copy"><span className="hero-kicker"><span className="live-dot" />{t('overview.weekRhythm')}</span><h3>{currentStats?.run_count ? t('overview.runsCompleted', { count: currentStats.run_count }) : t('overview.organizingRhythm')}</h3><p>{currentStats?.total_distance_km ? t('overview.distanceCount', { distance: formatDistance(currentStats.total_distance_km, locale) }) : t('overview.writeNext')}</p><div className="hero-action-row"><button className="text-button" onClick={() => onNavigate('stats')}>{t('actions.viewAnalysis')} <Icon name="arrow-right" size={16} /></button><button className="text-button" onClick={onShareLatest}>{t('actions.share')} <Icon name="external" size={15} /></button></div></div><div className="hero-graphic" aria-hidden="true"><div className="hero-ring hero-ring-one" /><div className="hero-ring hero-ring-two" /><div className="hero-runner"><Icon name="activity" size={30} /><span className="hero-label hero-label-top">{currentStats?.average_pace_seconds ? formatPace(currentStats.average_pace_seconds, t) : t('overview.stableForward')}</span><span className="hero-label hero-label-bottom">{t('overview.localRecord')}</span></div></div></section>
        <section className="metric-grid"><MetricCard label={t('overview.weeklyDistance')} value={currentStats ? formatNumber(currentStats.total_distance_km, locale) : '—'} unit="km" icon="activity" accent="green" detail={currentStats?.run_count ? t('overview.runsCount', { count: currentStats.run_count }) : t('overview.waitingForRecord')} /><MetricCard label={t('overview.averagePace')} value={currentStats?.average_pace_seconds ? formatMinutes(currentStats.average_pace_seconds) : '—'} unit={currentStats?.average_pace_seconds ? t('time.perKm') : ''} icon="zap" accent="amber" detail={t('overview.weightedByDistance')} /><MetricCard label={t('overview.trainingTime')} value={currentStats ? formatNumber(currentStats.total_duration_seconds / 3600, locale, 1) : '—'} unit={t('time.hours', { count: '' })} icon="clock" accent="blue" detail={t('overview.weeklyTotal')} /><MetricCard label={t('overview.shoeMileage')} value={shoes.data ? formatNumber(shoes.data.reduce((sum, shoe) => sum + (shoe.total_distance_km || 0), 0), locale) : '—'} unit="km" icon="shoe" accent="purple" detail={t('overview.gearCount', { count: shoes.data?.length ?? 0 })} /></section>
        <section className="content-grid overview-grid"><div className="card chart-card"><div className="card-heading"><div><span className="section-eyebrow">{t('overview.trainingTrend')}</span><h3>{t('overview.distanceDistribution')}</h3></div><PeriodSwitch period={period} onChange={onPeriodChange} t={t} /></div>{stats.status === 'loading' && !stats.data ? <ChartSkeleton /> : stats.status === 'error' && !stats.data ? <ErrorState compact message={stats.error ?? t('errors.loadStats')} t={t} /> : <StatsChart stats={stats.data} large={false} locale={locale} t={t} />}</div><div className="card recent-card"><div className="card-heading"><div><span className="section-eyebrow">{t('overview.recentRecords')}</span><h3>{t('overview.runLog')}</h3></div><button className="icon-button muted-icon" aria-label={t('actions.viewAllRuns')} onClick={() => onNavigate('runs')}><Icon name="arrow-up-right" size={18} /></button></div><RecentRuns runs={runs.data?.slice(0, 4) ?? []} onSelect={onSelectRun} locale={locale} t={t} /></div></section>
      </>}
      <section className="quiet-footer-row"><div className="source-caption"><span className="source-dot" />{t('overview.source')} <span>·</span> {t('overview.workspace')}</div><button className="text-button quiet-link" onClick={() => onNavigate('settings')}>{t('actions.manageSources')} <Icon name="arrow-right" size={15} /></button></section>
    </div>
  )
}

function OverviewSkeleton() { return <><div className="skeleton hero-skeleton" /><div className="metric-grid">{[1, 2, 3, 4].map((item) => <div className="skeleton metric-skeleton" key={item} />)}</div><div className="content-grid overview-grid"><div className="skeleton panel-skeleton" /><div className="skeleton panel-skeleton" /></div></> }

function EmptyDataHero({ onSeed, loading, onRecord, t }: { onSeed: () => void; loading: boolean; onRecord: () => void; t: Translate }) {
  return <section className="empty-hero"><div className="empty-orbit" aria-hidden="true"><div className="empty-orbit-core"><Icon name="activity" size={30} /></div><span /><span /><span /></div><div className="empty-copy"><span className="empty-kicker">{t('overview.firstMile')}</span><h3>{t('overview.noRuns')}</h3><p>{t('overview.emptyCopy')}</p><div className="empty-actions"><button className="button button-primary" onClick={onSeed} disabled={loading}>{loading ? <><span className="button-spinner" />{t('actions.loading')}</> : <><Icon name="spark" size={17} />{t('actions.loadDemo')}</>}</button><button className="button button-secondary" onClick={onRecord}>{t('actions.recordManually')}</button></div><span className="empty-note"><Icon name="info" size={15} />{t('overview.sampleNote')}</span></div></section>
}

function MetricCard({ label, value, unit, icon, accent, detail }: { label: string; value: string; unit: string; icon: IconName; accent: string; detail: string }) { return <div className="metric-card"><div className={classNames('metric-icon', `metric-${accent}`)}><Icon name={icon} size={18} /></div><div className="metric-label">{label}</div><div className="metric-value">{value}<small>{unit}</small></div><div className="metric-detail">{detail}</div></div> }

function PeriodSwitch({ period, onChange, large = false, t }: { period: 'week' | 'month'; onChange: (period: 'week' | 'month') => void; large?: boolean; t: Translate }) { return <div className={classNames('period-switch', large && 'period-switch-large')} role="group" aria-label={t('stats.period')}><button className={period === 'week' ? 'period-active' : ''} onClick={() => onChange('week')}>{t('stats.week')}</button><button className={period === 'month' ? 'period-active' : ''} onClick={() => onChange('month')}>{t('stats.month')}</button></div> }

function StatsChart({ stats, large = false, locale, t }: { stats: Stats | null; large?: boolean; locale: 'en' | 'zh'; t: Translate }) {
  const buckets = stats?.buckets ?? []
  const max = Math.max(...buckets.map((bucket) => bucket.distance_km), 1)
  if (!stats) return <InlineEmpty title={t('stats.noTraining')} copy={t('stats.completeRun')} />
  if (!buckets.length) return <InlineEmpty title={t('stats.noTraining')} copy={t('stats.completeRun')} />
  return <div className={classNames('stats-chart', large && 'stats-chart-large')} aria-label={t('stats.chartLabel')}><div className="chart-y-labels"><span>{formatNumber(max, locale, 0)} km</span><span>{formatNumber(max / 2, locale, 0)} km</span><span>0 km</span></div><div className="chart-plot"><div className="chart-gridline chart-gridline-top" /><div className="chart-gridline chart-gridline-middle" /><div className="chart-gridline chart-gridline-bottom" />{buckets.map((bucket, index) => { const height = Math.max(bucket.distance_km / max * 100, bucket.distance_km > 0 ? 8 : 3); return <div className="chart-bar-wrap" key={`${bucket.label}-${index}`}><div className="chart-tooltip">{formatDistance(bucket.distance_km, locale)}<span>{bucket.run_count}</span></div><div className="chart-bar" style={{ height: `${height}%` }}><span className="chart-bar-glow" /></div><span className="chart-x-label">{bucket.label}</span></div> })}</div></div>
}

function ChartSkeleton() { return <div className="chart-skeleton"><div /><div /><div /><div /><div /></div> }

function RecentRuns({ runs, onSelect, locale, t }: { runs: Run[]; onSelect: (run: Run) => void; locale: 'en' | 'zh'; t: Translate }) {
  if (!runs.length) return <InlineEmpty title={t('overview.noRecent')} copy={t('overview.nextRunAppears')} />
  return <div className="recent-list">{runs.map((run) => <button className="recent-run" key={String(run.id)} onClick={() => onSelect(run)}><span className={classNames('run-type-mark', runTypeClass(run.run_type))}><Icon name={runTypeClass(run.run_type) === 'type-long' ? 'arrow-up-right' : 'activity'} size={16} /></span><span className="recent-main"><strong>{run.title || t('runs.unnamed')}</strong><small>{formatDate(run.started_at, locale, true)} · {runTypeLabel(run.run_type, t)}</small></span><span className="recent-metrics"><strong>{formatNumber(run.distance_km, locale)} <small>km</small></strong><small>{formatPace(run.distance_km > 0 ? run.duration_seconds / run.distance_km : null, t)}</small></span><Icon name="chevron-right" size={17} /></button>)}</div>
}

function StatsView({ stats, period, date, onDateChange, onPeriodChange, onRetry, onShare, locale, t }: { stats: ResourceState<Stats>; period: 'week' | 'month'; date: string; onDateChange: (date: string) => void; onPeriodChange: (period: 'week' | 'month') => void; onRetry: () => void; onShare: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const data = stats.data
  return <div className="view-stack"><section className="page-intro-row"><div><div className="eyebrow">{t('stats.eyebrow')}</div><h2>{t('stats.headline')}</h2><p className="lede">{t('stats.lede')}</p></div><div className="stats-controls"><label className="stats-date-field"><span>{t('stats.date')}</span><input type="date" value={date} onChange={(event) => onDateChange(event.target.value)} /></label><PeriodSwitch period={period} onChange={onPeriodChange} large t={t} /><button className="button button-secondary" onClick={onShare}><Icon name="external" size={15} />{t('actions.share')}</button></div></section>{stats.status === 'loading' && !data && <><div className="metric-grid">{[1, 2, 3, 4].map((item) => <div className="skeleton metric-skeleton" key={item} />)}</div><div className="skeleton tall-skeleton" /></>}{stats.status === 'error' && !data && <ErrorState message={stats.error ?? t('errors.loadStats')} onRetry={onRetry} t={t} />}{data && <><section className="metric-grid stats-metric-grid"><MetricCard label={t('stats.totalDistance')} value={formatNumber(data.total_distance_km, locale)} unit="km" icon="activity" accent="green" detail={t('stats.nearDays', { count: period === 'week' ? 7 : 30 })} /><MetricCard label={t('stats.runCount')} value={String(data.run_count)} unit={t('stats.count')} icon="chart" accent="amber" detail={t('stats.completedStarts')} /><MetricCard label={t('stats.totalDuration')} value={formatNumber(data.total_duration_seconds / 3600, locale)} unit={t('time.hours', { count: '' })} icon="clock" accent="blue" detail={t('stats.trainingTime')} /><MetricCard label={t('overview.averagePace')} value={data.average_pace_seconds ? formatMinutes(data.average_pace_seconds) : '—'} unit={data.average_pace_seconds ? t('time.perKm') : ''} icon="zap" accent="purple" detail={t('overview.weightedByDistance')} /></section><section className="card large-chart-card"><div className="card-heading"><div><span className="section-eyebrow">{t('stats.distanceRunCount')}</span><h3>{t('stats.distribution')}</h3></div><span className="data-source-tag"><span className="source-dot" />{t('stats.liveApi')}</span></div><StatsChart stats={data} large locale={locale} t={t} /></section><section className="card bucket-card"><div className="card-heading"><div><span className="section-eyebrow">{t('stats.breakdown')}</span><h3>{t('stats.segments')}</h3></div><span className="muted-caption">{t('stats.fromApi')}</span></div>{data.buckets.length > 0 ? <div className="bucket-table"><div className="bucket-row bucket-header"><span>{t('stats.bucket')}</span><span>{t('stats.distance')}</span><span>{t('stats.count')}</span><span>{t('stats.averagePerRun')}</span></div>{data.buckets.map((bucket) => <div className="bucket-row" key={bucket.label}><span className="bucket-label">{bucket.label}</span><span>{formatDistance(bucket.distance_km, locale)}</span><span>{bucket.run_count}</span><span>{bucket.run_count ? formatDistance(bucket.distance_km / bucket.run_count, locale) : '—'}</span></div>)}</div> : <InlineEmpty title={t('stats.noBuckets')} copy={t('stats.completeRun')} />}</section></>}</div>
}

function RunsView({ runs, shoes, selectedRun, onSelectRun, onRetry, onNew, onEdit, onDelete, onSeed, seedState, onShare, locale, t }: { runs: ResourceState<Run[]>; shoes: Shoe[]; selectedRun: Run | null; onSelectRun: (run: Run) => void; onRetry: () => void; onNew: () => void; onEdit: (run: Run) => void; onDelete: (run: Run) => void; onSeed: () => void; seedState: 'idle' | 'loading'; onShare: (run: Run) => void; locale: 'en' | 'zh'; t: Translate }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const list = useMemo(() => {
    const all = runs.data ?? []
    return all.filter((run) => {
      const search = query.trim().toLowerCase()
      const matchesQuery = !search || `${run.title} ${run.notes} ${run.run_type}`.toLowerCase().includes(search)
      const matchesFilter = filter === 'all' || run.run_type.toLowerCase() === filter
      return matchesQuery && matchesFilter
    }).sort((a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime())
  }, [filter, query, runs.data])
  const shoeMap = useMemo(() => new Map(shoes.map((shoe) => [String(shoe.id), shoe.name])), [shoes])
  return <div className="view-stack"><section className="page-intro-row"><div><div className="eyebrow">{t('runs.eyebrow')}</div><h2>{t('runs.headline')}</h2><p className="lede">{t('runs.lede')}</p></div><button className="button button-primary" onClick={onNew}><Icon name="plus" size={17} />{t('actions.recordRun')}</button></section>{runs.status === 'loading' && !runs.data && <div className="skeleton table-skeleton" />}{runs.status === 'error' && !runs.data && <ErrorState message={runs.error ?? t('errors.loadRuns')} onRetry={onRetry} t={t} />}{runs.status === 'success' && !runs.data?.length && <EmptyDataHero onSeed={onSeed} loading={seedState === 'loading'} onRecord={onNew} t={t} />}{runs.data && runs.data.length > 0 && <section className="runs-layout"><div className="card runs-card"><div className="list-toolbar"><div className="search-field"><span className="search-symbol">⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('runs.searchPlaceholder')} aria-label={t('runs.searchLabel')} /></div><select value={filter} onChange={(event) => setFilter(event.target.value)} aria-label={t('runs.filterLabel')}><option value="all">{t('runs.allTypes')}</option>{RUN_TYPE_VALUES.map((value) => <option value={value} key={value}>{runTypeLabel(value, t)}</option>)}</select></div>{list.length ? <div className="run-table"><div className="run-row run-row-header"><span>{t('runs.run')}</span><span>{t('runs.distance')}</span><span>{t('runs.durationPace')}</span><span>{t('runs.source')}</span><span /></div>{list.map((run) => <button className={classNames('run-row', selectedRun && String(selectedRun.id) === String(run.id) && 'run-row-selected')} key={String(run.id)} onClick={() => onSelectRun(run)}><span className="run-name-cell"><span className={classNames('run-type-mark', runTypeClass(run.run_type))}><Icon name="activity" size={16} /></span><span><strong>{run.title || t('runs.unnamed')}</strong><small>{formatDateTime(run.started_at, locale)} · {runTypeLabel(run.run_type, t)}</small></span></span><span className="table-number">{formatDistance(run.distance_km, locale)}</span><span className="run-duration-cell"><strong>{formatDuration(run.duration_seconds, t)}</strong><small>{formatPace(run.distance_km > 0 ? run.duration_seconds / run.distance_km : null, t)}</small></span><span className="source-label">{sourceLabel(run.source, t)}</span><span><Icon name="chevron-right" size={17} /></span></button>)}</div> : <InlineEmpty title={t('runs.noMatch')} copy={t('runs.tryDifferent')} />}</div><RunDetailPanel run={selectedRun} shoeName={selectedRun?.shoe_id ? shoeMap.get(String(selectedRun.shoe_id)) : undefined} onEdit={onEdit} onDelete={onDelete} onShare={onShare} locale={locale} t={t} /></section>}</div>
}

function RunDetailPanel({ run, shoeName, onEdit, onDelete, onShare, locale, t }: { run: Run | null; shoeName?: string; onEdit: (run: Run) => void; onDelete: (run: Run) => void; onShare: (run: Run) => void; locale: 'en' | 'zh'; t: Translate }) {
  const [analysisState, setAnalysisState] = useState<ResourceState<Analysis>>(emptyResource)
  const [streamsState, setStreamsState] = useState<ResourceState<RunStreams>>(emptyResource)
  const [importing, setImporting] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const requestRef = useRef(0)
  const loadStreams = useCallback(async (runId: RunId) => {
    const requestId = ++requestRef.current
    setStreamsState((current) => ({ ...current, status: 'loading', error: null }))
    try {
      const data = await api.getRunStreams(runId)
      if (requestId === requestRef.current) setStreamsState({ data, status: 'success', error: null })
    } catch (error) {
      if (requestId !== requestRef.current) return
      if (error instanceof ApiError && error.status === 404) setStreamsState({ data: { available: false, samples: [], laps: [] }, status: 'success', error: null })
      else setStreamsState({ data: null, status: 'error', error: errorMessage(error, t) })
    }
  }, [t])
  useEffect(() => {
    let active = true
    if (!run) { setAnalysisState(emptyResource()); setStreamsState(emptyResource()); return undefined }
    setAnalysisState({ data: null, status: 'loading', error: null })
    setStreamsState({ data: null, status: 'loading', error: null })
    void api.getAnalysis(run.id).then((data) => { if (active) setAnalysisState({ data, status: 'success', error: null }) }).catch((error) => { if (active) setAnalysisState({ data: null, status: 'error', error: errorMessage(error, t) }) })
    void loadStreams(run.id)
    return () => { active = false; requestRef.current += 1 }
  }, [loadStreams, run, t])
  const importStreams = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !run) return
    setImporting(true)
    try {
      const parsed = JSON.parse(await file.text()) as RunStreams
      await api.putRunStreams(run.id, prepareImportedStreams(parsed, t('errors.streamsInvalid')))
      await loadStreams(run.id)
    } catch (error) {
      setStreamsState((current) => ({ ...current, status: 'error', error: errorMessage(error, t) }))
    } finally {
      setImporting(false)
    }
  }
  if (!run) return <aside className="detail-empty"><div className="detail-empty-icon"><Icon name="activity" size={21} /></div><strong>{t('runs.selectRun')}</strong><span>{t('runs.selectRunCopy')}</span></aside>
  return <aside className="card run-detail"><div className="detail-top"><span className={classNames('type-pill', runTypeClass(run.run_type))}>{runTypeLabel(run.run_type, t)}</span><div className="detail-actions"><button className="icon-button" aria-label={t('actions.share')} onClick={() => onShare(run)}><Icon name="external" size={17} /></button><button className="icon-button" aria-label={t('actions.edit')} onClick={() => onEdit(run)}><Icon name="edit" size={17} /></button><button className="icon-button danger-icon" aria-label={t('actions.delete')} onClick={() => onDelete(run)}><Icon name="trash" size={17} /></button></div></div><h3>{run.title || t('runs.unnamed')}</h3><p className="detail-date"><Icon name="calendar" size={15} />{formatDateTime(run.started_at, locale)}</p><div className="detail-stat-grid"><div><span>{t('runs.distanceStat')}</span><strong>{formatDistance(run.distance_km, locale)}</strong></div><div><span>{t('runs.timeStat')}</span><strong>{formatDuration(run.duration_seconds, t)}</strong></div><div><span>{t('runs.paceStat')}</span><strong>{formatPace(run.distance_km > 0 ? run.duration_seconds / run.distance_km : null, t)}</strong></div><div><span>{t('runs.heartRate')}</span><strong>{run.avg_hr ? `${Math.round(run.avg_hr)} bpm` : '—'}</strong></div></div>{(run.moving_seconds != null || run.elapsed_seconds != null || run.stopped_seconds != null) && <div className="detail-time-row"><span>{t('runs.movingTime')} <strong>{formatDuration(run.moving_seconds, t)}</strong></span><span>{t('runs.elapsedTime')} <strong>{formatDuration(run.elapsed_seconds, t)}</strong></span><span>{t('runs.stoppedTime')} <strong>{formatDuration(run.stopped_seconds, t)}</strong></span></div>}<div className="detail-meta-row"><span>{t('runs.source')}</span><strong>{sourceLabel(run.source, t)}</strong></div>{shoeName && <div className="detail-meta-row"><span>{t('runs.shoe')}</span><strong><Icon name="shoe" size={14} />{shoeName}</strong></div>}{run.rpe && <div className="detail-meta-row"><span>{t('runs.effort')}</span><strong>{run.rpe} / 10</strong></div>}{run.notes && <div className="detail-notes"><span>{t('runs.trainingNotes')}</span><p>{run.notes}</p></div>}<div className="analysis-block"><div className="analysis-heading"><span className="analysis-icon"><Icon name="spark" size={15} /></span><div><span>{t('runs.analysis')}</span><strong>{t('runs.howWasIt')}</strong></div></div>{analysisState.status === 'loading' && <div className="analysis-loading"><span /><span /><span /></div>}{analysisState.status === 'error' && <p className="analysis-error">{analysisState.error}</p>}{analysisState.data && <><p className="analysis-summary">{analysisState.data.summary}</p>{analysisState.data.observations.length > 0 && <ul>{analysisState.data.observations.map((observation, index) => <li key={`${observation}-${index}`}><Icon name="check" size={14} />{observation}</li>)}</ul>}</>}</div><div className="streams-block"><div className="streams-heading"><div><span className="section-eyebrow">{t('runs.streams')}</span><strong>{streamsState.data?.samples?.length ? t('runs.samples', { count: streamsState.data.samples.length }) : t('runs.noStreams')}</strong></div><label className="text-button"><Icon name="upload" size={15} />{importing ? t('actions.loading') : t('runs.importStreams')}<input ref={fileRef} type="file" accept="application/json,.json" hidden onChange={importStreams} /></label></div>{streamsState.status === 'loading' && <div className="analysis-loading"><span /><span /><span /></div>}{streamsState.status === 'error' && <p className="analysis-error">{streamsState.error}</p>}{streamsState.status === 'success' && streamsState.data && <RunStreamsView run={run} streams={streamsState.data} locale={locale} t={t} />}</div></aside>
}

function finiteNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function streamAnalysis(streams: RunStreams) {
  const nested = streams.analysis && typeof streams.analysis === 'object' ? streams.analysis : {}
  return { ...streams, ...nested } as Record<string, unknown>
}

function analysisArray(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object')) : []
}

function normalizeLap(lap: RunLap, index: number) {
  const distanceKm = finiteNumber(lap.distance_km) ?? (finiteNumber(lap.distance_m) == null ? null : (finiteNumber(lap.distance_m) as number) / 1000)
  const intervalDuration = finiteNumber(lap.duration_seconds) ?? finiteNumber(lap.elapsed_seconds) ?? ((finiteNumber(lap.end_seconds) ?? 0) - (finiteNumber(lap.start_seconds) ?? 0))
  return {
    label: lap.label ?? lap.lap ?? lap.split ?? index + 1,
    distanceKm,
    duration: intervalDuration > 0 ? intervalDuration : null,
    pace: finiteNumber(lap.pace_seconds),
  }
}

function RunStreamsView({ run, streams, locale, t }: { run: Run; streams: RunStreams; locale: 'en' | 'zh'; t: Translate }) {
  const samples = streams.samples ?? []
  const analysis = streamAnalysis(streams)
  const laps = streams.laps ?? []
  const available = streams.available ?? streams.stream_available ?? run.stream_available ?? run.streams_available ?? samples.length > 0
  const qualityFlags = Array.isArray(analysis.quality_flags) ? analysis.quality_flags.filter((flag): flag is string => typeof flag === 'string') : []
  const analysisSplits = analysisArray(analysis.splits)
  const analysisIntervals = analysisArray(analysis.intervals)
  const displayLaps = laps.length > 0 ? laps : analysisSplits as RunLap[]
  const elapsed = finiteNumber(analysis.elapsed_seconds)
  const moving = finiteNumber(analysis.moving_seconds)
  const stopped = finiteNumber(analysis.stopped_seconds)
  const movingPace = finiteNumber(analysis.moving_pace_seconds)
  const distanceKm = finiteNumber(analysis.distance_m) == null ? null : (finiteNumber(analysis.distance_m) as number) / 1000
  if (!available) return <p className="streams-empty">{t('runs.streamUnavailable')}</p>
  return <div className="streams-content"><div className="streams-summary"><span className="section-eyebrow">{t('runs.activitySummary')}</span><div className="stream-analysis-grid">{distanceKm != null && <span><small>{t('runs.distanceStat')}</small><strong>{formatDistance(distanceKm, locale)}</strong></span>}{elapsed != null && <span><small>{t('runs.elapsedTime')}</small><strong>{formatDuration(elapsed, t)}</strong></span>}{moving != null && <span><small>{t('runs.movingTime')}</small><strong>{formatDuration(moving, t)}</strong></span>}{stopped != null && <span><small>{t('runs.stoppedTime')}</small><strong>{formatDuration(stopped, t)}</strong></span>}{movingPace != null && <span><small>{t('runs.paceStat')}</small><strong>{formatPace(movingPace, t)}</strong></span>}</div></div>{samples.length >= 2 ? <StreamChart samples={samples} t={t} /> : <p className="streams-empty">{t('runs.streamUnavailable')}</p>}{qualityFlags.length > 0 && <div className="quality-flags"><span>{t('runs.qualityFlags')}</span>{qualityFlags.map((flag) => <em key={flag}>{flag}</em>)}</div>}{displayLaps.length > 0 && <div className="lap-table"><div className="lap-header"><span>{t('runs.split')}</span><span>{t('runs.distance')}</span><span>{t('runs.durationPace')}</span></div>{displayLaps.map((lap, index) => { const normalized = normalizeLap(lap, index); return <div className="lap-row" key={`${String(normalized.label)}-${index}`}><span>{normalized.label}</span><span>{normalized.distanceKm == null ? '—' : formatDistance(normalized.distanceKm, locale)}</span><span>{normalized.duration == null ? '—' : `${formatDuration(normalized.duration, t)} · ${formatPace(normalized.pace, t)}`}</span></div> })}</div>}{analysisIntervals.length > 0 && <div className="intervals-block"><div className="section-eyebrow">{t('runs.intervals')}</div><div className="interval-list">{analysisIntervals.map((interval, index) => { const label = typeof interval.label === 'string' ? interval.label : `${t('runs.intervals')} ${index + 1}`; const duration = finiteNumber(interval.elapsed_seconds); const intervalDistance = finiteNumber(interval.distance_m); return <div className="interval-row" key={`${label}-${index}`}><span>{label}</span><span>{duration == null ? '—' : formatDuration(duration, t)}</span><span>{intervalDistance == null ? '—' : formatDistance(intervalDistance / 1000, locale)}</span></div> })}</div></div>}{analysisIntervals.length === 0 && analysisSplits.length === 0 && displayLaps.length === 0 && <p className="streams-empty">{t('runs.noIntervals')}</p>}</div>
}

function StreamChart({ samples, t }: { samples: RunStreamSample[]; t: Translate }) {
  const paceSamples: Array<{ x: number; y: number }> = []
  let previous: { distance: number; elapsed: number } | null = null
  samples.forEach((sample, index) => {
    const distance = finiteNumber(sample.distance_km) ?? (finiteNumber(sample.distance_m) == null ? null : (finiteNumber(sample.distance_m) as number) / 1000)
    const elapsed = finiteNumber(sample.elapsed_seconds)
    const directPace = finiteNumber(sample.pace_seconds)
    if (distance != null && directPace != null && directPace > 0) paceSamples.push({ x: distance, y: directPace })
    else if (distance != null && elapsed != null && previous && distance > previous.distance && elapsed > previous.elapsed) paceSamples.push({ x: distance, y: (elapsed - previous.elapsed) / (distance - previous.distance) })
    else if (directPace != null && directPace > 0) paceSamples.push({ x: index, y: directPace })
    if (distance != null && elapsed != null) previous = { distance, elapsed }
  })
  if (paceSamples.length < 2) return <p className="streams-empty">{t('runs.streamUnavailable')}</p>
  const minX = Math.min(...paceSamples.map((sample) => sample.x))
  const maxX = Math.max(...paceSamples.map((sample) => sample.x), minX + 1)
  const minY = Math.min(...paceSamples.map((sample) => sample.y))
  const maxY = Math.max(...paceSamples.map((sample) => sample.y), minY + 1)
  const points = paceSamples.map((sample) => `${8 + ((sample.x - minX) / (maxX - minX)) * 184},${8 + ((sample.y - minY) / (maxY - minY)) * 54}`).join(' ')
  return <div className="stream-chart"><svg viewBox="0 0 200 70" role="img" aria-label={t('runs.streams')} preserveAspectRatio="none"><path d="M8 8H192M8 62H192" className="stream-grid" /><polyline points={points} className="stream-line" /></svg></div>
}

function prepareImportedStreams(value: unknown, invalidMessage: string): RunStreams {
  const record = value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
  const rawSamples = Array.isArray(value) ? value : Array.isArray(record.samples) ? record.samples : []
  const samples = rawSamples.map((item) => {
    const sample = item && typeof item === 'object' ? item as Record<string, unknown> : {}
    const elapsed = finiteNumber(sample.elapsed_seconds)
    const distanceM = finiteNumber(sample.distance_m) ?? (finiteNumber(sample.distance_km) == null ? null : (finiteNumber(sample.distance_km) as number) * 1000)
    if (elapsed == null || distanceM == null) throw new Error(invalidMessage)
    return { elapsed_seconds: elapsed, distance_m: distanceM, heart_rate: finiteNumber(sample.heart_rate), latitude: finiteNumber(sample.latitude) ?? finiteNumber(sample.lat), longitude: finiteNumber(sample.longitude) ?? finiteNumber(sample.lon) }
  })
  if (samples.length === 0) throw new Error(invalidMessage)
  const rawLaps = Array.isArray(record.laps) ? record.laps : []
  const laps = rawLaps.map((item) => {
    const lap = item && typeof item === 'object' ? item as Record<string, unknown> : {}
    const distanceM = finiteNumber(lap.distance_m) ?? (finiteNumber(lap.distance_km) == null ? null : (finiteNumber(lap.distance_km) as number) * 1000)
    const start = finiteNumber(lap.start_seconds) ?? finiteNumber(lap.start)
    const end = finiteNumber(lap.end_seconds) ?? finiteNumber(lap.end)
    if (start == null || end == null) throw new Error(invalidMessage)
    return { start_seconds: start, end_seconds: end, distance_m: distanceM, elapsed_seconds: finiteNumber(lap.elapsed_seconds) ?? finiteNumber(lap.duration_seconds), moving_seconds: finiteNumber(lap.moving_seconds) ?? finiteNumber(lap.active_duration_seconds), pace_seconds: finiteNumber(lap.pace_seconds), label: typeof lap.label === 'string' ? lap.label : null }
  })
  return { samples, laps }
}

type RunFormValues = { title: string; started_at: string; original_started_at: string | null; distance_km: string; duration_minutes: string; original_duration_seconds: number | null; run_type: string; avg_hr: string; shoe_id: string; notes: string; rpe: string }

function defaultRunValues(run: Run | null): RunFormValues {
  return { title: run?.title ?? '', started_at: run ? toInputDateTime(run.started_at) : toInputDateTime(new Date().toISOString()), original_started_at: run?.started_at ?? null, distance_km: run ? String(run.distance_km) : '', duration_minutes: run ? String(Number((run.duration_seconds / 60).toFixed(2))) : '', original_duration_seconds: run?.duration_seconds ?? null, run_type: run?.run_type ?? 'easy', avg_hr: run?.avg_hr == null ? '' : String(run.avg_hr), shoe_id: run?.shoe_id == null ? '' : String(run.shoe_id), notes: run?.notes ?? '', rpe: run?.rpe == null ? '' : String(run.rpe) }
}

function RunFormModal({ run, shoes, onClose, onSaved, t }: { run: Run | null; shoes: Shoe[]; onClose: () => void; onSaved: (message: string) => Promise<void>; t: Translate }) {
  const [values, setValues] = useState<RunFormValues>(() => defaultRunValues(run))
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  useEffect(() => setValues(defaultRunValues(run)), [run])
  const update = (field: keyof RunFormValues, value: string) => setValues((current) => ({ ...current, [field]: value }))
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setFormError(null)
    const distance = Number(values.distance_km)
    const durationMinutes = Number(values.duration_minutes)
    if (!values.title.trim()) return setFormError(t('errors.nameRequired'))
    if (!values.started_at) return setFormError(t('errors.startRequired'))
    const parsedStartedAt = new Date(values.started_at)
    if (Number.isNaN(parsedStartedAt.getTime())) return setFormError(t('errors.startInvalid'))
    if (!Number.isFinite(distance) || distance <= 0) return setFormError(t('errors.distancePositive'))
    if (!Number.isFinite(durationMinutes) || durationMinutes <= 0) return setFormError(t('errors.durationPositive'))
    const shoeId = values.shoe_id ? (/^\d+$/.test(values.shoe_id) ? Number(values.shoe_id) : values.shoe_id) : null
    const originalTimeInput = values.original_started_at ? toInputDateTime(values.original_started_at) : null
    const startedAt = run && values.original_started_at && values.started_at === originalTimeInput ? values.original_started_at : parsedStartedAt.toISOString()
    const originalMinutes = values.original_duration_seconds == null ? null : String(Number((values.original_duration_seconds / 60).toFixed(2)))
    const durationSeconds = run && values.original_duration_seconds != null && values.duration_minutes === originalMinutes ? values.original_duration_seconds : Math.round(durationMinutes * 60)
    const payload: RunPayload = { title: values.title.trim(), started_at: startedAt, distance_km: distance, duration_seconds: durationSeconds, run_type: values.run_type, avg_hr: values.avg_hr ? Number(values.avg_hr) : null, shoe_id: shoeId as RunId | null, notes: values.notes.trim(), rpe: values.rpe ? Number(values.rpe) : null, source: run?.source ?? 'manual' }
    setSaving(true)
    try {
      if (run) { await api.updateRun(run.id, payload); await onSaved(t('runs.updated')) } else { await api.createRun(payload); await onSaved(t('runs.saved')) }
    } catch (error) {
      setFormError(errorMessage(error, t))
    } finally {
      setSaving(false)
    }
  }
  const shoeLocked = Boolean(run && (run.shoe_assignment === 'manual' || (run.source.toLowerCase() === 'manual' && run.shoe_id != null)))
  return <Modal title={run ? t('runs.editRun') : t('runs.newRun')} eyebrow={run ? t('runs.editRun') : t('runs.newRun')} onClose={onClose} t={t}><form className="modal-form" onSubmit={submit}><div className="form-grid form-grid-two"><Field label={t('runs.name')} required t={t}><input value={values.title} onChange={(event) => update('title', event.target.value)} placeholder={t('runs.namePlaceholder')} /></Field><Field label={t('runs.runType')} t={t}><select value={values.run_type} onChange={(event) => update('run_type', event.target.value)}>{RUN_TYPE_VALUES.map((value) => <option key={value} value={value}>{runTypeLabel(value, t)}</option>)}</select></Field><Field label={t('runs.startedAt')} required t={t}><input type="datetime-local" value={values.started_at} onChange={(event) => update('started_at', event.target.value)} /></Field><Field label={t('runs.distanceKm')} required t={t}><input type="number" min="0.001" step="any" value={values.distance_km} onChange={(event) => update('distance_km', event.target.value)} placeholder="5.0" /></Field><Field label={t('runs.durationMinutes')} required hint={t('runs.durationHint')} t={t}><input type="number" min="0.1" step="0.01" value={values.duration_minutes} onChange={(event) => update('duration_minutes', event.target.value)} placeholder="32" /></Field><Field label={t('runs.heartRateInput')} hint={t('runs.heartRateHint')} t={t}><input type="number" min="20" max="260" value={values.avg_hr} onChange={(event) => update('avg_hr', event.target.value)} placeholder="145" /></Field><Field label={t('runs.shoe')} hint={shoeLocked ? t('runs.shoeLocked') : t('runs.shoeOptional')} t={t}><select value={values.shoe_id} disabled={shoeLocked} onChange={(event) => update('shoe_id', event.target.value)}><option value="">{t('runs.noShoe')}</option>{shoes.map((shoe) => <option value={String(shoe.id)} key={String(shoe.id)}>{shoe.brand ? `${shoe.brand} · ` : ''}{shoe.name}</option>)}</select></Field><Field label={t('runs.effortInput')} hint={t('runs.effortHint')} t={t}><input type="number" min="1" max="10" value={values.rpe} onChange={(event) => update('rpe', event.target.value)} placeholder="6" /></Field></div><Field label={t('runs.notes')} t={t}><textarea rows={3} value={values.notes} onChange={(event) => update('notes', event.target.value)} placeholder={t('runs.notesPlaceholder')} /></Field>{formError && <div className="form-error"><Icon name="info" size={16} />{formError}</div>}<div className="modal-footer"><button className="button button-secondary" type="button" onClick={onClose}>{t('actions.cancel')}</button><button className="button button-primary" type="submit" disabled={saving}>{saving ? <><span className="button-spinner" />{t('actions.loading')}</> : run ? t('actions.saveChanges') : t('actions.save')}</button></div></form></Modal>
}

function ShoeImage({ src, alt, t, className = '' }: { src?: string | null; alt: string; t: Translate; className?: string }) {
  const [failed, setFailed] = useState(false)
  if (!src || failed) return <div className={classNames('shoe-image-fallback', className)} aria-label={t('shoes.fallback')}><Icon name="shoe" size={53} /></div>
  return <img className={classNames('shoe-image', className)} src={src} alt={alt} onError={() => setFailed(true)} />
}

function ShoesView({ shoes, runs, onRetry, onNew, onEdit, onInfer, locale, t }: { shoes: ResourceState<Shoe[]>; runs: Run[]; onRetry: () => void; onNew: () => void; onEdit: (shoe: Shoe) => void; onInfer: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const runCountByShoe = useMemo(() => {
    const counts = new Map<string, number>()
    runs.forEach((run) => { if (run.shoe_id != null) counts.set(String(run.shoe_id), (counts.get(String(run.shoe_id)) ?? 0) + 1) })
    return counts
  }, [runs])
  return <div className="view-stack"><section className="page-intro-row"><div><div className="eyebrow">{t('shoes.eyebrow')}</div><h2>{t('shoes.headline')}</h2><p className="lede">{t('shoes.lede')}</p></div><div className="intro-actions"><button className="button button-secondary" onClick={onInfer}><Icon name="spark" size={16} />{t('shoes.inference')}</button><button className="button button-primary" onClick={onNew}><Icon name="plus" size={17} />{t('actions.addShoe')}</button></div></section>{shoes.status === 'loading' && !shoes.data && <div className="shoe-grid">{[1, 2, 3].map((item) => <div className="skeleton shoe-skeleton" key={item} />)}</div>}{shoes.status === 'error' && !shoes.data && <ErrorState message={shoes.error ?? t('errors.loadShoes')} onRetry={onRetry} t={t} />}{shoes.status === 'success' && !shoes.data?.length && <section className="card inline-empty-card"><div className="empty-card-icon"><Icon name="shoe" size={25} /></div><h3>{t('shoes.noShoes')}</h3><p>{t('shoes.emptyCopy')}</p><button className="button button-primary" onClick={onNew}><Icon name="plus" size={16} />{t('actions.addFirstShoe')}</button></section>}{shoes.data && shoes.data.length > 0 && <div className="shoe-grid">{shoes.data.map((shoe) => { const total = shoe.total_distance_km ?? 0; const progress = Math.min(100, total / 8); return <article className="card shoe-card" key={String(shoe.id)}><div className="shoe-card-head"><span className={classNames('status-pill', shoeStatusClass(shoe.status))}><span />{shoeStatusLabel(shoe.status, t)}</span><button className="icon-button" aria-label={`${t('actions.edit')} ${shoe.name}`} onClick={() => onEdit(shoe)}><Icon name="edit" size={17} /></button></div><div className="shoe-illustration"><div className="shoe-sole" /><ShoeImage src={shoe.image_url} alt={shoe.name} t={t} /></div><div className="shoe-brand">{shoe.brand || t('shoes.brandUnset')}</div><h3>{shoe.name || t('shoes.nameUnset')}</h3><div className="shoe-meter-label"><span>{t('shoes.totalMileage')}</span><strong>{formatDistance(total, locale)}</strong></div><div className="shoe-meter"><span style={{ width: `${progress}%` }} /></div><div className="shoe-card-foot"><span>{t('shoes.initialMileage', { distance: formatDistance(shoe.initial_distance_km, locale) })}</span><span>{t('shoes.records', { count: runCountByShoe.get(String(shoe.id)) ?? 0 })}</span></div>{total >= 640 && total < 800 && <div className="shoe-reminder"><Icon name="info" size={14} />{t('shoes.nearReplacement')}</div>}{total >= 800 && <div className="shoe-reminder shoe-reminder-alert"><Icon name="info" size={14} />{t('shoes.replaceSoon')}</div>}</article> })}<button className="add-shoe-card" onClick={onNew}><span><Icon name="plus" size={22} /></span><strong>{t('actions.addAnotherShoe')}</strong><small>{t('shoes.lede')}</small></button></div>}<div className="quiet-footer-row"><span className="source-caption"><span className="source-dot" />{t('shoes.totalMileage')}</span></div></div>
}

type ShoeFormValues = { name: string; brand: string; initial_distance_km: string; status: string; purchase_date: string; image_url: string; min_distance_km: string; max_distance_km: string; min_pace_seconds: string; max_pace_seconds: string; run_types: string[]; priority: string }

function defaultShoeValues(shoe: Shoe | null): ShoeFormValues {
  const rules = shoe?.rules ?? {}
  return { name: shoe?.name ?? '', brand: shoe?.brand ?? '', initial_distance_km: shoe ? String(shoe.initial_distance_km ?? 0) : '0', status: shoe?.status ?? 'active', purchase_date: shoe?.purchase_date ?? '', image_url: shoe?.image_url ?? '', min_distance_km: rules.min_distance_km == null ? '' : String(rules.min_distance_km), max_distance_km: rules.max_distance_km == null ? '' : String(rules.max_distance_km), min_pace_seconds: rules.min_pace_seconds == null ? '' : String(rules.min_pace_seconds), max_pace_seconds: rules.max_pace_seconds == null ? '' : String(rules.max_pace_seconds), run_types: rules.run_types ?? [], priority: rules.priority == null ? '0' : String(rules.priority) }
}

function ShoeFormModal({ shoe, onClose, onSaved, t }: { shoe: Shoe | null; onClose: () => void; onSaved: (message: string) => Promise<void>; t: Translate }) {
  const [values, setValues] = useState<ShoeFormValues>(() => defaultShoeValues(shoe))
  const [catalog, setCatalog] = useState<ShoeCatalogItem[]>([])
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  useEffect(() => setValues(defaultShoeValues(shoe)), [shoe])
  useEffect(() => { let active = true; void api.getShoeCatalog().then((items) => { if (active) setCatalog(items) }).catch(() => undefined); return () => { active = false } }, [])
  const update = (field: keyof ShoeFormValues, value: string) => setValues((current) => ({ ...current, [field]: value }))
  const updateRunType = (value: string, checked: boolean) => setValues((current) => ({ ...current, run_types: checked ? [...new Set([...current.run_types, value])] : current.run_types.filter((item) => item !== value) }))
  const chooseCatalog = (id: string) => {
    const item = catalog.find((entry) => entry.id === id)
    if (!item) return
    setValues((current) => ({ ...current, name: item.name, brand: item.brand, image_url: item.image_url ?? current.image_url }))
  }
  const optionalNumber = (value: string) => value.trim() ? Number(value) : null
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setFormError(null)
    const initial = Number(values.initial_distance_km)
    if (!values.name.trim()) return setFormError(t('errors.shoeNameRequired'))
    if (!Number.isFinite(initial) || initial < 0) return setFormError(t('errors.initialDistance'))
    const rules: ShoeRules = { min_distance_km: optionalNumber(values.min_distance_km), max_distance_km: optionalNumber(values.max_distance_km), min_pace_seconds: optionalNumber(values.min_pace_seconds), max_pace_seconds: optionalNumber(values.max_pace_seconds), run_types: values.run_types, priority: Number(values.priority) || 0 }
    const payload: ShoePayload = { name: values.name.trim(), brand: values.brand.trim(), initial_distance_km: initial, status: values.status, purchase_date: values.purchase_date || null, image_url: values.image_url.trim() || null, rules }
    setSaving(true)
    try { if (shoe) { await api.updateShoe(shoe.id, payload); await onSaved(t('shoes.updated')) } else { await api.createShoe(payload); await onSaved(t('shoes.added')) } } catch (error) { setFormError(errorMessage(error, t)) } finally { setSaving(false) }
  }
  return <Modal title={shoe ? t('shoes.editShoe') : t('shoes.newShoe')} eyebrow={shoe ? t('shoes.editShoe') : t('shoes.newShoe')} onClose={onClose} t={t}><form className="modal-form" onSubmit={submit}><div className="form-grid form-grid-two"><Field label={t('shoes.shoeName')} required t={t}><input value={values.name} onChange={(event) => update('name', event.target.value)} placeholder={t('shoes.shoeNamePlaceholder')} /></Field><Field label={t('shoes.brand')} t={t}><input value={values.brand} onChange={(event) => update('brand', event.target.value)} placeholder="Nike" /></Field><Field label={t('shoes.initialDistance')} hint={t('shoes.initialDistanceHint')} t={t}><input type="number" min="0" step="0.1" value={values.initial_distance_km} onChange={(event) => update('initial_distance_km', event.target.value)} /></Field><Field label={t('shoes.purchaseDate')} t={t}><input type="date" value={values.purchase_date} onChange={(event) => update('purchase_date', event.target.value)} /></Field><Field label={t('shoes.status')} t={t}><select value={values.status} onChange={(event) => update('status', event.target.value)}>{SHOE_STATUS_VALUES.map((value) => <option value={value} key={value}>{shoeStatusLabel(value, t)}</option>)}</select></Field><Field label={t('shoes.imageUrl')} hint={t('shoes.imageUrlHint')} t={t}><input value={values.image_url} onChange={(event) => update('image_url', event.target.value)} placeholder="https://…" /></Field></div>{catalog.length > 0 && <Field label={t('shoes.chooseCatalog')} hint={t('shoes.catalog')} t={t}><select defaultValue="" onChange={(event) => chooseCatalog(event.target.value)}><option value="">{t('shoes.chooseCatalog')}</option>{catalog.map((item) => <option value={item.id} key={item.id}>{item.brand} · {item.name}</option>)}</select></Field>}<section className="shoe-rules-form"><div className="form-section-heading"><span>{t('shoes.rules')}</span><small>{t('shoes.inferenceCopy')}</small></div><div className="form-grid form-grid-two"><Field label={t('shoes.ruleDistance')} t={t}><div className="range-fields"><input type="number" min="0" step="0.1" value={values.min_distance_km} onChange={(event) => update('min_distance_km', event.target.value)} placeholder={t('shoes.minDistance')} /><input type="number" min="0" step="0.1" value={values.max_distance_km} onChange={(event) => update('max_distance_km', event.target.value)} placeholder={t('shoes.maxDistance')} /></div></Field><Field label={t('shoes.rulePace')} t={t}><div className="range-fields"><input type="number" min="0" step="1" value={values.min_pace_seconds} onChange={(event) => update('min_pace_seconds', event.target.value)} placeholder={t('shoes.minPace')} /><input type="number" min="0" step="1" value={values.max_pace_seconds} onChange={(event) => update('max_pace_seconds', event.target.value)} placeholder={t('shoes.maxPace')} /></div></Field><Field label={t('shoes.priority')} hint={t('shoes.priorityHint')} t={t}><input type="number" step="1" value={values.priority} onChange={(event) => update('priority', event.target.value)} /></Field></div><div className="run-type-checks"><span>{t('shoes.runTypes')}</span><div>{RUN_TYPE_VALUES.map((value) => <label key={value}><input type="checkbox" checked={values.run_types.includes(value)} onChange={(event) => updateRunType(value, event.target.checked)} />{runTypeLabel(value, t)}</label>)}</div><small>{values.run_types.length === 0 ? t('shoes.allRunTypes') : values.run_types.map((value) => runTypeLabel(value, t)).join(', ')}</small></div></section>{formError && <div className="form-error"><Icon name="info" size={16} />{formError}</div>}<div className="modal-footer"><button className="button button-secondary" type="button" onClick={onClose}>{t('actions.cancel')}</button><button className="button button-primary" type="submit" disabled={saving}>{saving ? <><span className="button-spinner" />{t('actions.loading')}</> : shoe ? t('actions.saveChanges') : t('actions.addShoe')}</button></div></form></Modal>
}

function normalizePredictions(result: ShoeInference): ShoePrediction[] {
  return result.predictions ?? result.assignments ?? result.matches ?? []
}

function InferenceDialog({ onClose, onApplied, locale, t }: { onClose: () => void; onApplied: () => Promise<void>; locale: 'en' | 'zh'; t: Translate }) {
  const [state, setState] = useState<ResourceState<ShoeInference>>(emptyResource)
  const [applying, setApplying] = useState(false)
  const requestRef = useRef(0)
  const preview = useCallback(async () => {
    const requestId = ++requestRef.current
    setState({ data: null, status: 'loading', error: null })
    try {
      const data = await api.inferShoes({ apply: false })
      if (requestId === requestRef.current) setState({ data, status: 'success', error: null })
    } catch (error) {
      if (requestId === requestRef.current) setState({ data: null, status: 'error', error: errorMessage(error, t) })
    }
  }, [t])
  useEffect(() => { void preview(); return () => { requestRef.current += 1 } }, [preview])
  const predictions = state.data ? normalizePredictions(state.data) : []
  const apply = async () => {
    setApplying(true)
    try { await api.inferShoes({ apply: true }); await onApplied() } catch (error) { setState((current) => ({ ...current, error: errorMessage(error, t) })) } finally { setApplying(false) }
  }
  const previewCount = state.data?.count ?? state.data?.assignments?.length ?? predictions.length
  return <Modal title={t('shoes.inference')} eyebrow={t('shoes.inference')} onClose={onClose} t={t}><div className="inference-copy">{t('shoes.inferenceCopy')}</div>{state.status === 'loading' && <div className="inference-loading"><span /><span /><span /></div>}{state.status === 'error' && <ErrorState message={state.error ?? t('errors.generic')} onRetry={() => void preview()} t={t} compact />}{state.status === 'success' && <>{predictions.length ? <><div className="inference-count">{t('shoes.previewCount', { count: previewCount })}</div><div className="inference-list">{predictions.map((prediction, index) => { const targetName = prediction.shoe_name ?? (prediction.shoe_id == null ? t('runs.noShoe') : `${t('shoes.nameUnset')} ${prediction.shoe_id}`); const confidence = prediction.confidence == null ? '' : ` · ${Math.round(prediction.confidence * 100)}%`; return <div className="inference-row" key={`${prediction.run_id}-${prediction.shoe_id ?? 'none'}-${index}`}><div><strong>{prediction.run_title || `${t('runs.run')} ${prediction.run_id}`}</strong><small>{prediction.run_started_at ? formatDate(prediction.run_started_at, locale) : ''}</small></div><span className="inference-arrow">→</span><div><strong>{targetName}</strong><small>{`${prediction.reason || t('shoes.predicted')}${confidence}`}</small></div></div> })}</div></> : <InlineEmpty title={t('shoes.noPredictions')} copy={t('shoes.inferenceCopy')} />}</>}{state.error && state.status === 'success' && <p className="analysis-error">{state.error}</p>}<div className="modal-footer"><button className="button button-secondary" type="button" onClick={onClose}>{t('actions.cancel')}</button><button className="button button-primary" type="button" onClick={() => void apply()} disabled={applying || state.status !== 'success' || predictions.length === 0}>{applying ? <><span className="button-spinner" />{t('actions.loading')}</> : t('actions.apply')}</button></div></Modal>
}

function CheckinView({ onToast, locale, t }: { onToast: (message: string, tone?: Toast['tone']) => void; locale: 'en' | 'zh'; t: Translate }) {
  const [date, setDate] = useState(todayIso)
  const [resource, setResource] = useState<ResourceState<Checkin>>(() => emptyResource())
  const [values, setValues] = useState({ sleep_hours: '', energy: 3, soreness: 2, notes: '' })
  const [saving, setSaving] = useState(false)
  const requestRef = useRef(0)
  const currentDateRef = useRef(date)
  const load = useCallback(async (targetDate: string) => {
    const requestId = ++requestRef.current
    setResource((current) => ({ ...current, status: 'loading', error: null }))
    try {
      const data = await api.getCheckin(targetDate)
      if (requestId !== requestRef.current || currentDateRef.current !== targetDate) return
      setResource({ data, status: 'success', error: null })
      setValues({ sleep_hours: data.sleep_hours == null ? '' : String(data.sleep_hours), energy: data.energy ?? 3, soreness: data.soreness ?? 2, notes: data.notes ?? '' })
    } catch (error) {
      if (requestId !== requestRef.current || currentDateRef.current !== targetDate) return
      if (error instanceof ApiError && error.status === 404) { setResource({ data: null, status: 'success', error: null }); setValues({ sleep_hours: '', energy: 3, soreness: 2, notes: '' }) } else setResource((current) => ({ ...current, status: 'error', error: errorMessage(error, t, 'errors.loadCheckin') }))
    }
  }, [t])
  useEffect(() => { currentDateRef.current = date; void load(date) }, [date, load])
  const save = async (event: FormEvent) => {
    event.preventDefault()
    const targetDate = date
    const sleep = values.sleep_hours.trim() ? Number(values.sleep_hours) : null
    if (sleep != null && (!Number.isFinite(sleep) || sleep < 0 || sleep > 24)) { onToast(t('errors.sleepInvalid'), 'error'); return }
    setSaving(true)
    try { await api.updateCheckin(targetDate, { sleep_hours: sleep, energy: values.energy, soreness: values.soreness, notes: values.notes.trim() }); if (currentDateRef.current === targetDate) await load(targetDate); onToast(t('checkin.saved')) } catch (error) { onToast(errorMessage(error, t), 'error') } finally { setSaving(false) }
  }
  return <div className="view-stack"><section className="page-intro-row checkin-intro"><div><div className="eyebrow">{t('checkin.eyebrow')}</div><h2>{t('checkin.headline')}</h2><p className="lede">{t('checkin.lede')}</p></div><div className="date-navigator"><button className="icon-button" aria-label={t('checkin.previousDay')} onClick={() => setDate((current) => shiftDate(current, -1))}><Icon name="chevron-left" size={18} /></button><label><Icon name="calendar" size={16} /><input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label><button className="icon-button" aria-label={t('checkin.nextDay')} onClick={() => setDate((current) => shiftDate(current, 1))}><Icon name="chevron-right" size={18} /></button></div></section>{resource.status === 'loading' && <div className="skeleton checkin-skeleton" />}{resource.status === 'error' && <ErrorState message={resource.error ?? t('errors.loadCheckin')} onRetry={() => void load(date)} t={t} />}{resource.status === 'success' && <form className="checkin-layout" onSubmit={save}><section className="card checkin-main-card"><div className="checkin-card-heading"><div><span className="section-eyebrow">{resource.data ? t('checkin.recorded') : t('checkin.notRecorded')}</span><h3>{t('checkin.bodyStatus', { date: formatDate(`${date}T12:00:00`, locale, true) })}</h3></div><span className={classNames('checkin-status', resource.data && 'checkin-status-done')}><span />{resource.data ? t('checkin.saved') : t('checkin.toFill')}</span></div><div className="checkin-question"><div className="question-copy"><Icon name="clock" size={19} /><div><strong>{t('checkin.sleepQuestion')}</strong><span>{t('checkin.sleepHint')}</span></div></div><div className="sleep-input"><input type="number" min="0" max="24" step="0.5" value={values.sleep_hours} onChange={(event) => setValues((current) => ({ ...current, sleep_hours: event.target.value }))} placeholder="—" /><span>{t('checkin.hours')}</span></div></div><RatingQuestion label={t('checkin.energyQuestion')} hint={t('checkin.energyHint')} icon="zap" value={values.energy} onChange={(value) => setValues((current) => ({ ...current, energy: value }))} labels={[t('checkin.low'), t('checkin.average'), t('checkin.okay'), t('checkin.good'), t('checkin.full')]} /><RatingQuestion label={t('checkin.sorenessQuestion')} hint={t('checkin.sorenessHint')} icon="heart" value={values.soreness} onChange={(value) => setValues((current) => ({ ...current, soreness: value }))} labels={[t('checkin.none'), t('checkin.mild'), t('checkin.some'), t('checkin.clear'), t('checkin.rest')]} /><div className="checkin-note-field"><label htmlFor="checkin-notes">{t('checkin.noteQuestion')}</label><textarea id="checkin-notes" rows={4} value={values.notes} onChange={(event) => setValues((current) => ({ ...current, notes: event.target.value }))} placeholder={t('checkin.notePlaceholder')} /></div><div className="checkin-submit-row"><span><Icon name="info" size={15} />{t('checkin.onlyToday')}</span><button className="button button-primary" type="submit" disabled={saving}>{saving ? <><span className="button-spinner" />{t('actions.loading')}</> : t('checkin.saveToday')}</button></div></section><aside className="card checkin-side-card"><div className="side-card-icon"><Icon name="sun" size={20} /></div><span className="section-eyebrow">{t('checkin.ritual')}</span><h3>{t('checkin.sideTitle')}</h3><p>{t('checkin.sideCopy')}</p><div className="side-rule" /><span className="side-caption">{t('checkin.sideCaption')}</span></aside></form>}</div>
}

function RatingQuestion({ label, hint, icon, value, onChange, labels }: { label: string; hint: string; icon: IconName; value: number; onChange: (value: number) => void; labels: string[] }) { return <div className="rating-question"><div className="question-copy"><Icon name={icon} size={19} /><div><strong>{label}</strong><span>{hint}</span></div></div><div className="rating-options">{[1, 2, 3, 4, 5].map((rating) => <button type="button" className={value === rating ? 'rating-active' : ''} key={rating} onClick={() => onChange(rating)}><strong>{rating}</strong><small>{labels[rating - 1]}</small></button>)}</div></div> }

function InspectionResult({ data, t }: { data: GoogleHealthInspection | null; t: Translate }) {
  const earliest = data?.earliest_imported_date ?? data?.earliest_accessed_date ?? null
  const coverage = data?.source_coverage_note ?? data?.coverage ?? (data?.coverage_start || data?.coverage_end ? `${data.coverage_start ?? t('settings.unknown')} – ${data.coverage_end ?? t('settings.unknown')}` : null)
  const source = data?.raw_example ?? data?.source_json ?? data?.data ?? null
  return <><div className="inspection-meta"><span>{t('settings.earliestAccessed')}<strong>{earliest ?? t('settings.unknown')}</strong></span><span>{t('settings.coverage')}<strong>{coverage ?? t('settings.unknown')}</strong></span><span>{t('settings.dataSources')}<strong>{data?.available_types?.length ? data.available_types.join(', ') : t('settings.unknown')}</strong></span></div>{source ? <pre className="source-json">{JSON.stringify(source, null, 2)}</pre> : <p className="streams-empty">{t('settings.notAvailable')}</p>}</>
}

function SettingsView({ onToast, onDataChanged, locale, onToggleLocale, t }: { onToast: (message: string, tone?: Toast['tone']) => void; onDataChanged: () => Promise<void>; locale: 'en' | 'zh'; onToggleLocale: () => void; t: Translate }) {
  const auth = useAuth()
  const [integrations, setIntegrations] = useState<ResourceState<Integration[]>>(() => emptyResource())
  const [inspection, setInspection] = useState<ResourceState<GoogleHealthInspection | null>>(() => emptyResource())
  const [shares, setShares] = useState<ResourceState<Share[]>>(() => emptyResource())
  const [importing, setImporting] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [importResult, setImportResult] = useState<ImportResult | null>(null)
  const [syncStartDate, setSyncStartDate] = useState('')
  const [fullHistory, setFullHistory] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const integrationRequestRef = useRef(0)
  const inspectionRequestRef = useRef(0)
  const sharesRequestRef = useRef(0)
  const loadIntegrations = useCallback(() => loadResource(api.getIntegrations, setIntegrations, integrationRequestRef, t, 'errors.loadIntegrations'), [t])
  const loadInspection = useCallback(async () => {
    const requestId = ++inspectionRequestRef.current
    setInspection((current) => ({ ...current, status: 'loading', error: null }))
    try { const data = await api.getGoogleHealthInspection(); if (requestId === inspectionRequestRef.current) setInspection({ data, status: 'success', error: null }) } catch (error) { if (requestId !== inspectionRequestRef.current) return; if (error instanceof ApiError && error.status === 404) setInspection({ data: null, status: 'success', error: null }); else setInspection({ data: null, status: 'error', error: errorMessage(error, t) }) }
  }, [t])
  const loadShares = useCallback(() => loadResource(api.listShares, setShares, sharesRequestRef, t, 'errors.generic'), [t])
  useEffect(() => { void loadIntegrations(); void loadShares(); void loadInspection() }, [loadIntegrations, loadInspection, loadShares])
  const google = integrations.data?.find((integration) => integration.id.toLowerCase().includes('google'))
  const googleConnected = Boolean(google && ['connected', 'active', 'ready'].includes(google.status.toLowerCase()))
  const connectGoogle = async () => {
    try { const result = await api.connectGoogleHealth(); if (result.url) window.location.assign(result.url) } catch (error) { onToast(errorMessage(error, t), 'error') }
  }
  const syncGoogle = async (event: FormEvent) => {
    event.preventDefault()
    setSyncing(true)
    try { await api.syncGoogleHealth({ start_date: syncStartDate || undefined, full_history: fullHistory }); await Promise.allSettled([loadIntegrations(), loadInspection(), onDataChanged()]); onToast(t('settings.syncComplete')) } catch (error) { onToast(errorMessage(error, t), 'error') } finally { setSyncing(false) }
  }
  const onFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setImporting(true); setImportResult(null)
    try { const result = await api.importCsv(file); setImportResult(result); await onDataChanged(); onToast(t('settings.lastImport', { imported: result.imported, skipped: result.skipped })) } catch (error) { onToast(errorMessage(error, t), 'error') } finally { setImporting(false) }
  }
  const exportData = async () => {
    try { const blob = await api.exportData(); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `runwise-export-${todayIso()}.json`; document.body.appendChild(anchor); anchor.click(); anchor.remove(); URL.revokeObjectURL(url); onToast(t('settings.downloadBackup'), 'info') } catch (error) { onToast(errorMessage(error, t), 'error') }
  }
  const revoke = async (share: Share) => {
    const id = share.id ?? share.token
    if (id == null) return
    try { await api.revokeShare(id); await loadShares(); onToast(t('actions.revoke'), 'info') } catch (error) { onToast(errorMessage(error, t), 'error') }
  }
  const sourceRecords = integrations.data?.filter((integration) => integration.id.toLowerCase().includes('google')) ?? []
  const googleDescription = google ? integrationMessage(google, googleConnected, t) : (googleConnected ? t('settings.connectCopy') : t('settings.googleNotConfigured'))
  return <div className="view-stack settings-view"><section className="page-intro-row"><div><div className="eyebrow">{t('settings.eyebrow')}</div><h2>{t('settings.headline')}</h2><p className="lede">{t('settings.lede')}</p></div></section><section className="settings-section"><div className="settings-section-heading"><div><span className="section-eyebrow">{t('settings.dataSources')}</span><h3>{t('settings.sourcesTitle')}</h3><p>{t('settings.sourcesCopy')}</p></div><button className="text-button" onClick={() => void loadIntegrations()}><Icon name="refresh" size={15} />{t('actions.refresh')}</button></div>{integrations.status === 'loading' && !integrations.data && <div className="integration-grid"><div className="skeleton integration-skeleton" /><div className="skeleton integration-skeleton" /></div>}{integrations.status === 'error' && !integrations.data && <ErrorState compact message={integrations.error ?? t('errors.loadIntegrations')} onRetry={() => void loadIntegrations()} t={t} />}{integrations.data && <div className="integration-grid">{sourceRecords.length ? sourceRecords.map((integration) => <IntegrationCard key={integration.id} integration={integration} t={t} />) : <IntegrationCard integration={{ id: 'local', name: t('settings.localRecords'), status: 'connected', message: t('settings.localRecordsCopy') }} t={t} />}</div>}</section><section className="google-health-panel card"><div className="google-health-heading"><div className="pending-icon"><Icon name="activity" size={19} /></div><div><span className="section-eyebrow">{t('settings.googleHealth')}</span><h3>{google?.name ?? t('settings.googleHealth')}</h3><p>{googleDescription}</p></div><span className={classNames('integration-status', googleConnected && 'integration-status-connected')}><span />{googleConnected ? t('settings.connected') : google?.status === 'not_connected' ? t('settings.notConnected') : t('settings.notConfigured')}</span></div><div className="google-health-actions"><button className="button button-secondary" onClick={() => void connectGoogle()}><Icon name="external" size={16} />{t('actions.connect')}</button>{googleConnected && <form className="sync-form" onSubmit={syncGoogle}><label><span>{t('settings.startDate')}</span><input type="date" value={syncStartDate} onChange={(event) => setSyncStartDate(event.target.value)} /></label><label className="checkbox-field"><input type="checkbox" checked={fullHistory} onChange={(event) => setFullHistory(event.target.checked)} />{t('settings.fullHistory')}</label><button className="button button-primary" type="submit" disabled={syncing}>{syncing ? <><span className="button-spinner" />{t('actions.loading')}</> : t('actions.sync')}</button></form>}</div><div className="inspection-block"><div className="inspection-heading"><div><span className="section-eyebrow">{t('settings.inspectionTitle')}</span><strong>{t('settings.inspectionCopy')}</strong></div>{inspection.status === 'loading' && <span className="inspection-status">{t('actions.loading')}</span>}</div>{inspection.status === 'error' && <p className="analysis-error">{inspection.error}</p>}{inspection.status === 'success' && <InspectionResult data={inspection.data} t={t} />}</div></section><section className="settings-section"><div className="settings-section-heading"><div><span className="section-eyebrow">{t('settings.portability')}</span><h3>{t('settings.portabilityTitle')}</h3><p>{t('settings.portabilityCopy')}</p></div></div><div className="portability-grid"><div className="portability-card"><div className="portability-icon"><Icon name="upload" size={19} /></div><div><strong>{t('settings.importCsv')}</strong><p>{t('settings.importCopy')}</p>{importResult && <span className="import-result"><Icon name="check" size={14} />{t('settings.lastImport', { imported: importResult.imported, skipped: importResult.skipped })}</span>}</div><button className="button button-secondary" onClick={() => fileRef.current?.click()} disabled={importing}>{importing ? <><span className="button-spinner" />{t('settings.importing')}</> : t('settings.chooseCsv')}</button><input ref={fileRef} type="file" accept=".csv,text/csv" onChange={onFile} hidden /></div><div className="portability-card"><div className="portability-icon portability-icon-green"><Icon name="download" size={19} /></div><div><strong>{t('settings.exportJson')}</strong><p>{t('settings.exportCopy')}</p></div><button className="button button-secondary" onClick={() => void exportData()}><Icon name="download" size={16} />{t('settings.downloadBackup')}</button></div></div></section><section className="settings-section"><div className="settings-section-heading"><div><span className="section-eyebrow">{t('share.title')}</span><h3>{t('actions.share')}</h3><p>{t('share.copy')}</p></div></div>{shares.status === 'loading' && !shares.data && <div className="skeleton integration-skeleton" />}{shares.status === 'error' && !shares.data && <ErrorState compact message={shares.error ?? t('errors.generic')} onRetry={() => void loadShares()} t={t} />}{shares.data && (shares.data.length ? <div className="shares-list">{shares.data.map((share, index) => <div className="share-row" key={String(share.id ?? share.token ?? index)}><div><strong>{share.kind === 'run' ? t('share.publicRun') : share.kind === 'week' ? t('share.publicWeek') : t('share.publicMonth')}</strong><small>{share.expires_at ? t('share.expiresAt', { date: formatDateTime(share.expires_at, locale) }) : t('settings.unknown')}</small></div><button className="text-button danger-text" onClick={() => void revoke(share)}>{t('actions.revoke')}</button></div>)}</div> : <InlineEmpty title={t('settings.notAvailable')} copy={t('share.copy')} />)}</section><section className="language-card card"><div><span className="section-eyebrow">{t('settings.language')}</span><h3>{t('settings.language')}</h3><p>{t('settings.languageCopy')}</p></div><button className="button button-secondary" onClick={onToggleLocale}>{t('actions.changeLanguage')}</button></section>{auth.session && <div className="settings-footnote"><Icon name="database" size={15} />{auth.session.user.email ?? t('app.localAthlete')}<button className="text-button" onClick={() => void auth.signOut()}>{t('actions.signOut')}</button></div>}</div>
}

function integrationMessage(integration: Integration, configured: boolean, t: Translate) {
  const id = integration.id.toLowerCase()
  const status = integration.status.toLowerCase()
  if (id.includes('google')) {
    if (status === 'not_connected') return t('settings.googleReady')
    if (status === 'error' || status === 'failed') return t('errors.server')
    return configured ? t('settings.connectCopy') : t('settings.googleNotConfigured')
  }
  if (id.includes('local')) return t('settings.localRecordsCopy')
  return configured ? t('settings.connectCopy') : t('settings.googleNotConfigured')
}

function IntegrationCard({ integration, t }: { integration: Integration; t: Translate }) {
  const configured = ['connected', 'active', 'ready'].includes(integration.status.toLowerCase())
  return <article className="integration-card"><div className={classNames('integration-icon', configured && 'integration-icon-connected')}><Icon name={integration.id.toLowerCase().includes('local') ? 'database' : 'activity'} size={20} /></div><div className="integration-copy"><div className="integration-title"><strong>{integration.name}</strong><span className={classNames('integration-status', configured && 'integration-status-connected')}><span />{configured ? t('settings.connected') : integration.status === 'not_connected' ? t('settings.notConnected') : t('settings.notConfigured')}</span></div><p>{integrationMessage(integration, configured, t)}</p></div><Icon name="chevron-right" size={17} /></article>
}

async function copyText(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return true
  }
  const input = document.createElement('textarea')
  input.value = value
  input.style.position = 'fixed'
  input.style.opacity = '0'
  document.body.appendChild(input)
  input.focus()
  input.select()
  const copied = document.execCommand('copy')
  input.remove()
  return copied
}

function ShareDialog({ target, onClose, locale, t }: { target: ShareTarget; onClose: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const [expiresDays, setExpiresDays] = useState('30')
  const [state, setState] = useState<ResourceState<Share>>(emptyResource)
  const [copied, setCopied] = useState(false)
  const create = useCallback(async () => {
    setState({ data: null, status: 'loading', error: null })
    try {
      const data = await api.createShare({ kind: target.kind, run_id: target.runId, date: target.date, expires_days: Number(expiresDays) })
      setState({ data, status: 'success', error: null })
    } catch (error) {
      setState({ data: null, status: 'error', error: errorMessage(error, t) })
    }
  }, [expiresDays, t, target.date, target.kind, target.runId])
  useEffect(() => { void create() }, [create])
  const url = state.data?.token ? `${window.location.origin}/share/${encodeURIComponent(state.data.token)}` : (state.data?.url ?? '')
  const copy = async () => { if (!url) return; try { const didCopy = await copyText(url); setCopied(didCopy) } catch { setCopied(false) } }
  const title = target.kind === 'run' ? (target.runId == null ? t('share.shareRun') : t('share.shareRunDetail')) : target.kind === 'week' ? t('share.shareWeek') : t('share.shareMonth')
  return <Modal title={title} eyebrow={t('share.title')} onClose={onClose} t={t}><p className="share-dialog-copy">{t('share.copy')}</p>{state.status === 'loading' && <div className="inference-loading"><span /><span /><span /></div>}{state.status === 'error' && <ErrorState compact message={state.error ?? t('errors.generic')} onRetry={() => void create()} t={t} />}{state.status === 'success' && <div className="share-result"><span className="section-eyebrow">{t('share.created')}</span><div className="share-url-row"><input value={url} readOnly aria-label={url} onFocus={(event) => event.currentTarget.select()} /><button className="button button-secondary" onClick={() => void copy()}>{copied ? t('actions.copied') : t('actions.copyLink')}</button></div>{state.data?.expires_at && <small>{t('share.expiresAt', { date: formatDateTime(state.data.expires_at, locale) })}</small>}</div>}<div className="share-expiry"><label><span>{t('share.expires')}</span><select value={expiresDays} disabled={state.status === 'loading'} onChange={(event) => setExpiresDays(event.target.value)}><option value="7">7 {t('share.days')}</option><option value="30">30 {t('share.days')}</option></select></label></div><div className="modal-footer"><button className="button button-secondary" type="button" onClick={onClose}>{t('actions.cancel')}</button>{state.status === 'success' && <button className="button button-primary" type="button" onClick={() => void copy()}>{copied ? t('actions.copied') : t('actions.copyLink')}</button>}</div></Modal>
}

function AuthGate() {
  const { locale, t, toggleLocale } = useI18n()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const connect = async () => {
    setError(null)
    setBusy(true)
    try {
      const result = await api.connectGoogleHealth()
      if (result.url) window.location.assign(result.url)
      else setError(t('settings.googleNotConfigured'))
    } catch (connectError) {
      setError(errorMessage(connectError, t))
      setBusy(false)
    }
  }
  return <main className="auth-page"><section className="auth-card"><div className="auth-brand"><div className="brand-mark"><span /><span /><span /></div><div className="brand-name">{t('app.name')}</div><button className="language-toggle" onClick={toggleLocale}>{t('actions.changeLanguage')}</button></div><div className="auth-copy"><span className="eyebrow">{t('auth.eyebrow')}</span><h1>{t('auth.headline')}</h1><p>{t('auth.copy')}</p></div><button className="google-button" type="button" onClick={() => void connect()} disabled={busy}><span className="google-glyph">G</span>{busy ? t('actions.loading') : t('settings.googleHealth')}</button>{error && <div className="form-error"><Icon name="info" size={16} />{error}</div>}<p className="auth-hint">{t('settings.connectCopy')}</p></section></main>
}

function PublicSharePage({ token }: { token: string }) {
  const { locale, t, toggleLocale } = useI18n()
  const [state, setState] = useState<ResourceState<PublicShareSnapshot>>(emptyResource)
  const [copied, setCopied] = useState(false)
  useEffect(() => { let active = true; setState({ data: null, status: 'loading', error: null }); void api.getPublicShare(token).then((data) => { if (active) setState({ data, status: 'success', error: null }) }).catch((error) => { if (active) setState({ data: null, status: 'error', error: errorMessage(error, t, 'errors.loadShare') }) }); return () => { active = false } }, [t, token])
  const copy = async () => { try { setCopied(await copyText(window.location.href)) } catch { setCopied(false) } }
  const snapshot = state.data
  const runs = snapshot ? (Array.isArray(snapshot.runs) ? snapshot.runs : snapshot.run ? [snapshot.run] : []) : []
  const stats = snapshot?.stats
  const kindLabel = snapshot?.kind === 'run' ? t('share.publicRun') : snapshot?.kind === 'week' ? t('share.publicWeek') : t('share.publicMonth')
  return <main className="public-share-page"><div className="public-share-header"><div className="brand-name">{t('app.name')}</div><button className="language-toggle" onClick={toggleLocale}>{t('actions.changeLanguage')}</button></div>{state.status === 'loading' && <div className="share-loading"><div className="skeleton share-skeleton" /></div>}{state.status === 'error' && <StandaloneError message={state.error ?? t('share.notFound')} />}{state.status === 'success' && snapshot && <section className="public-share-card card"><div className="public-share-title"><div><span className="eyebrow">{t('share.publicTitle')}</span><h1>{kindLabel}</h1><p>{t('share.publicCopy')}</p></div><button className="button button-secondary" onClick={() => void copy()}><Icon name="download" size={16} />{copied ? t('actions.copied') : t('actions.copyLink')}</button></div>{stats && <div className="public-metrics"><MetricCard label={t('stats.totalDistance')} value={stats.total_distance_km == null ? '—' : formatNumber(stats.total_distance_km, locale)} unit="km" icon="activity" accent="green" detail={t('stats.runCount')} /><MetricCard label={t('stats.runCount')} value={stats.run_count == null ? '—' : String(stats.run_count)} unit={t('stats.count')} icon="chart" accent="amber" detail={t('share.publicCopy')} /><MetricCard label={t('stats.totalDuration')} value={stats.total_duration_seconds == null ? '—' : formatNumber(stats.total_duration_seconds / 3600, locale)} unit={t('time.hours', { count: '' })} icon="clock" accent="blue" detail={t('stats.trainingTime')} /></div>}{runs.length > 0 && <div className="public-run-list">{runs.map((run, index) => <article className="public-run-row" key={String(run.id ?? index)}><div><strong>{run.title || t('runs.unnamed')}</strong><small>{run.started_at ? formatDateTime(run.started_at, locale) : t('share.date')}</small></div><span>{run.distance_km == null ? '—' : formatDistance(run.distance_km, locale)}</span><span>{run.duration_seconds == null ? '—' : formatDuration(run.duration_seconds, t)}</span></article>)}</div>}</section>}</main>
}

function LoadingScreen() { const { t } = useI18n(); return <main className="standalone-state"><div className="loading-mark"><span /><span /><span /></div><p>{t('app.loading')}</p></main> }

function StandaloneError({ message }: { message: string }) { const { t } = useI18n(); return <main className="standalone-state"><div className="error-icon"><Icon name="refresh" size={18} /></div><strong>{t('errors.generic')}</strong><p>{message}</p></main> }

function Modal({ title, eyebrow, onClose, children, t }: { title: string; eyebrow: string; onClose: () => void; children: ReactNode; t: Translate }) {
  useEffect(() => { const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }; document.addEventListener('keydown', onKeyDown); const previous = document.body.style.overflow; document.body.style.overflow = 'hidden'; return () => { document.removeEventListener('keydown', onKeyDown); document.body.style.overflow = previous } }, [onClose])
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><div className="modal-card" role="dialog" aria-modal="true" aria-label={title}><div className="modal-heading"><div><span className="section-eyebrow">{eyebrow}</span><h2>{title}</h2></div><button className="icon-button" onClick={onClose} aria-label={t('app.close')}><Icon name="close" size={19} /></button></div>{children}</div></div>
}

function Field({ label, required, hint, children }: { label: string; required?: boolean; hint?: string; children: ReactNode; t?: Translate }) { return <label className="form-field"><span>{label}{required && <em>*</em>}</span>{children}{hint && <small>{hint}</small>}</label> }

function ErrorState({ message, onRetry, compact = false, t }: { message: string; onRetry?: () => void; compact?: boolean; t: Translate }) { return <div className={classNames('error-state', compact && 'error-state-compact')}><div className="error-icon"><Icon name="refresh" size={18} /></div><div><strong>{t('errors.generic')}</strong><p>{message}</p>{onRetry && <button className="text-button" onClick={onRetry}>{t('actions.retry')} <Icon name="arrow-right" size={15} /></button>}</div></div> }

function InlineEmpty({ title, copy }: { title: string; copy: string }) { return <div className="inline-empty"><div className="inline-empty-mark"><Icon name="activity" size={18} /></div><strong>{title}</strong><span>{copy}</span></div> }

function ToastMessage({ toast, onClose, t }: { toast: Toast; onClose: () => void; t: Translate }) { return <div className={classNames('toast-message', `toast-${toast.tone}`)} role="status"><span className="toast-icon"><Icon name={toast.tone === 'success' ? 'check' : toast.tone === 'error' ? 'info' : 'spark'} size={16} /></span><span>{toast.message}</span><button className="toast-close" aria-label={t('app.close')} onClick={onClose}><Icon name="close" size={14} /></button></div> }

export default App
