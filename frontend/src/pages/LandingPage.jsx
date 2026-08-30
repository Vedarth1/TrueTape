import { Link } from 'react-router-dom'
import { useAuth } from '../lib/auth'

const FEATURES = [
  {
    title: 'Schema-aware validation',
    body: '18 seeded rules across row, dataset and cross-source scopes catch malformed dates, negative principals, duplicate fingerprints and stale servicer data — 40k+ checks per run.',
    icon: '✓',
    tone: 'bg-emerald-50 text-emerald-600',
  },
  {
    title: 'Root-cause clustering',
    body: 'Exceptions group by their signal, so one systematic defect reads as one card — not four hundred rows. Cluster cards open straight into a filtered queue.',
    icon: '⧉',
    tone: 'bg-amber-50 text-amber-600',
  },
  {
    title: 'AI review copilot',
    body: 'Explains why a rule fired, proposes corrections from the most-trusted source, drafts reviewer comments, and never pretends confidence it does not have.',
    icon: '✦',
    tone: 'bg-violet-50 text-violet-600',
  },
  {
    title: 'Hash-chained audit trail',
    body: 'Every decision, edit and import appends to a tamper-evident chain. A built-in verifier re-walks the hashes — tampering is detected, not assumed away.',
    icon: '⛓',
    tone: 'bg-sky-50 text-sky-600',
  },
  {
    title: 'Trust-scored output',
    body: 'Verified records carry a 0–100 trust score built from validation pass rate, exception health, source coverage and source trust — with the full breakdown shown.',
    icon: '★',
    tone: 'bg-lime-50 text-lime-600',
  },
  {
    title: 'Consumer-ready API',
    body: 'A read-only verified-records API with search, filtering, per-field provenance and CSV/JSON export — the data consumer never touches the review pipeline.',
    icon: '⇣',
    tone: 'bg-rose-50 text-rose-600',
  },
]

const STEPS = [
  { n: '01', title: 'Import', body: 'Drop the three source files — loan tape, servicer update, document manifest. Parsing and normalisation run immediately, quarantining rows that cannot even be read.' },
  { n: '02', title: 'Validate & reconcile', body: 'Row rules, dataset rules and cross-source conflict detection run as one atomic stage. Canonical records blend every source by field-level trust.' },
  { n: '03', title: 'Review with AI', body: 'Reviewers work a cluster-first queue: the AI explains failures, proposes fixes and drafts comments — every decision pinned, versioned and hash-chained.' },
  { n: '04', title: 'Verify & consume', body: 'Clean loans become hash-chained verified records with trust scores. Data consumers browse, inspect provenance and export — read-only, always.' },
]

const ROLE_CARDS = [
  { role: 'Operator', blurb: 'Uploads source files and runs the validation pipeline.', cta: 'Sign in as operator' },
  { role: 'Reviewer', blurb: 'Resolves exceptions with AI assistance and verifies loans.', cta: 'Sign in as reviewer' },
  { role: 'Consumer', blurb: 'Browses verified records, provenance and exports. Self-signup.', cta: 'Create a consumer account' },
]

