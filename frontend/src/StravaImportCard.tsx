import { useRef, useState, type ChangeEvent } from 'react'
import { Icon } from './icons'
import { useI18n, type Translate } from './i18n'
import { ApiError, api } from './lib/api'
import type { StravaImportResult } from './types'

const MAX_UPLOAD_BYTES = 100 * 1024 * 1024
const STRAVA_EXPORT_HELP_URL = 'https://support.strava.com/en-us/articles/15401919-how-do-i-export-my-strava-data'
const STRAVA_ACCOUNT_URL = 'https://www.strava.com/account'

type StravaImportCardProps = {
  onDataChanged: () => Promise<void>
  onToast: (message: string, tone?: 'success' | 'error' | 'info') => void
  t: Translate
}

function getImportError(error: unknown, t: Translate) {
  if (error instanceof ApiError) {
    if (error.status === 413) return t('settings.stravaFileTooLarge')
    if (error.message && !/^Request failed \(/.test(error.message)) return error.message
    if (error.status >= 500) return t('errors.server')
  }
  return t('settings.stravaImportError')
}

function fileSizeInMb(file: File) {
  return Math.max(0.1, Math.round((file.size / (1024 * 1024)) * 10) / 10)
}

export function StravaImportCard({ onDataChanged, onToast, t }: StravaImportCardProps) {
  const { locale } = useI18n()
  const fileRef = useRef<HTMLInputElement>(null)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [result, setResult] = useState<StravaImportResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)

  const onFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] ?? null
    event.target.value = ''
    setError(null)
    setResult(null)
    if (!file) return
    const name = file.name.toLowerCase()
    if (!name.endsWith('.zip') && !name.endsWith('.csv')) {
      setSelectedFile(null)
      setError(t('settings.stravaFileTypeError'))
      return
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setSelectedFile(null)
      setError(t('settings.stravaFileTooLarge'))
      return
    }
    setSelectedFile(file)
  }

  const importFile = async () => {
    if (!selectedFile || importing) return
    setError(null)
    setImporting(true)
    try {
      const imported = await api.importStrava(selectedFile)
      setResult({ ...imported, warnings: imported.warnings ?? [] })
      await Promise.allSettled([onDataChanged()])
      onToast(t('settings.stravaImportComplete', { imported: imported.imported, matched: imported.matched, skipped: imported.skipped }))
    } catch (requestError) {
      setError(getImportError(requestError, t))
      onToast(getImportError(requestError, t), 'error')
    } finally {
      setImporting(false)
    }
  }

  return <section className="strava-import-card card" aria-labelledby="strava-import-title">
    <div className="strava-import-heading">
      <div className="strava-import-icon"><Icon name="upload" size={21} /></div>
      <div>
        <span className="section-eyebrow">{t('settings.stravaEyebrow')}</span>
        <h3 id="strava-import-title">{t('settings.stravaTitle')}</h3>
        <p>{t('settings.stravaCopy')}</p>
      </div>
      <span className="strava-import-badge">{t('settings.stravaNoApi')}</span>
    </div>

    <div className="strava-import-help">
      <strong>{t('settings.stravaExportTitle')}</strong>
      <p>{t('settings.stravaExportSteps')}</p>
      <p>{t('settings.stravaExportContents')}</p>
      <div className="strava-import-links">
        <a href={STRAVA_ACCOUNT_URL} target="_blank" rel="noreferrer"><Icon name="external" size={14} />{t('settings.stravaRequestExport')}</a>
        <a href={STRAVA_EXPORT_HELP_URL} target="_blank" rel="noreferrer"><Icon name="external" size={14} />{t('settings.stravaExportHelp')}</a>
      </div>
    </div>

    <div className="strava-import-controls">
      <div className="strava-file-copy">
        <button className="button button-secondary" type="button" onClick={() => fileRef.current?.click()} disabled={importing}>
          <Icon name="file" size={16} />{t('settings.stravaChooseFile')}
        </button>
        <input ref={fileRef} type="file" accept=".zip,.csv,application/zip,text/csv,application/x-zip-compressed" onChange={onFile} hidden />
        {selectedFile && <span className="strava-file-name" title={selectedFile.name}>{t('settings.stravaSelectedFile', { name: selectedFile.name, size: fileSizeInMb(selectedFile) })}</span>}
      </div>
      <button className="button button-primary" type="button" onClick={() => void importFile()} disabled={!selectedFile || importing}>
        {importing ? <><span className="button-spinner" />{t('settings.stravaImporting')}</> : <><Icon name="upload" size={16} />{t('settings.stravaImport')}</>}
      </button>
    </div>

    {error && <p className="strava-import-error" role="alert"><Icon name="info" size={15} />{error}</p>}
    {result && <div className="strava-import-result" role="status">
      <div className="strava-result-heading"><strong>{t('settings.stravaImportReport')}</strong><span>{t('settings.stravaReportFile', { name: selectedFile?.name ?? t('settings.stravaExportFile') })}</span></div>
      <div className="strava-result-grid">
        <span><small>{t('settings.stravaImported')}</small><strong>{result.imported.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US')}</strong></span>
        <span><small>{t('settings.stravaMatched')}</small><strong>{result.matched.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US')}</strong></span>
        <span><small>{t('settings.stravaSkipped')}</small><strong>{result.skipped.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US')}</strong></span>
      </div>
      <div className="strava-warnings"><strong>{t('settings.stravaWarnings')}</strong>{result.warnings.length > 0 ? <ul>{result.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul> : <p>{t('settings.stravaNoWarnings')}</p>}</div>
    </div>}
  </section>
}
