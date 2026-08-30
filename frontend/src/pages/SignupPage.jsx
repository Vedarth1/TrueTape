import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import api from '../lib/api'
import { useAuth } from '../lib/auth'

export default function SignupPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(e) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const res = await apiSignup(name, email, password)
      // auto-login: store the token the backend returned and route in
      localStorage.setItem('truetape_token', res.access_token)
      await login(email, password)
      navigate('/consumer', { replace: true })
    } catch (err) {
      setError(err.message ?? 'Signup failed')
    } finally {
      setBusy(false)
    }
  }

  async function apiSignup(name, email, password) {
    return (await api.post('/auth/signup', { name, email, password })).data
  }

  return (
    <div className="flex min-h-[80vh] items-center justify-center px-4">
      <div className="w-full max-w-md">
        <div className="mb-6 text-center">
          <Link to="/" className="text-2xl font-semibold tracking-tight">TrueTape</Link>
          <p className="mt-1 text-sm text-slate-500">
            Create a data-consumer account — browse verified records, inspect provenance, export data.
          </p>
        </div>

        <form onSubmit={submit} className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <label className="block text-sm font-medium text-slate-700" htmlFor="name">Full name</label>
          <input
            id="name" type="text" required value={name}
            onChange={(e) => setName(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
            placeholder="Jordan Blake"
            autoComplete="name"
          />

          <label className="mt-4 block text-sm font-medium text-slate-700" htmlFor="email">Work email</label>
          <input
            id="email" type="email" required value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
            placeholder="you@company.com"
            autoComplete="email"
          />

          <label className="mt-4 block text-sm font-medium text-slate-700" htmlFor="password">Password</label>
          <input
            id="password" type="password" required minLength={8} value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
            placeholder="at least 8 characters"
            autoComplete="new-password"
          />

          <div className="mt-3 rounded-lg bg-sky-50 px-3 py-2 text-xs text-sky-700">
            New accounts join as <span className="font-medium">data consumers</span> (read-only).
            Operator and reviewer access is provisioned by the challenge admins.
          </div>

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
            {busy ? 'Creating account…' : 'Create account'}
          </button>

          <p className="mt-4 text-center text-xs text-slate-400">
            Already have an account?{' '}
            <Link to="/login" className="font-medium text-slate-600 underline hover:text-slate-900">Sign in</Link>
          </p>
        </form>
      </div>
    </div>
  )
}
