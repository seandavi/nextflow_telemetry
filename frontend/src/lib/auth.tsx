/**
 * Auth context — fetches /auth/me once on mount and exposes the current
 * user + role to the rest of the SPA.
 *
 * Sign-in is a full-page navigation to ${VITE_API_URL}/auth/login (not a
 * fetch), because the OAuth dance requires top-level browser redirects.
 * After Google → /auth/callback → FRONTEND_URL, the SPA reloads, this
 * provider fetches /auth/me, and the user state becomes truthy.
 *
 * useRole('admin') is the gate for edit/create UI. admin implies
 * contributor, matching the backend require_role dependency.
 */
import {
  createContext, useCallback, useContext, useEffect, useState,
  type ReactNode,
} from 'react'
import { API_BASE } from './api'

export interface AuthUser {
  email: string
  role: 'admin' | 'contributor' | null
}

interface AuthState {
  user: AuthUser | null
  loading: boolean
  refresh: () => Promise<void>
  signIn: () => void
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

// API_BASE includes the /api prefix used by the rest of the app; auth
// routes are mounted at root, so we strip it to land at /auth/...
const AUTH_BASE = API_BASE.replace(/\/api$/, '')

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user,    setUser]    = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const res = await fetch(`${AUTH_BASE}/auth/me`, { credentials: 'include' })
      if (res.ok)        setUser(await res.json() as AuthUser)
      else if (res.status === 401) setUser(null)
      // Other statuses (5xx, network) leave the previous state untouched —
      // a transient blip shouldn't kick the user out.
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const signIn = useCallback(() => {
    window.location.href = `${AUTH_BASE}/auth/login`
  }, [])

  const signOut = useCallback(async () => {
    await fetch(`${AUTH_BASE}/auth/logout`, { method: 'POST', credentials: 'include' })
    setUser(null)
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, refresh, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within <AuthProvider>')
  return ctx
}

/**
 * Returns true when the current user satisfies `required`. Admin implies
 * contributor, matching the backend `require_role` semantics.
 *
 * Use this to gate edit/create UI:
 *
 *   const isAdmin = useRole('admin')
 *   {isAdmin && <Btn>+ Register Workflow</Btn>}
 */
export function useRole(required: 'admin' | 'contributor'): boolean {
  const { user } = useAuth()
  if (!user || user.role == null) return false
  if (user.role === 'admin') return true
  return required === 'contributor' && user.role === 'contributor'
}
