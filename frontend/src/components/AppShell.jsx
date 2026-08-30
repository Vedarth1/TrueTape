import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/auth'
import HealthBadge from './HealthBadge'

const NAV_BY_ROLE = {
  operator: [
    { to: '/operator', label: 'Ingestion' },
    { to: '/consumer', label: 'Verified records' },
  ],
  reviewer: [
    { to: '/reviewer', label: 'Exception queue' },
    { to: '/rules', label: 'Rule studio' },
    { to: '/consumer', label: 'Verified records' },
  ],
  consumer: [
    { to: '/consumer', label: 'Verified records' },
  ],
}

const ROLE_STYLES = {
  operator: 'bg-sky-50 text-sky-700',
  reviewer: 'bg-violet-50 text-violet-700',
  consumer: 'bg-emerald-50 text-emerald-700',
}

export default function AppShell() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const nav = NAV_BY_ROLE[user?.role] ?? []

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/95 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-6">
            <span className="font-semibold tracking-tight">TrueTape</span>
            <nav className="flex gap-1 text-sm">
              {nav.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `rounded-lg px-3 py-1.5 transition ${
                      isActive
                        ? 'bg-slate-900 text-white'
                        : 'text-slate-600 hover:bg-slate-100'
                    }`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
            </nav>
          </div>
          <div className="flex items-center gap-3">
            <HealthBadge />
            <span
              className={`rounded px-2 py-1 text-xs font-medium capitalize ${ROLE_STYLES[user?.role] ?? ''}`}
            >
              {user?.role}
            </span>
            <span className="hidden text-sm text-slate-600 sm:inline">{user?.name}</span>
            <button
              onClick={handleLogout}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:border-slate-400 hover:text-slate-900"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-6">
        <Outlet />
      </main>
    </div>
  )
}