export default function LandingPage() {
  const { user } = useAuth()

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      {/* nav */}
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
          <span className="text-lg font-semibold tracking-tight">TrueTape</span>
          <div className="flex items-center gap-2">
            {user ? (
              <Link
                to={user.role === 'reviewer' ? '/reviewer' : user.role === 'operator' ? '/operator' : '/consumer'}
                className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700"
              >
                Open workspace
              </Link>
            ) : (
              <>
                <Link to="/login" className="rounded-lg px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100">
                  Sign in
                </Link>
                <Link to="/signup" className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700">
                  Create account
                </Link>
              </>
            )}
          </div>
        </div>
      </header>

      {/* hero */}
      <section className="mx-auto max-w-6xl px-6 pb-16 pt-14">
        <div className="grid items-center gap-10 lg:grid-cols-2">
          <div>
            <span className="inline-block rounded-full bg-violet-50 px-3 py-1 text-xs font-medium text-violet-700">
              Intain Campus FinTech Challenge 2026 · Full Stack Track
            </span>
            <h1 className="mt-4 text-4xl font-semibold leading-tight tracking-tight sm:text-5xl">
              Loan data you can actually <span className="text-violet-600">trust</span>.
            </h1>
            <p className="mt-4 max-w-lg text-lg text-slate-600">
              TrueTape ingests messy multi-source loan tapes, quarantines the unreadable,
              validates every field, reconciles sources by trust, and turns what survives
              into hash-chained verified records — with an AI copilot at the reviewer's side.
            </p>
            <div className="mt-6 flex flex-wrap gap-3">
              <Link
                to={user ? (user.role === 'reviewer' ? '/reviewer' : user.role === 'operator' ? '/operator' : '/consumer') : '/login'}
                className="rounded-lg bg-slate-900 px-5 py-2.5 text-sm font-medium text-white hover:bg-slate-700"
              >
                {user ? 'Open your workspace' : 'Sign in to the workspace'}
              </Link>
              {!user && (
                <Link
                  to="/signup"
                  className="rounded-lg border border-slate-300 px-5 py-2.5 text-sm font-medium text-slate-700 hover:border-slate-400"
                >
                  Browse verified records →
                </Link>
              )}
            </div>
            <div className="mt-6 flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-400">
              <span>1,203-loan demo dataset</span>
              <span>230 findings · 20 root-cause clusters</span>
              <span>21 canonical fields, fully provenanced</span>
            </div>
          </div>

          {/* mock trust card */}
          <div className="relative mx-auto w-full max-w-md">
            <div className="absolute -inset-4 rounded-3xl bg-gradient-to-br from-violet-100 via-transparent to-sky-100" />
            <div className="relative space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-xl">
              <div className="flex items-center justify-between">
                <div>
                  <div className="text-sm font-semibold">LN-000003 · Meera Menon</div>
                  <div className="text-xs text-slate-400">3 sources reconciled</div>
                </div>
                <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700">verified</span>
              </div>
              <div>
                <div className="mb-1 flex justify-between text-xs text-slate-400">
                  <span>Trust score</span><span className="font-semibold text-slate-700">96.1</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-slate-100">
                  <div className="h-full w-[96%] rounded-full bg-emerald-500" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="rounded-lg bg-slate-50 p-2">
                  <div className="text-slate-400">Balance</div>
                  <div className="font-medium">$415,944.12</div>
                </div>
                <div className="rounded-lg bg-slate-50 p-2">
                  <div className="text-slate-400">Rate</div>
                  <div className="font-medium">7.85%</div>
                </div>
              </div>
              <div className="flex flex-wrap gap-1.5 text-[11px]">
                <span className="rounded bg-sky-50 px-1.5 py-0.5 text-sky-700">OriginationCore</span>
                <span className="rounded bg-violet-50 px-1.5 py-0.5 text-violet-700">ServicerFeed</span>
                <span className="rounded bg-teal-50 px-1.5 py-0.5 text-teal-700">DocumentManifest</span>
              </div>
              <div className="flex items-center justify-between border-t border-slate-100 pt-2 text-[11px] text-slate-400">
                <span>record hash</span>
                <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-slate-600">8f5dff80c7d6…</span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* features */}
      <section className="border-y border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-6 py-14">
          <h2 className="text-2xl font-semibold tracking-tight">Everything the brief asked for, wired end to end</h2>
          <p className="mt-2 max-w-2xl text-slate-600">
            Not a slide deck — a working pipeline. Every number on the dashboards comes from
            the same database the APIs serve and the audit chain attests to.
          </p>
          <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((f) => (
              <div key={f.title} className="rounded-xl border border-slate-200 p-5 transition-shadow hover:shadow-md">
                <span className={`inline-flex h-9 w-9 items-center justify-center rounded-lg text-lg font-semibold ${f.tone}`}>
                  {f.icon}
                </span>
                <h3 className="mt-3 font-semibold">{f.title}</h3>
                <p className="mt-1 text-sm leading-relaxed text-slate-600">{f.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* how it works */}
      <section className="mx-auto max-w-6xl px-6 py-14">
        <h2 className="text-2xl font-semibold tracking-tight">How a messy CSV becomes a verified record</h2>
        <div className="mt-8 grid gap-4 md:grid-cols-4">
          {STEPS.map((s) => (
            <div key={s.n} className="rounded-xl border border-slate-200 bg-white p-5">
              <div className="text-xs font-semibold text-violet-600">{s.n}</div>
              <h3 className="mt-1 font-semibold">{s.title}</h3>
              <p className="mt-1 text-sm leading-relaxed text-slate-600">{s.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* roles */}
      <section className="border-t border-slate-200 bg-white">
        <div className="mx-auto max-w-6xl px-6 py-14">
          <h2 className="text-2xl font-semibold tracking-tight">Three roles, one pipeline</h2>
          <div className="mt-8 grid gap-4 md:grid-cols-3">
            {ROLE_CARDS.map((r) => (
              <div key={r.role} className="flex flex-col rounded-xl border border-slate-200 p-5">
                <h3 className="font-semibold">{r.role}</h3>
                <p className="mt-1 flex-1 text-sm text-slate-600">{r.blurb}</p>
                <Link
                  to={r.role === 'Consumer' ? '/signup' : '/login'}
                  className="mt-4 text-sm font-medium text-violet-600 hover:text-violet-800"
                >
                  {r.cta} →
                </Link>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* footer */}
      <footer className="border-t border-slate-200 bg-slate-900 py-8 text-center text-sm text-slate-400">
        <span className="font-semibold text-white">TrueTape</span> — built for the Intain
        Campus FinTech Challenge 2026. Deterministic AI with honest confidence, immutable
        audit chains, and loan data that earns its trust score.
      </footer>
    </div>
  )
}
