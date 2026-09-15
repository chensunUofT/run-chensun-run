import shareImageEn from './locales/share-image.en.json'
import shareImageZh from './locales/share-image.zh.json'
import statsPeriodEn from './locales/stats-period.en.json'
import statsPeriodZh from './locales/stats-period.zh.json'
import shoeBreakdownEn from './locales/shoe-breakdown.en.json'
import shoeBreakdownZh from './locales/shoe-breakdown.zh.json'
import fitnessEn from './locales/fitness.en.json'
import fitnessZh from './locales/fitness.zh.json'
import libraryEn from './locales/library.en.json'
import libraryZh from './locales/library.zh.json'
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import en from './locales/en.json'
import zh from './locales/zh.json'
import trainingEn from './locales/training.en.json'
import trainingZh from './locales/training.zh.json'
import telemetryEn from './locales/telemetry.en.json'
import telemetryZh from './locales/telemetry.zh.json'

export type Locale = 'en' | 'zh'
interface MessageTree {
  [key: string]: string | MessageTree
}

const messages: Record<Locale, MessageTree> = { en: {...en,...trainingEn,...telemetryEn,...libraryEn,...shareImageEn,...statsPeriodEn,...shoeBreakdownEn,...fitnessEn}, zh: {...zh,...trainingZh,...telemetryZh,...libraryZh,...shareImageZh,...statsPeriodZh,...shoeBreakdownZh,...fitnessZh} }
const LOCALE_STORAGE_KEY = 'runwise-locale'

function getInitialLocale(): Locale {
  const saved = window.localStorage.getItem(LOCALE_STORAGE_KEY)
  if (saved === 'en' || saved === 'zh') return saved
  return window.navigator.language.toLowerCase().startsWith('zh') ? 'zh' : 'en'
}

function lookup(tree: MessageTree, key: string): string | undefined {
  const value = key.split('.').reduce<unknown>((current, part) => {
    if (!current || typeof current !== 'object') return undefined
    return (current as MessageTree)[part]
  }, tree)
  return typeof value === 'string' ? value : undefined
}

function interpolate(template: string, values?: Record<string, string | number>) {
  if (!values) return template
  return template.replace(/\{(\w+)\}/g, (_, key: string) => String(values[key] ?? `{${key}}`))
}

export type Translate = (key: string, values?: Record<string, string | number>) => string

type I18nContextValue = {
  locale: Locale
  setLocale: (locale: Locale) => void
  toggleLocale: () => void
  t: Translate
}

const I18nContext = createContext<I18nContextValue | null>(null)

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(getInitialLocale)
  useEffect(() => {
    document.documentElement.lang = localeTag(locale)
  }, [locale])
  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next)
    window.localStorage.setItem(LOCALE_STORAGE_KEY, next)
  }, [])
  const toggleLocale = useCallback(() => setLocale(locale === 'en' ? 'zh' : 'en'), [locale, setLocale])
  const t = useCallback<Translate>((key, values) => {
    const template = lookup(messages[locale], key) ?? lookup(messages.en, key) ?? key
    return interpolate(template, values)
  }, [locale])
  const value = useMemo(() => ({ locale, setLocale, toggleLocale, t }), [locale, setLocale, t, toggleLocale])
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n() {
  const value = useContext(I18nContext)
  if (!value) throw new Error('useI18n must be used inside I18nProvider')
  return value
}

export function localeTag(locale: Locale) {
  return locale === 'zh' ? 'zh-CN' : 'en-US'
}
