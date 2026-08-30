import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../lib/auth'

const DEMO_ACCOUNTS = [
  { role: 'operator', email: 'operator@truetape.dev', password: 'operator123', blurb: 'upload files, run the pipeline' },
  { role: 'reviewer', email: 'reviewer@truetape.dev', password: 'reviewer123', blurb: 'resolve exceptions, verify loans' },
  { role: 'consumer', email: 'consumer@truetape.dev', password: 'consumer123', blurb: 'browse verified records' },
]

export default function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const home = await login(email, password)
      navigate(home, { replace: true })
    } catch (err) {
      setError(err.message ?? 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  function quickFill(account) {
    setEmail(account.email)
    setPassword(account.password)
  }

  return (
    <div className="flex min-h-[80vh] items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <div className="text-2xl font-semibold tracking-tight">TrueTape</div>
          <p className="mt-1 text-sm text-slate-500">
            Loan data verification copilot — sign in to your workspace
          </p>
        </div>

        <form onSubmit={submit} className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <label className="block text-sm font-medium text-slate-700" htmlFor="email">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
            placeholder="you@truetape.dev"
            autoComplete="username"
          />

          <label className="mt-4 block text-sm font-medium text-slate-700" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
            placeholder="••••••••"
            autoComplete="current-password"
          />

          {error && (
            <div className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700" role="alert">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy}
            className="mt-5 w-full rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? 'Signing in…' : 'Sign in'}
          </button>

          <p className="mt-4 text-center text-xs text-slate-400">
            New here?{' '}
            <Link to="/signup" className="font-medium text-slate-600 underline hover:text-slate-900">
              Create a consumer account
            </Link>
          </p>
        </form>

        <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="text-xs font-medium uppercase tracking-wide text-slate-400">
            Demo accounts
          </div>
          <div className="mt-2 grid gap-2">
            {DEMO_ACCOUNTS.map((a) => (
              <button
                key={a.role}
                type="button"
                onClick={() => quickFill(a)}
                className="flex items-center justify-between rounded-lg border border-slate-200 px-3 py-2 text-left text-sm transition hover:border-slate-400 hover:bg-slate-50"
              >
                <span>
                  <span className="font-medium capitalize">{a.role}</span>
                  <span className="ml-2 text-xs text-slate-400">{a.blurb}</span>
                </span>
                <span className="text-xs text-slate-400">{a.email}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
