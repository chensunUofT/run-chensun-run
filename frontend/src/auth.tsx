import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import type { Session, SupabaseClient } from '@supabase/supabase-js'
import { api, ApiError, setAccessTokenProvider } from './lib/api'
import type { AuthConfig } from './types'

type AuthState = {
  config: AuthConfig | null
  client: SupabaseClient | null
  session: Session | null
  serverAuthenticated: boolean
  loading: boolean
  error: Error | null
  signInWithGoogle: () => Promise<void>
  signInWithPassword: (email: string, password: string) => Promise<void>
  signUpWithPassword: (email: string, password: string) => Promise<void>
  sendPasswordReset: (email: string) => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

const localConfig: AuthConfig = {
  auth_required: false,
  supabase_url: null,
  supabase_publishable_key: null,
}

async function loadConfig() {
  try {
    return await api.getConfig()
  } catch (error) {
    // The original local API predates the public config endpoint. A 404 is a
    // useful local-development signal; all other failures remain visible.
    if (error instanceof ApiError && error.status === 404) return localConfig
    throw error
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<AuthConfig | null>(null)
  const [client, setClient] = useState<SupabaseClient | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [serverAuthenticated, setServerAuthenticated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    let active = true
    let unsubscribe: (() => void) | undefined
    void loadConfig()
      .then(async (nextConfig) => {
        if (!active) return
        setConfig(nextConfig)
        if (!nextConfig.auth_required) {
          setLoading(false)
          return
        }
        if (!nextConfig.supabase_url || !nextConfig.supabase_publishable_key) {
          // Personal mode uses the backend's HttpOnly same-origin session.
          // Validate it through the private API; the cookie never enters JS
          // storage or the Supabase client.
          void api.getMe().then(() => {
            if (!active) return
            setServerAuthenticated(true)
            setLoading(false)
          }).catch((authError: unknown) => {
            if (!active) return
            if (!(authError instanceof ApiError && (authError.status === 401 || authError.status === 403))) {
              setError(authError instanceof Error ? authError : new Error('Unable to validate the workspace session'))
            }
            setLoading(false)
          })
          return
        }
        // Keep the optional multi-user auth path out of the local/personal
        // bundle. Production only loads Supabase after the public config
        // explicitly enables it.
        const { createClient } = await import('@supabase/supabase-js')
        if (!active) return
        const supabase = createClient(nextConfig.supabase_url, nextConfig.supabase_publishable_key, {
          auth: {
            persistSession: true,
            autoRefreshToken: true,
            detectSessionInUrl: true,
          },
        })
        setClient(supabase)
        setAccessTokenProvider(async () => {
          const result = await supabase.auth.getSession()
          return result.data.session?.access_token ?? null
        })
        void supabase.auth.getSession().then(({ data }) => {
          if (!active) return
          setSession(data.session)
          setLoading(false)
        }).catch((sessionError: unknown) => {
          if (!active) return
          setError(sessionError instanceof Error ? sessionError : new Error('Unable to read the sign-in session'))
          setLoading(false)
        })
        const subscription = supabase.auth.onAuthStateChange((_event, nextSession) => {
          if (active) setSession(nextSession)
        })
        unsubscribe = () => subscription.data.subscription.unsubscribe()
      })
      .catch((configError: unknown) => {
        if (!active) return
        setError(configError instanceof Error ? configError : new Error('Unable to load workspace configuration'))
        setLoading(false)
      })
    return () => {
      active = false
      unsubscribe?.()
      setAccessTokenProvider(null)
    }
  }, [])

  const value = useMemo<AuthState>(() => ({
    config,
    client,
    session,
    serverAuthenticated,
    loading,
    error,
    signInWithGoogle: async () => {
      if (!client) throw new Error('Cloud sign-in is not configured')
      const result = await client.auth.signInWithOAuth({
        provider: 'google',
        options: { redirectTo: window.location.origin },
      })
      if (result.error) throw result.error
    },
    signInWithPassword: async (email, password) => {
      if (!client) throw new Error('Cloud sign-in is not configured')
      const result = await client.auth.signInWithPassword({ email, password })
      if (result.error) throw result.error
    },
    signUpWithPassword: async (email, password) => {
      if (!client) throw new Error('Cloud sign-in is not configured')
      const result = await client.auth.signUp({
        email,
        password,
        options: { emailRedirectTo: window.location.origin },
      })
      if (result.error) throw result.error
    },
    sendPasswordReset: async (email) => {
      if (!client) throw new Error('Cloud sign-in is not configured')
      const result = await client.auth.resetPasswordForEmail(email, { redirectTo: window.location.origin })
      if (result.error) throw result.error
    },
    signOut: async () => {
      if (!client) return
      const result = await client.auth.signOut()
      if (result.error) throw result.error
    },
  }), [client, config, error, loading, serverAuthenticated, session])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside AuthProvider')
  return value
}
