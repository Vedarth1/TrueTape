import { createContext, useContext, useEffect, useState } from 'react'
import api from './api'

// JWT payload decode (base64url). The token carries role + name as
// additional claims, so the client knows who it is without a round trip.
function decodeJwt(token) {
  try {
    const payload = token.split('.')[1]
    const b64 = payload.replace(/-/g, '+').replace(/_/g, '/')
    const pad = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
    return JSON.parse(atob(pad))
  } catch {
    return null
  }
}

const ROLE_HOME = { operator: '/operator', reviewer: '/reviewer', consumer: '/consumer' }

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)       // { id, role, name }
  const [booting, setBooting] = useState(true) // restoring a saved session?

  // On mount: if a token is stored, verify it is still valid (and pull fresh
  // role/name) via /me. An expired token logs the user out cleanly.
  useEffect(() => {
    const token = localStorage.getItem('truetape_token')
    if (!token) { setBooting(false); return }
    api.get('/auth/me')
      .then((res) => setUser(res.data))
      .catch(() => localStorage.removeItem('truetape_token'))
      .finally(() => setBooting(false))
  }, [])

  async function login(email, password) {
    const res = await api.post('/auth/login', { email, password })
    const token = res.data.access_token
    localStorage.setItem('truetape_token', token)
    const claims = decodeJwt(token)
    setUser({ id: claims?.sub ?? null, role: claims?.role, name: claims?.name })
    return ROLE_HOME[claims?.role] ?? '/'
  }

  function logout() {
    localStorage.removeItem('truetape_token')
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, booting, login, logout, roleHome: ROLE_HOME }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
