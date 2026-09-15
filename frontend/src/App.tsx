import { ShareImage } from './ShareImage'
import { StatsPeriodPicker } from './StatsPeriodPicker'
import { ShoeBreakdown } from './ShoeBreakdown'
import { RunLibrary } from './RunLibrary'
import { RunWeather } from './RunWeather'
import { RunFitness } from './RunFitness'
import { CoachDashboard, TrainingCalendar } from './TrainingViews'
import { RunTelemetry } from './RunTelemetry'
import './run-layout.css'
import { ShoeCatalogPicker } from './ShoeCatalogPicker'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ChangeEvent, Dispatch, FormEvent, ReactNode, SetStateAction } from 'react'
import { useAuth } from './auth'
import { Icon, type IconName } from './icons'
import { useI18n, localeTag, type Translate } from './i18n'
import { api, ApiError, request } from './lib/api'
import type {
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

type ViewKey = 'runs' | 'stats' | 'coach' | 'shoes'
type ResourceState<T> = {
  data: T | null
  status: 'idle' | 'loading' | 'success' | 'error'
  error: string | null
}
type Toast = { tone: 'success' | 'error' | 'info'; message: string }
type ShareTarget = { kind: ShareKind; runId?: RunId; date?: string }

const VIEWS: Array<{ key: ViewKey; icon: IconName }> = [
  { key: 'runs', icon: 'activity' },
  { key: 'stats', icon: 'chart' },
  { key: 'coach', icon: 'spark' },
  { key: 'shoes', icon: 'shoe' },
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

function localDateValue(value: string | null | undefined) {
  if (!value) return null
  const trimmed = value.trim()
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed)
  if (match) {
    const year = Number(match[1])
    const month = Number(match[2])
    const day = Number(match[3])
    const date = new Date(year, month - 1, day, 12)
    return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null
  }
  const instant = new Date(trimmed)
  if (Number.isNaN(instant.getTime())) return null
  return new Date(instant.getFullYear(), instant.getMonth(), instant.getDate(), 12)
}

function localDateKey(date: Date) {
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function formatDate(value: string, _locale: 'en' | 'zh') {
  const date = localDateValue(value)
  return date ? localDateKey(date) : value
}

function formatDateTime(value: string, locale: 'en' | 'zh') {
  return formatDate(value, locale)
}

function localDateOnly(value: string | null | undefined) {
  if (!value) return null
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim())
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const date = new Date(year, month - 1, day, 12)
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null
}

function formatRunDateTime(run: Pick<Run, 'started_at' | 'local_date'>, locale: 'en' | 'zh') {
  const date = localDateOnly(run.local_date)
  if (!date) return formatDateTime(run.started_at, locale)
  return localDateKey(date)
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

function getViewFromHash(): ViewKey {
  const value = window.location.hash.replace(/^#/, '').split('/')[0] as ViewKey
  return VIEWS.some((item) => item.key === value) ? value : 'runs'
}

function getRunIdFromHash(): RunId | null {
  const [view, id] = window.location.hash.replace(/^#/, '').split('/')
  if (view !== 'runs' || !id) return null
  const decoded = decodeURIComponent(id)
  return /^\d+$/.test(decoded) ? Number(decoded) : decoded
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
  const [selectedRunId, setSelectedRunId] = useState<RunId | null>(() => getRunIdFromHash())
  const [runForm, setRunForm] = useState<{ open: boolean; run: Run | null }>({ open: false, run: null })
  const [shoeForm, setShoeForm] = useState<{ open: boolean; shoe: Shoe | null }>({ open: false, shoe: null })
  const [toast, setToast] = useState<Toast | null>(null)
  const [shareTarget, setShareTarget] = useState<ShareTarget | null>(null)
  const [inferenceOpen, setInferenceOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)

  const runsRequestRef = useRef(0)
  const shoesRequestRef = useRef(0)
  const statsRequestRef = useRef(0)
  const refreshRuns = useCallback(() => loadResource(api.getRuns, setRunsState, runsRequestRef, t, 'errors.loadRuns'), [t])
  const refreshShoes = useCallback(() => loadResource(api.getShoes, setShoesState, shoesRequestRef, t, 'errors.loadShoes'), [t])
  const refreshStats = useCallback((period = statsPeriod, date = statsDate) => loadResource(() => api.getStats(period, date), setStatsState, statsRequestRef, t, 'errors.loadStats'), [statsDate, statsPeriod, t])

  useEffect(() => {
    const onHashChange = () => {
      const hashValue = window.location.hash.replace(/^#/, '').split('/')[0]
      if (!VIEWS.some((item) => item.key === hashValue)) {
        window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#runs`)
      }
      const nextView = getViewFromHash()
      setView(nextView)
      setSelectedRunId(nextView === 'runs' ? getRunIdFromHash() : null)
    }
    onHashChange()
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
    setSelectedRunId(null)
    setSidebarOpen(false)
  }, [])
  const selectRun = useCallback((run: Run) => {
    setSelectedRunId(run.id)
    window.location.hash = `runs/${encodeURIComponent(String(run.id))}`
    setView('runs')
    setSidebarOpen(false)
  }, [])
  const showToast = useCallback((message: string, tone: Toast['tone'] = 'success') => setToast({ message, tone }), [])
  const selectedRun = useMemo(() => runsState.data?.find((run) => String(run.id) === String(selectedRunId)) ?? null, [runsState.data, selectedRunId])
  const pageMeta = VIEWS.find((item) => item.key === view) ?? VIEWS[0]

  const handleBulkUpdate = async (ids: RunId[], patch: { run_type?: string; shoe_id?: RunId | null }) => {
    const payload = { ...patch, run_ids: ids.map(Number) }
    if ('shoe_id' in payload && payload.shoe_id != null) payload.shoe_id = Number(payload.shoe_id)
    const result = await request<{ updated_count: number }>('/api/runs/bulk-update', { method: 'POST', body: JSON.stringify(payload) })
    await Promise.all([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)])
    showToast(t('library.updated', { count: result.updated_count }))
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
            <button className="icon-button settings-trigger" aria-label={t('nav.settings')} title={t('nav.settings')} onClick={() => setSettingsOpen(true)}><Icon name="settings" size={18} /></button>
            <button className="language-toggle" onClick={toggleLocale} aria-label={t('actions.changeLanguage')}>{t('actions.changeLanguage')}</button>
            <span className="mode-badge"><span className="mode-dot" />{auth.config?.auth_required ? t('app.cloudMode') : t('app.localMode')}</span>
            {auth.session && <button className="text-button topbar-signout" onClick={() => void auth.signOut()}>{t('actions.signOut')}</button>}
            <button className="button button-primary topbar-add" onClick={() => setRunForm({ open: true, run: null })}><Icon name="plus" size={17} /><span>{t('actions.recordRun')}</span></button>
          </div>
        </header>

        <main className="page-content">
          {view === 'stats' && <StatsView runs={runsState.data ?? []} onSelectRun={selectRun} stats={statsState} period={statsPeriod} date={statsDate} onDateChange={setStatsDate} onPeriodChange={setStatsPeriod} onRetry={() => void refreshStats(statsPeriod, statsDate)} onShare={() => openShare({ kind: statsPeriod, date: statsDate })} locale={locale} t={t} />}
          {view === 'runs' && <RunsView onBulkUpdate={handleBulkUpdate} runs={runsState} shoes={shoesState.data ?? []} selectedRun={selectedRun} onSelectRun={selectRun} onRetry={() => void refreshRuns()} onNew={() => setRunForm({ open: true, run: null })} onEdit={(run) => setRunForm({ open: true, run })} onDelete={handleDeleteRun} onShare={(run) => openShare({ kind: 'run', runId: run.id })} locale={locale} t={t} />}
          {view === 'coach' && <CoachDashboard runs={runsState.data ?? []} onSelectRun={selectRun} locale={locale} t={t} />}
          {view === 'shoes' && <ShoesView shoes={shoesState} runs={runsState.data ?? []} onRetry={() => void refreshShoes()} onNew={() => setShoeForm({ open: true, shoe: null })} onEdit={(shoe) => setShoeForm({ open: true, shoe })} onInfer={() => setInferenceOpen(true)} locale={locale} t={t} />}
        </main>
      </div>

      {runForm.open && <RunFormModal run={runForm.run} shoes={shoesState.data ?? []} onClose={() => setRunForm({ open: false, run: null })} onSaved={handleRunSaved} t={t} />}
      {shoeForm.open && <ShoeFormModal shoe={shoeForm.shoe} onClose={() => setShoeForm({ open: false, shoe: null })} onSaved={handleShoeSaved} t={t} />}
      {inferenceOpen && <InferenceDialog onClose={() => setInferenceOpen(false)} onApplied={onInferenceApplied} locale={locale} t={t} />}
      {shareTarget && <ShareDialog runs={runsState.data ?? []} target={shareTarget} onClose={() => setShareTarget(null)} locale={locale} t={t} />}
      {settingsOpen && <Modal title={t('nav.settings')} eyebrow={t('settings.eyebrow')} className="settings-modal-card" onClose={() => setSettingsOpen(false)} t={t}><SettingsView embedded onToast={showToast} onDataChanged={async () => { await Promise.allSettled([refreshRuns(), refreshShoes(), refreshStats(statsPeriod, statsDate)]) }} locale={locale} onToggleLocale={toggleLocale} t={t} /></Modal>}
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
        {VIEWS.map((item) => <button key={item.key} className={classNames('nav-item', view === item.key && 'nav-item-active')} onClick={() => onNavigate(item.key)}><Icon name={item.icon} size={19} /><span>{t(`nav.${item.key}`)}</span></button>)}
      </nav>
      <div className="sidebar-spacer" />
      <div className="sidebar-note"><div className="note-icon"><Icon name="spark" size={17} /></div><div><strong>{t('app.mottoTitle')}</strong><span>{t('app.mottoCopy')}</span></div></div>
      <div className="sidebar-footer"><div className="avatar">R</div><div className="profile-copy"><strong>{t('app.localAthlete')}</strong><span>{t('app.savedInWorkspace')}</span></div><Icon name="chevron-down" size={16} /></div>
    </aside>
  )
}

function MetricCard({ label, value, unit, icon, accent, detail }: { label: string; value: string; unit: string; icon: IconName; accent: string; detail?: string }) { return <div className="metric-card"><div className={classNames('metric-icon', `metric-${accent}`)}><Icon name={icon} size={18} /></div><div className="metric-label">{label}</div><div className="metric-value">{value}<small>{unit}</small></div>{detail && <div className="metric-detail">{detail}</div>}</div> }

function StatsView({ runs, onSelectRun, stats, period, date, onDateChange, onPeriodChange, onRetry, onShare, locale, t }: { runs: Run[]; onSelectRun: (run: Run) => void; stats: ResourceState<Stats>; period: 'week' | 'month'; date: string; onDateChange: (date: string) => void; onPeriodChange: (period: 'week' | 'month') => void; onRetry: () => void; onShare: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const data = stats.data
  return <div className="view-stack"><section className="page-intro-row"><div><h2>{t('nav.stats')}</h2></div><div className="stats-controls"><StatsPeriodPicker period={period} date={date} onPeriodChange={onPeriodChange} onDateChange={onDateChange} locale={locale} t={t} /><button className="button button-secondary" onClick={onShare}><Icon name="external" size={15} />{t('actions.share')}</button></div></section>{stats.status === 'loading' && !data && <><div className="metric-grid">{[1, 2, 3, 4].map((item) => <div className="skeleton metric-skeleton" key={item} />)}</div><div className="skeleton tall-skeleton" /></>}{stats.status === 'error' && !data && <ErrorState message={stats.error ?? t('errors.loadStats')} onRetry={onRetry} t={t} />}{data && <><section className="metric-grid stats-metric-grid"><MetricCard label={t('stats.totalDistance')} value={formatNumber(data.total_distance_km, locale)} unit="km" icon="activity" accent="green" /><MetricCard label={t('stats.runCount')} value={String(data.run_count)} unit={t('stats.count')} icon="chart" accent="amber" /><MetricCard label={t('stats.totalDuration')} value={formatNumber(data.total_duration_seconds / 3600, locale)} unit={t('time.hours', { count: '' })} icon="clock" accent="blue" /><MetricCard label={t('stats.averagePace')} value={data.average_pace_seconds ? formatMinutes(data.average_pace_seconds) : '—'} unit={data.average_pace_seconds ? t('time.perKm') : ''} icon="zap" accent="purple" /></section><TrainingCalendar runs={runs} onSelectRun={onSelectRun} period={period} date={date} onDateChange={onDateChange} locale={locale} t={t} /></>}</div>
}

function displayedRunType(run: Partial<Run>) {
  if (run.run_type && run.run_type !== 'run') return run.run_type
  const title = run.title ?? ''
  if (/interval|\d+\s*(m|km)\s*[x×]\s*\d+|\d+\s*[x×]\s*\d+\s*(m|km)/i.test(title)) return 'interval'
  if (/tempo|threshold/i.test(title)) return 'tempo'
  if (/long run/i.test(title)) return 'long'
  if (/easy|recovery/i.test(title)) return 'easy'
  if (/treadmill/i.test(title)) return 'treadmill'
  return 'run'
}
function runDisplayTitle(run: Partial<Run>, _locale: 'en' | 'zh', t: Translate) {
  return runTypeLabel(displayedRunType(run), t)
}

function RunsView({ runs, shoes, selectedRun, onSelectRun, onRetry, onNew, onEdit, onDelete, onShare, onBulkUpdate, locale, t }: { runs: ResourceState<Run[]>; shoes: Shoe[]; selectedRun: Run | null; onSelectRun: (run: Run) => void; onRetry: () => void; onNew: () => void; onEdit: (run: Run) => void; onDelete: (run: Run) => void; onShare: (run: Run) => void; onBulkUpdate: (ids: RunId[], patch: { run_type?: string; shoe_id?: RunId | null }) => Promise<void>; locale: 'en' | 'zh'; t: Translate }) {
  const shoeName = shoes.find(shoe => String(shoe.id) === String(selectedRun?.shoe_id))?.name
  return <div className="view-stack runs-clean">
    <section className="page-intro-row"><div><h2>{t('nav.runs')}</h2><p className="lede">{t('runs.realRecords', {count: (runs.data ?? []).filter(run => !/demo|sample/i.test(run.source)).length})}</p></div></section>
    {runs.status === 'loading' && !runs.data && <div className="skeleton tall-skeleton" />}
    {runs.status === 'error' && <ErrorState message={runs.error ?? t('errors.loadRuns')} onRetry={onRetry} t={t} />}
    <section className={classNames('runs-workspace', selectedRun && 'runs-workspace-selected')}>
      <RunLibrary runs={runs.data ?? []} shoes={shoes} selectedRunId={selectedRun?.id ?? null} onSelectRun={onSelectRun} onBulkUpdate={onBulkUpdate} locale={locale} t={t} />
      {selectedRun && <RunDetailPanel run={selectedRun} shoeName={shoeName} onEdit={onEdit} onDelete={onDelete} onShare={onShare} locale={locale} t={t} />}
    </section>
  </div>
}

function RunDetailPanel({ run, shoeName, onEdit, onDelete, onShare, locale, t }: { run: Run | null; shoeName?: string; onEdit: (run: Run) => void; onDelete: (run: Run) => void; onShare: (run: Run) => void; locale: 'en' | 'zh'; t: Translate }) {
  const [streamsState, setStreamsState] = useState<ResourceState<RunStreams>>(emptyResource)
  const [importing, setImporting] = useState(false)
  const [weatherRevision, setWeatherRevision] = useState(0)
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
    if (!run) { setStreamsState(emptyResource()); return undefined }
    setStreamsState({ data: null, status: 'loading', error: null })
    void loadStreams(run.id)
    return () => { requestRef.current += 1 }
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
      setWeatherRevision(value => value + 1)
    } catch (error) {
      setStreamsState((current) => ({ ...current, status: 'error', error: errorMessage(error, t) }))
    } finally {
      setImporting(false)
    }
  }
  if (!run) return <aside className="detail-empty"><div className="detail-empty-icon"><Icon name="activity" size={21} /></div><strong>{t('runs.selectRun')}</strong><span>{t('runs.selectRunCopy')}</span></aside>
  return <aside className="card run-detail"><div className="detail-top"><div className="detail-actions"><button className="icon-button" aria-label={t('actions.share')} onClick={() => onShare(run)}><Icon name="external" size={17} /></button><button className="icon-button" aria-label={t('actions.edit')} onClick={() => onEdit(run)}><Icon name="edit" size={17} /></button><button className="icon-button danger-icon" aria-label={t('actions.delete')} onClick={() => onDelete(run)}><Icon name="trash" size={17} /></button></div></div><h3>{runDisplayTitle(run, locale, t)}</h3><p className="detail-date">{formatRunDateTime(run, locale)}</p><div className="detail-stat-grid"><div><span>{t('runs.distanceStat')}</span><strong>{formatDistance(run.distance_km, locale)}</strong></div><div><span>{t(run.moving_seconds != null ? 'runs.movingTime' : 'runs.elapsedTime')}</span><strong>{formatDuration(run.moving_seconds ?? run.duration_seconds, t)}</strong></div><div><span>{t('runs.paceStat')}</span><strong>{formatPace(run.distance_km > 0 ? (run.moving_seconds ?? run.duration_seconds) / run.distance_km : null, t)}</strong></div><div><span>{t('runs.heartRate')}</span><strong>{run.avg_hr ? `${Math.round(run.avg_hr)} bpm` : '—'}</strong></div></div>{(run.moving_seconds != null || run.elapsed_seconds != null || run.stopped_seconds != null) && <div className="detail-time-row"><span>{t('runs.elapsedTime')} <strong>{formatDuration(run.elapsed_seconds ?? run.duration_seconds, t)}</strong></span></div>}{shoeName && <div className="detail-meta-row"><span>{t('runs.shoe')}</span><strong><Icon name="shoe" size={14} />{shoeName}</strong></div>}{run.rpe && <div className="detail-meta-row"><span>{t('runs.effort')}</span><strong>{run.rpe} / 10</strong></div>}{run.notes && <div className="detail-notes"><span>{t('runs.trainingNotes')}</span><p>{run.notes}</p></div>}<RunWeather key={`${run.id}:${run.started_at}:${weatherRevision}`} runId={run.id} t={t} /><RunFitness key={`${run.id}:${run.started_at}:${weatherRevision}`} runId={run.id} t={t} /><div className="streams-block"><div className="streams-heading"><label className="text-button"><Icon name="upload" size={15} />{importing ? t('actions.loading') : t('runs.importStreams')}<input ref={fileRef} type="file" accept="application/json,.json" hidden onChange={importStreams} /></label></div>{streamsState.status === 'loading' && <div className="analysis-loading"><span /><span /><span /></div>}{streamsState.status === 'error' && <p className="analysis-error">{streamsState.error}</p>}{streamsState.status === 'success' && streamsState.data && <RunStreamsView run={run} streams={streamsState.data} locale={locale} t={t} />}</div></aside>
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
    label: lap.label === 'DISTANCE' ? index + 1 : lap.label ?? lap.lap ?? lap.split ?? index + 1,
    distanceKm,
    duration: intervalDuration > 0 ? intervalDuration : null,
    pace: finiteNumber(lap.pace_seconds) ?? (distanceKm && intervalDuration > 0 ? (finiteNumber(lap.moving_seconds) ?? intervalDuration) / distanceKm : null),
  }
}

function RunStreamsView({ run, streams, locale, t }: { run: Run; streams: RunStreams; locale: 'en' | 'zh'; t: Translate }) {
  const samples = streams.samples ?? []
  const analysis = streamAnalysis(streams)
  const laps = streams.laps ?? []
  const available = streams.available ?? streams.stream_available ?? run.stream_available ?? run.streams_available ?? samples.length > 0
  const analysisSplits = analysisArray(analysis.splits)
  const analysisIntervals = analysisArray(analysis.intervals)
  const displayLaps = laps.length > 0 ? laps : analysisSplits.map(split => ({...split, pace_seconds: finiteNumber(split.pace_seconds) == null ? null : Number(split.pace_seconds) * 1000})) as RunLap[]
  const elapsed = finiteNumber(analysis.elapsed_seconds)
  const moving = finiteNumber(analysis.moving_seconds)
  const stopped = finiteNumber(analysis.stopped_seconds)
  const movingPaceRaw = finiteNumber(analysis.moving_pace_seconds)
  const movingPace = movingPaceRaw == null ? null : movingPaceRaw * 1000
  const distanceKm = finiteNumber(analysis.distance_m) == null ? null : (finiteNumber(analysis.distance_m) as number) / 1000
  if (!available) return <p className="streams-empty">{t('runs.streamUnavailable')}</p>
  return <div className="streams-content">{samples.length >= 2 ? <RunTelemetry samples={samples} laps={laps} locale={locale} t={t} /> : <p className="streams-empty">{t('runs.streamUnavailable')}</p>}{displayLaps.length > 0 && <div className="lap-table"><div className="lap-header"><span>{t('runs.split')}</span><span>{t('runs.distance')}</span><span>{t('runs.durationPace')}</span></div>{displayLaps.map((lap, index) => { const normalized = normalizeLap(lap, index); return <div className="lap-row" key={`${String(normalized.label)}-${index}`}><span>{normalized.label}</span><span>{normalized.distanceKm == null ? '—' : formatDistance(normalized.distanceKm, locale)}</span><span>{normalized.duration == null ? '—' : `${formatDuration(normalized.duration, t)} · ${formatPace(normalized.pace, t)}`}</span></div> })}</div>}{analysisIntervals.length === 0 && analysisSplits.length === 0 && displayLaps.length === 0 && <p className="streams-empty">{t('runs.noIntervals')}</p>}</div>
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
  useEffect(() => setFailed(false), [src])
  if (!src || failed) return <div className={classNames('shoe-image-fallback', className)} aria-label={t('shoes.fallback')}><svg viewBox="0 0 280 140" width="240" role="img" aria-label={t('shoes.fallback')}><path d="M25 88 Q32 68 42 39 L74 61 Q105 70 125 43 L154 70 Q182 79 221 86 Q248 88 256 108 L29 108Z" fill="#8ab6a9" stroke="#385c52" strokeWidth="3"/><path d="M26 103 Q143 115 257 103 L253 120 Q140 136 26 118Z" fill="#f4eee2" stroke="#385c52" strokeWidth="3"/><path d="M116 64 L143 79 M104 72 L129 87 M88 76 L112 92" stroke="white" strokeWidth="5" strokeLinecap="round"/></svg><small>{t('shoes.illustration')}</small></div>
  return <img className={classNames('shoe-image', className)} src={src} alt={alt} onError={() => setFailed(true)} />
}

function ShoesView({ shoes, runs, onRetry, onNew, onEdit, onInfer, locale, t }: { shoes: ResourceState<Shoe[]>; runs: Run[]; onRetry: () => void; onNew: () => void; onEdit: (shoe: Shoe) => void; onInfer: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const runCountByShoe = useMemo(() => {
    const counts = new Map<string, number>()
    runs.forEach((run) => { if (run.shoe_id != null) counts.set(String(run.shoe_id), (counts.get(String(run.shoe_id)) ?? 0) + 1) })
    return counts
  }, [runs])
  return <div className="view-stack"><section className="page-intro-row"><div><div className="eyebrow">{t('shoes.eyebrow')}</div><h2>{t('shoes.headline')}</h2><p className="lede">{t('shoes.lede')}</p></div><div className="intro-actions"><button className="button button-secondary" onClick={onInfer}><Icon name="spark" size={16} />{t('shoes.inference')}</button><button className="button button-primary" onClick={onNew}><Icon name="plus" size={17} />{t('actions.addShoe')}</button></div></section>{shoes.status === 'loading' && !shoes.data && <div className="shoe-grid">{[1, 2, 3].map((item) => <div className="skeleton shoe-skeleton" key={item} />)}</div>}{shoes.status === 'error' && !shoes.data && <ErrorState message={shoes.error ?? t('errors.loadShoes')} onRetry={onRetry} t={t} />}{shoes.status === 'success' && !shoes.data?.length && <section className="card inline-empty-card"><div className="empty-card-icon"><Icon name="shoe" size={25} /></div><h3>{t('shoes.noShoes')}</h3><p>{t('shoes.emptyCopy')}</p><button className="button button-primary" onClick={onNew}><Icon name="plus" size={16} />{t('actions.addFirstShoe')}</button></section>}{shoes.data && shoes.data.length > 0 && <div className="shoe-grid">{shoes.data.map((shoe) => { const total = shoe.total_distance_km ?? 0; const progress = Math.min(100, total / 8); return <article className="card shoe-card" key={String(shoe.id)}><div className="shoe-card-head"><span className={classNames('status-pill', shoeStatusClass(shoe.status))}><span />{shoeStatusLabel(shoe.status, t)}</span><button className="icon-button" aria-label={`${t('actions.edit')} ${shoe.name}`} onClick={() => onEdit(shoe)}><Icon name="edit" size={17} /></button></div><div className="shoe-illustration"><div className="shoe-sole" /><ShoeImage src={shoe.image_url} alt={shoe.name} t={t} /></div><div className="shoe-brand">{shoe.brand || t('shoes.brandUnset')}</div><h3>{shoe.name || t('shoes.nameUnset')}</h3><div className="shoe-meter-label"><span>{t('shoes.totalMileage')}</span><strong>{formatDistance(total, locale)}</strong></div><div className="shoe-meter"><span style={{ width: `${progress}%` }} /></div><div className="shoe-card-foot"><span>{t('shoes.initialMileage', { distance: formatDistance(shoe.initial_distance_km, locale) })}</span><span>{t('shoes.records', { count: runCountByShoe.get(String(shoe.id)) ?? 0 })}</span></div><ShoeBreakdown shoe={shoe} runs={runs} locale={locale} t={t} />{total >= 640 && total < 800 && <div className="shoe-reminder"><Icon name="info" size={14} />{t('shoes.nearReplacement')}</div>}{total >= 800 && <div className="shoe-reminder shoe-reminder-alert"><Icon name="info" size={14} />{t('shoes.replaceSoon')}</div>}</article> })}<button className="add-shoe-card" onClick={onNew}><span><Icon name="plus" size={22} /></span><strong>{t('actions.addAnotherShoe')}</strong><small>{t('shoes.lede')}</small></button></div>}<div className="quiet-footer-row"><span className="source-caption"><span className="source-dot" />{t('shoes.totalMileage')}</span></div></div>
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
  return <Modal title={shoe ? t('shoes.editShoe') : t('shoes.newShoe')} eyebrow={shoe ? t('shoes.editShoe') : t('shoes.newShoe')} onClose={onClose} t={t}><form className="modal-form" onSubmit={submit}><ShoeCatalogPicker catalog={catalog} onChoose={(item) => setValues((current) => ({ ...current, name: item.name, brand: item.brand, image_url: item.image_url ?? '' }))} labels={{ brand: t('shoes.brand'), model: t('shoes.model'), color: t('shoes.color'), choose: t('shoes.useSelection'), hint: t('shoes.catalog'), preview: t('shoes.representativeImage') }} /><div className="form-grid form-grid-two"><Field label={t('shoes.shoeName')} required t={t}><input value={values.name} onChange={(event) => update('name', event.target.value)} placeholder={t('shoes.shoeNamePlaceholder')} /></Field><Field label={t('shoes.brand')} t={t}><input value={values.brand} onChange={(event) => update('brand', event.target.value)} placeholder="Nike" /></Field><Field label={t('shoes.initialDistance')} hint={t('shoes.initialDistanceHint')} t={t}><input type="number" min="0" step="0.1" value={values.initial_distance_km} onChange={(event) => update('initial_distance_km', event.target.value)} /></Field><Field label={t('shoes.purchaseDate')} t={t}><input type="date" value={values.purchase_date} onChange={(event) => update('purchase_date', event.target.value)} /></Field><Field label={t('shoes.status')} t={t}><select value={values.status} onChange={(event) => update('status', event.target.value)}>{SHOE_STATUS_VALUES.map((value) => <option value={value} key={value}>{shoeStatusLabel(value, t)}</option>)}</select></Field><Field label={t('shoes.imageUrl')} hint={t('shoes.imageUrlHint')} t={t}><input value={values.image_url} onChange={(event) => update('image_url', event.target.value)} placeholder="https://…" /></Field></div><section className="shoe-rules-form"><div className="form-section-heading"><span>{t('shoes.rules')}</span><small>{t('shoes.inferenceCopy')}</small></div><div className="form-grid form-grid-two"><Field label={t('shoes.ruleDistance')} t={t}><div className="range-fields"><input type="number" min="0" step="0.1" value={values.min_distance_km} onChange={(event) => update('min_distance_km', event.target.value)} placeholder={t('shoes.minDistance')} /><input type="number" min="0" step="0.1" value={values.max_distance_km} onChange={(event) => update('max_distance_km', event.target.value)} placeholder={t('shoes.maxDistance')} /></div></Field><Field label={t('shoes.rulePace')} t={t}><div className="range-fields"><input type="number" min="0" step="1" value={values.min_pace_seconds} onChange={(event) => update('min_pace_seconds', event.target.value)} placeholder={t('shoes.minPace')} /><input type="number" min="0" step="1" value={values.max_pace_seconds} onChange={(event) => update('max_pace_seconds', event.target.value)} placeholder={t('shoes.maxPace')} /></div></Field><Field label={t('shoes.priority')} hint={t('shoes.priorityHint')} t={t}><input type="number" step="1" value={values.priority} onChange={(event) => update('priority', event.target.value)} /></Field></div><div className="run-type-checks"><span>{t('shoes.runTypes')}</span><div>{RUN_TYPE_VALUES.map((value) => <label key={value}><input type="checkbox" checked={values.run_types.includes(value)} onChange={(event) => updateRunType(value, event.target.checked)} />{runTypeLabel(value, t)}</label>)}</div><small>{values.run_types.length === 0 ? t('shoes.allRunTypes') : values.run_types.map((value) => runTypeLabel(value, t)).join(', ')}</small></div></section>{formError && <div className="form-error"><Icon name="info" size={16} />{formError}</div>}<div className="modal-footer"><button className="button button-secondary" type="button" onClick={onClose}>{t('actions.cancel')}</button><button className="button button-primary" type="submit" disabled={saving}>{saving ? <><span className="button-spinner" />{t('actions.loading')}</> : shoe ? t('actions.saveChanges') : t('actions.addShoe')}</button></div></form></Modal>
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

function InspectionResult({ data, t }: { data: GoogleHealthInspection | null; t: Translate }) {
  const earliest = data?.earliest_imported_date ?? data?.earliest_accessed_date ?? null
  const coverage = data?.source_coverage_note ?? data?.coverage ?? (data?.coverage_start || data?.coverage_end ? `${data.coverage_start ?? t('settings.unknown')} – ${data.coverage_end ?? t('settings.unknown')}` : null)
  const source = data?.raw_example ?? data?.source_json ?? data?.data ?? null
  return <><div className="inspection-meta"><span>{t('settings.earliestAccessed')}<strong>{earliest ?? t('settings.unknown')}</strong></span><span>{t('settings.coverage')}<strong>{coverage ?? t('settings.unknown')}</strong></span><span>{t('settings.dataSources')}<strong>{data?.available_types?.length ? data.available_types.join(', ') : t('settings.unknown')}</strong></span></div>{source ? <pre className="source-json">{JSON.stringify(source, null, 2)}</pre> : <p className="streams-empty">{t('settings.notAvailable')}</p>}</>
}

function SettingsView({ onToast, onDataChanged, locale, onToggleLocale, t, embedded = false }: { onToast: (message: string, tone?: Toast['tone']) => void; onDataChanged: () => Promise<void>; locale: 'en' | 'zh'; onToggleLocale: () => void; t: Translate; embedded?: boolean }) {
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
  return <div className="view-stack settings-view">{!embedded && <section className="page-intro-row"><div><div className="eyebrow">{t('settings.eyebrow')}</div><h2>{t('settings.headline')}</h2><p className="lede">{t('settings.lede')}</p></div></section>}<section className="settings-section"><div className="settings-section-heading"><div><span className="section-eyebrow">{t('settings.dataSources')}</span><h3>{t('settings.sourcesTitle')}</h3><p>{t('settings.sourcesCopy')}</p></div><button className="text-button" onClick={() => void loadIntegrations()}><Icon name="refresh" size={15} />{t('actions.refresh')}</button></div>{integrations.status === 'loading' && !integrations.data && <div className="integration-grid"><div className="skeleton integration-skeleton" /><div className="skeleton integration-skeleton" /></div>}{integrations.status === 'error' && !integrations.data && <ErrorState compact message={integrations.error ?? t('errors.loadIntegrations')} onRetry={() => void loadIntegrations()} t={t} />}{integrations.data && <div className="integration-grid">{sourceRecords.length ? sourceRecords.map((integration) => <IntegrationCard key={integration.id} integration={integration} t={t} />) : <IntegrationCard integration={{ id: 'local', name: t('settings.localRecords'), status: 'connected', message: t('settings.localRecordsCopy') }} t={t} />}</div>}</section><section className="google-health-panel card"><div className="google-health-heading"><div className="pending-icon"><Icon name="activity" size={19} /></div><div><span className="section-eyebrow">{t('settings.googleHealth')}</span><h3>{google?.name ?? t('settings.googleHealth')}</h3><p>{googleDescription}</p></div><span className={classNames('integration-status', googleConnected && 'integration-status-connected')}><span />{googleConnected ? t('settings.connected') : google?.status === 'not_connected' ? t('settings.notConnected') : t('settings.notConfigured')}</span></div><div className="google-health-actions"><button className="button button-secondary" onClick={() => void connectGoogle()}><Icon name="external" size={16} />{t('actions.connect')}</button>{googleConnected && <form className="sync-form" onSubmit={syncGoogle}><label><span>{t('settings.startDate')}</span><input type="date" value={syncStartDate} onChange={(event) => setSyncStartDate(event.target.value)} /></label><label className="checkbox-field"><input type="checkbox" checked={fullHistory} onChange={(event) => setFullHistory(event.target.checked)} />{t('settings.fullHistory')}</label><button className="button button-primary" type="submit" disabled={syncing}>{syncing ? <><span className="button-spinner" />{t('actions.loading')}</> : t('actions.sync')}</button></form>}</div><div className="inspection-block"><div className="inspection-heading"><div><span className="section-eyebrow">{t('settings.inspectionTitle')}</span><strong>{t('settings.inspectionCopy')}</strong></div>{inspection.status === 'loading' && <span className="inspection-status">{t('actions.loading')}</span>}</div>{inspection.status === 'error' && <p className="analysis-error">{inspection.error}</p>}{inspection.status === 'success' && <InspectionResult data={inspection.data} t={t} />}</div></section><section className="settings-section"><div className="settings-section-heading"><div><span className="section-eyebrow">{t('settings.portability')}</span><h3>{t('settings.portabilityTitle')}</h3><p>{t('settings.portabilityCopy')}</p></div></div><div className="portability-grid"><div className="portability-card"><div className="portability-icon"><Icon name="upload" size={19} /></div><div><strong>{t('settings.importCsv')}</strong><p>{t('settings.importCopy')}</p>{importResult && <span className="import-result"><Icon name="check" size={14} />{t('settings.lastImport', { imported: importResult.imported, skipped: importResult.skipped })}</span>}</div><button className="button button-secondary" onClick={() => fileRef.current?.click()} disabled={importing}>{importing ? <><span className="button-spinner" />{t('settings.importing')}</> : t('settings.chooseCsv')}</button><input ref={fileRef} type="file" accept=".csv,text/csv" onChange={onFile} hidden /></div><div className="portability-card"><div className="portability-icon portability-icon-green"><Icon name="download" size={19} /></div><div><strong>{t('settings.exportJson')}</strong><p>{t('settings.exportCopy')}</p></div><button className="button button-secondary" onClick={() => void exportData()}><Icon name="download" size={16} />{t('settings.downloadBackup')}</button></div></div></section><section className="settings-section"><div className="settings-section-heading"><div><span className="section-eyebrow">{t('share.title')}</span><h3>{t('actions.share')}</h3><p>{t('share.copy')}</p></div></div>{shares.status === 'loading' && !shares.data && <div className="skeleton integration-skeleton" />}{shares.status === 'error' && !shares.data && <ErrorState compact message={shares.error ?? t('errors.generic')} onRetry={() => void loadShares()} t={t} />}{shares.data && (shares.data.length ? <div className="shares-list">{shares.data.map((share, index) => <div className="share-row" key={String(share.id ?? share.token ?? index)}><div><strong>{share.kind === 'run' ? t('share.publicRun') : share.kind === 'week' ? t('share.publicWeek') : t('share.publicMonth')}</strong><small>{share.expires_at ? t('share.expiresAt', { date: formatDateTime(share.expires_at, locale) }) : t('settings.unknown')}</small></div><button className="text-button danger-text" onClick={() => void revoke(share)}>{t('actions.revoke')}</button></div>)}</div> : <InlineEmpty title={t('settings.notAvailable')} copy={t('share.copy')} />)}</section><section className="language-card card"><div><span className="section-eyebrow">{t('settings.language')}</span><h3>{t('settings.language')}</h3><p>{t('settings.languageCopy')}</p></div><button className="button button-secondary" onClick={onToggleLocale}>{t('actions.changeLanguage')}</button></section>{auth.session && <div className="settings-footnote"><Icon name="database" size={15} />{auth.session.user.email ?? t('app.localAthlete')}<button className="text-button" onClick={() => void auth.signOut()}>{t('actions.signOut')}</button></div>}</div>
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

function ShareDialog({ target, runs, onClose, locale, t }: { target: ShareTarget; runs: Run[]; onClose: () => void; locale: 'en' | 'zh'; t: Translate }) {
  const title = target.kind === 'run' ? t('share.shareRunDetail') : target.kind === 'week' ? t('share.shareWeek') : t('share.shareMonth')
  return <Modal title={title} eyebrow={t('shareImage.preview')} onClose={onClose} t={t}><ShareImage target={target} runs={runs} locale={locale} t={t} /></Modal>
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
  return <main className="public-share-page"><div className="public-share-header"><div className="brand-name">{t('app.name')}</div><button className="language-toggle" onClick={toggleLocale}>{t('actions.changeLanguage')}</button></div>{state.status === 'loading' && <div className="share-loading"><div className="skeleton share-skeleton" /></div>}{state.status === 'error' && <StandaloneError message={state.error ?? t('share.notFound')} />}{state.status === 'success' && snapshot && <section className="public-share-card card"><div className="public-share-title"><div><span className="eyebrow">{t('share.publicTitle')}</span><h1>{kindLabel}</h1><p>{t('share.publicCopy')}</p></div><button className="button button-secondary" onClick={() => void copy()}><Icon name="download" size={16} />{copied ? t('actions.copied') : t('actions.copyLink')}</button></div>{stats && <div className="public-metrics"><MetricCard label={t('stats.totalDistance')} value={stats.total_distance_km == null ? '—' : formatNumber(stats.total_distance_km, locale)} unit="km" icon="activity" accent="green" detail={t('stats.runCount')} /><MetricCard label={t('stats.runCount')} value={stats.run_count == null ? '—' : String(stats.run_count)} unit={t('stats.count')} icon="chart" accent="amber" detail={t('share.publicCopy')} /><MetricCard label={t('stats.totalDuration')} value={stats.total_duration_seconds == null ? '—' : formatNumber(stats.total_duration_seconds / 3600, locale)} unit={t('time.hours', { count: '' })} icon="clock" accent="blue" detail={t('stats.trainingTime')} /></div>}{runs.length > 0 && <div className="public-run-list">{runs.map((run, index) => <article className="public-run-row" key={String(run.id ?? index)}><div><strong>{runDisplayTitle(run, locale, t)}</strong><small>{run.started_at ? formatDateTime(run.started_at, locale) : t('share.date')}</small></div><span>{run.distance_km == null ? '—' : formatDistance(run.distance_km, locale)}</span><span>{run.duration_seconds == null ? '—' : formatDuration(run.duration_seconds, t)}</span></article>)}</div>}</section>}</main>
}

function LoadingScreen() { const { t } = useI18n(); return <main className="standalone-state"><div className="loading-mark"><span /><span /><span /></div><p>{t('app.loading')}</p></main> }

function StandaloneError({ message }: { message: string }) { const { t } = useI18n(); return <main className="standalone-state"><div className="error-icon"><Icon name="refresh" size={18} /></div><strong>{t('errors.generic')}</strong><p>{message}</p></main> }

function Modal({ title, eyebrow, onClose, children, t, className }: { title: string; eyebrow: string; onClose: () => void; children: ReactNode; t: Translate; className?: string }) {
  useEffect(() => { const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }; document.addEventListener('keydown', onKeyDown); const previous = document.body.style.overflow; document.body.style.overflow = 'hidden'; return () => { document.removeEventListener('keydown', onKeyDown); document.body.style.overflow = previous } }, [onClose])
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}><div className={classNames('modal-card', className)} role="dialog" aria-modal="true" aria-label={title}><div className="modal-heading"><div><span className="section-eyebrow">{eyebrow}</span><h2>{title}</h2></div><button className="icon-button" onClick={onClose} aria-label={t('app.close')}><Icon name="close" size={19} /></button></div>{children}</div></div>
}

function Field({ label, required, hint, children }: { label: string; required?: boolean; hint?: string; children: ReactNode; t?: Translate }) { return <label className="form-field"><span>{label}{required && <em>*</em>}</span>{children}{hint && <small>{hint}</small>}</label> }

function ErrorState({ message, onRetry, compact = false, t }: { message: string; onRetry?: () => void; compact?: boolean; t: Translate }) { return <div className={classNames('error-state', compact && 'error-state-compact')}><div className="error-icon"><Icon name="refresh" size={18} /></div><div><strong>{t('errors.generic')}</strong><p>{message}</p>{onRetry && <button className="text-button" onClick={onRetry}>{t('actions.retry')} <Icon name="arrow-right" size={15} /></button>}</div></div> }

function InlineEmpty({ title, copy }: { title: string; copy: string }) { return <div className="inline-empty"><div className="inline-empty-mark"><Icon name="activity" size={18} /></div><strong>{title}</strong><span>{copy}</span></div> }

function ToastMessage({ toast, onClose, t }: { toast: Toast; onClose: () => void; t: Translate }) { return <div className={classNames('toast-message', `toast-${toast.tone}`)} role="status"><span className="toast-icon"><Icon name={toast.tone === 'success' ? 'check' : toast.tone === 'error' ? 'info' : 'spark'} size={16} /></span><span>{toast.message}</span><button className="toast-close" aria-label={t('app.close')} onClick={onClose}><Icon name="close" size={14} /></button></div> }

export default App
