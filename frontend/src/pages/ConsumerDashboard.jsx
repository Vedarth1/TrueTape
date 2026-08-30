import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '../lib/api'
import { fieldLabel, formatValue, humanize } from '../lib/format'

const SEV_STYLES = {
  CRITICAL: 'bg-red-100 text-red-800',
  HIGH: 'bg-orange-100 text-orange-800',
  MEDIUM: 'bg-amber-100 text-amber-800',
  LOW: 'bg-slate-100 text-slate-600',
}

const SOURCE_STYLES = {
  OriginationCore: 'bg-sky-50 text-sky-700',
  ServicerFeed: 'bg-violet-50 text-violet-700',
  DocumentManifest: 'bg-teal-50 text-teal-700',
  human_override: 'bg-emerald-50 text-emerald-700',
}

// ---- human-readable field presentation -------------------------------------
function TrustBar({ score }) {
  const pct = Math.max(0, Math.min(100, score))
  const color = pct >= 90 ? 'bg-emerald-500' : pct >= 75 ? 'bg-lime-500' : pct >= 50 ? 'bg-amber-500' : 'bg-red-500'
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-20 overflow-hidden rounded-full bg-slate-200">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-sm font-medium text-slate-700">{Number(score).toFixed(1)}</span>
    </div>
  )
}

function CopyChip({ value, label }) {
  const [copied, setCopied] = useState(false)
  const short = String(value ?? '').slice(0, 10)
  return (
    <button
      type="button"
      title={`${label ?? 'hash'}: ${value} — click to copy`}
      onClick={() => {
        navigator.clipboard?.writeText(String(value ?? ''))
        setCopied(true)
        setTimeout(() => setCopied(false), 1200)
      }}
      className="inline-flex items-center gap-1 rounded bg-slate-100 px-2 py-0.5 font-mono text-[11px] text-slate-600 transition hover:bg-slate-200"
    >
      {short}…{copied && <span className="font-sans text-emerald-600">copied</span>}
    </button>
  )
}

function downloadBlob(data, filename) {
  const url = URL.createObjectURL(data)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function ProvenanceTable({ provenance }) {
  const rows = Object.entries(provenance ?? {})
  if (rows.length === 0) return <div className="text-sm text-slate-400">No provenance recorded.</div>
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-slate-400 uppercase tracking-wide">
          <tr>
            <th className="py-1 pr-3 font-medium">Field</th>
            <th className="py-1 pr-3 font-medium">Value from</th>
            <th className="py-1 pr-3 font-medium">Source trust</th>
            <th className="py-1 font-medium">Pinned</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map(([field, p]) => {
            const meta = typeof p === 'object' ? p : { source_system: String(p) }
            return (
              <tr key={field}>
                <td className="py-1.5 pr-3 font-medium text-slate-800">{fieldLabel(field)}</td>
                <td className="py-1.5 pr-3">
                  <span className={`rounded px-1.5 py-0.5 ${SOURCE_STYLES[meta.source_system] ?? 'bg-slate-100 text-slate-600'}`}>
                    {meta.source_system === 'human_override' ? 'Reviewer' : meta.source_system ?? '—'}
                  </span>
                </td>
                <td className="py-1.5 pr-3 text-slate-600">{meta.trust_score ?? '—'}</td>
                <td className="py-1.5">{meta.pinned ? '✓' : ''}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

const EVENT_LABELS = {
  record_imported: 'Record imported',
  exception: 'Exception raised',
  audit: 'Audit event',
  verified: 'Loan verified',
}
const AUDIT_EVENT_LABELS = {
  file_uploaded: 'File uploaded', record_imported: 'Records imported',
  validation_executed: 'Validation run', exception_created: 'Exception created',
  ai_recommendation_generated: 'AI recommendation', reviewer_comment_added: 'Comment added',
  field_edited: 'Field edited', loan_approved: 'Loan approved', loan_rejected: 'Loan rejected',
  verified_record_created: 'Verified record created', verified_record_exported: 'Record exported',
  rule_created: 'Rule created', rule_deactivated: 'Rule deactivated',
  correction_requested: 'Correction requested', trust_config_updated: 'Trust config updated',
}

function Timeline({ loanId }) {
  const timeline = useQuery({
    queryKey: ['loan-timeline', loanId],
    queryFn: async () => (await api.get(`/loans/${loanId}/timeline`)).data,
    enabled: Boolean(loanId),
  })

  if (timeline.isLoading) return <div className="text-sm text-slate-400">Loading timeline…</div>
  if (timeline.isError) return <div className="text-sm text-red-600">{timeline.error.message}</div>

  const events = timeline.data?.events ?? []
  const DOT = {
    record_imported: 'bg-sky-500',
    exception: 'bg-red-500',
    audit: 'bg-slate-300',
    verified: 'bg-emerald-500',
  }
  return (
    <ol className="space-y-3">
      {events.length === 0 && <li className="text-sm text-slate-400">No events recorded.</li>}
      {events.map((e, i) => (
        <li key={i} className="flex gap-3">
          <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full ${DOT[e.type] ?? 'bg-slate-300'}`} />
          <div className="min-w-0">
            <div className="text-sm font-medium text-slate-800">
              {EVENT_LABELS[e.type] ?? humanize(e.type)}
              {e.type === 'record_imported' && (
                <span className="ml-2 text-xs font-normal text-slate-500">
                  {e.source_system} · version {e.version} · {e.fields_count} fields
                </span>
              )}
              {e.type === 'exception' && (
                <span className={`ml-2 rounded px-1.5 py-0.5 text-xs font-normal ${SEV_STYLES[e.severity] ?? 'bg-slate-100 text-slate-600'}`}>
                  {humanize(e.exception_type ?? '')} · {e.severity}
                </span>
              )}
              {e.type === 'audit' && (
                <span className="ml-2 text-xs font-normal text-slate-500">
                  {AUDIT_EVENT_LABELS[e.event_type] ?? humanize(e.event_type ?? '')} · by {e.actor_type}
                </span>
              )}
              {e.type === 'verified' && (
                <span className="ml-2 text-xs font-normal text-slate-500">
                  version {e.version} · trust {Number(e.trust_score).toFixed(1)}
                </span>
              )}
            </div>
            <div className="text-xs text-slate-400">{new Date(e.timestamp).toLocaleString()}</div>
          </div>
        </li>
      ))}
    </ol>
  )
}

function LoanDetailPage({ loan, onBack }) {
  const [showTimeline, setShowTimeline] = useState(true)
  const detail = useQuery({
    queryKey: ['loan-detail', loan.id],
    queryFn: async () => (await api.get(`/loans/${loan.id}`)).data,
  })

  if (detail.isLoading) return <div className="text-sm text-slate-400">Loading loan detail…</div>
  if (detail.isError) return <div className="text-sm text-red-600">{detail.error.message}</div>

  const d = detail.data
  const v = d.verification
  const b = v?.trust_score_breakdown ?? {}
  const canonical = d.canonical_data ?? {}

  return (
    <div className="space-y-5">
      <button
        onClick={onBack}
        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
      >
        ← Back to verified records
      </button>

      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="text-xl font-semibold">{d.loan_id}</div>
            <div className="text-sm text-slate-500">
              {d.borrower_name ?? d.borrower_id} · {d.borrower_id}
            </div>
          </div>
          <div className="flex items-center gap-3">
            {v && <TrustBar score={v.trust_score} />}
            <span className="rounded-full bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700">
              version {v?.version ?? '—'}
            </span>
            <button
              onClick={() => setShowTimeline((s) => !s)}
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-slate-400"
            >
              {showTimeline ? 'Hide timeline' : 'Show timeline'}
            </button>
          </div>
        </div>

        {showTimeline && (
          <div className="mt-4 rounded-lg bg-slate-50 p-4">
            <Timeline loanId={loan.id} />
          </div>
        )}

        {/* canonical data — human labels + formatted values */}
        <div className="mt-5 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-4">
          {Object.entries(canonical).map(([k, val]) => (
            <div key={k}>
              <div className="text-xs text-slate-400">{fieldLabel(k)}</div>
              <div
                className="truncate text-sm font-medium text-slate-800"
                title={String(val)}
              >
                {formatValue(k, val)}
              </div>
            </div>
          ))}
        </div>
      </div>

      {v && (
        <div className="grid gap-5 lg:grid-cols-2">
          {/* trust breakdown — curated, no [object Object] */}
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Why this trust score
            </div>
            <div className="space-y-3">
              {[
                ['Validation pass rate', b.validation_pass_rate, b.weights?.validation_pass_rate],
                ['Exception health', b.exception_health, b.weights?.exception_health],
                ['Source coverage', b.source_coverage, b.weights?.source_coverage],
                ['Source trust average', b.source_trust_average, b.weights?.source_trust_average],
              ].map(([label, value, weight]) => (
                <div key={label} className="flex items-center justify-between">
                  <span className="text-sm text-slate-600">{label}</span>
                  <span className="text-sm">
                    <span className="font-semibold text-slate-900">
                      {value != null ? `${Number(value).toFixed(1)}` : '—'}
                    </span>
                    {weight != null && <span className="ml-1 text-xs text-slate-400">× {Number(weight).toFixed(2)}</span>}
                  </span>
                </div>
              ))}
            </div>
            <div className="mt-4 border-t border-slate-100 pt-3 text-xs text-slate-500">
              {b.validation_counts && (
                <div>
                  Validation: {b.validation_counts.pass} pass · {b.validation_counts.fail} fail · {b.validation_counts.not_applicable} n/a
                </div>
              )}
              {b.exception_counts && (
                <div className="mt-0.5">
                  Exceptions: {b.exception_counts.total} total · {b.exception_counts.open} open · {b.exception_counts.resolved} resolved
                </div>
              )}
              {Array.isArray(b.sources) && (
                <div className="mt-0.5">Sources: {b.sources.join(', ')}</div>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-slate-400">
                record hash <CopyChip value={v.record_hash} label="record_hash" />
                {v.prev_record_hash && <>· prev <CopyChip value={v.prev_record_hash} label="prev_record_hash" /></>}
              </div>
            </div>
          </div>

          {/* provenance */}
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Field provenance — where each value came from
            </div>
            <ProvenanceTable provenance={v.field_provenance ?? d.field_provenance} />
          </div>
        </div>
      )}

      {/* source files */}
      {v?.source_files?.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Source files (lineage)
          </div>
          <div className="divide-y divide-slate-100">
            {v.source_files.map((sf, i) => (
              <div key={i} className="flex items-center justify-between gap-3 py-1.5">
                <span className="text-sm text-slate-700">
                  {sf.filename ?? sf.file_name ?? 'source'}
                </span>
                {(sf.sha256 ?? sf.file_hash) ? (
                  <CopyChip value={sf.sha256 ?? sf.file_hash} label={`${sf.filename ?? 'file'} sha256`} />
                ) : (
                  <span className="text-xs text-slate-400">no hash</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* exceptions */}
      {d.exceptions?.length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
            Exceptions ({d.open_exceptions} open)
          </div>
          <div className="flex flex-wrap gap-2">
            {d.exceptions.map((e, i) => (
              <span key={i} className={`rounded px-2 py-1 text-xs ${SEV_STYLES[e.severity] ?? 'bg-slate-100 text-slate-600'}`}>
                {humanize(e.type)} · {e.severity} · {e.status} ({e.count})
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function PaginationBar({ page, pages, total, perPage, onPage }) {
  if (pages <= 1) return null
  const start = (page - 1) * perPage + 1
  const end = Math.min(page * perPage, total)

  // compact page list with ellipsis: 1 … 4 5 6 … 15
  const nums = []
  const push = (n) => nums.push(n)
  const window = 2
  for (let n = 1; n <= pages; n++) {
    if (n === 1 || n === pages || Math.abs(n - page) <= window) push(n)
    else if (nums[nums.length - 1] !== '…') push('…')
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 px-4 py-3">
      <span className="text-xs text-slate-400">
        Showing {start.toLocaleString()}–{end.toLocaleString()} of {total.toLocaleString()}
      </span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPage(page - 1)}
          disabled={page <= 1}
          className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 transition hover:border-slate-400 disabled:opacity-40"
        >
          ← Prev
        </button>
        {nums.map((n, i) =>
          n === '…' ? (
            <span key={`e${i}`} className="px-1 text-xs text-slate-400">…</span>
          ) : (
            <button
              key={n}
              onClick={() => onPage(n)}
              className={`min-w-7 rounded-lg px-2 py-1 text-xs transition ${
                n === page
                  ? 'bg-slate-900 font-medium text-white'
                  : 'border border-slate-200 text-slate-600 hover:border-slate-400'
              }`}
            >
              {n}
            </button>
          ),
        )}
        <button
          onClick={() => onPage(page + 1)}
          disabled={page >= pages}
          className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 transition hover:border-slate-400 disabled:opacity-40"
        >
          Next →
        </button>
      </div>
    </div>
  )
}

export default function ConsumerDashboard() {
  const [search, setSearch] = useState('')
  const [minTrust, setMinTrust] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState(null)
  const PER_PAGE = 25

  const params = useMemo(() => {
    const p = { status: 'verified', per_page: PER_PAGE, page }
    if (search.trim()) p.search = search.trim()
    if (minTrust) p.min_trust = minTrust
    return p
  }, [search, minTrust, page])

  const loans = useQuery({
    queryKey: ['verified-loans', params],
    queryFn: async () => (await api.get('/loans', { params })).data,
  })

  const chain = useQuery({
    queryKey: ['chain'],
    queryFn: async () => (await api.get('/verify')).data,
    refetchInterval: 60000,
  })

  const summary = useQuery({
    queryKey: ['summary'],
    queryFn: async () => (await api.get('/summary')).data,
    refetchInterval: 30000,
  })

  async function exportFile(format) {
    try {
      const res = await api.get('/export', {
        params: { format, ...(minTrust ? { min_trust: minTrust } : {}) },
        responseType: 'blob',
      })
      downloadBlob(res.data, format === 'json' ? 'verified_records.json' : 'verified_records.csv')
    } catch (err) {
      alert(`Export failed: ${err.message ?? 'unknown error'}`)
    }
  }

  const rows = loans.data?.loans ?? []
  const v = summary.data?.verification
  const chainOk = chain.data?.ok

  // Detail mode replaces the list entirely, as a page.
  if (selected) {
    return (
      <LoanDetailPage loan={selected} onBack={() => setSelected(null)} />
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Verified records</h2>
        <div className="flex items-center gap-2">
          <span
            className={`rounded-full px-3 py-1 text-xs font-medium ${
              chainOk === true ? 'bg-emerald-50 text-emerald-700' : chainOk === false ? 'bg-red-50 text-red-700' : 'bg-slate-100 text-slate-500'
            }`}
          >
            {chainOk === true ? '✓ hash chains verified' : chainOk === false ? '✗ chain broken!' : 'checking chain…'}
          </span>
          <button
            onClick={() => exportFile('csv')}
            disabled={v?.verified_loans === 0}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-slate-400 disabled:opacity-40"
          >
            Export CSV
          </button>
          <button
            onClick={() => exportFile('json')}
            disabled={v?.verified_loans === 0}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:border-slate-400 disabled:opacity-40"
          >
            Export JSON
          </button>
        </div>
      </div>

      {v && (
        <div className="flex flex-wrap gap-3 text-xs">
          <span className="rounded-full bg-emerald-50 px-3 py-1 font-medium text-emerald-700">
            {v.verified_loans} verified loans
          </span>
          {v.avg_trust_score != null && (
            <span className="rounded-full bg-slate-100 px-3 py-1 text-slate-600">
              avg trust {Number(v.avg_trust_score).toFixed(1)}
            </span>
          )}
          {Object.entries(v.trust_distribution ?? {}).map(([bucket, n]) => (
            <span key={bucket} className="rounded-full bg-slate-100 px-3 py-1 text-slate-600">
              trust {bucket}: {n}
            </span>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-3">
        <input
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          placeholder="Search loan ID or borrower ID…"
          className="w-64 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
        />
        <select
          value={minTrust}
          onChange={(e) => { setMinTrust(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
        >
          <option value="">Any trust score</option>
          <option value="90">≥ 90</option>
          <option value="75">≥ 75</option>
          <option value="50">≥ 50</option>
        </select>
        <span className="self-center text-xs text-slate-400">
          {loans.data?.pagination?.total ?? 0} results
        </span>
      </div>

      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        {loans.isLoading ? (
          <div className="p-6 text-sm text-slate-400">Loading verified records…</div>
        ) : rows.length === 0 ? (
          <div className="p-8 text-center">
            <div className="text-sm font-medium text-slate-600">
              {v?.verified_loans === 0 ? 'No verified records yet' : 'No results for these filters'}
            </div>
            <div className="mt-1 text-xs text-slate-400">
              {v?.verified_loans === 0
                ? 'Reviewers resolve exceptions and verify loans from the exception queue — verified records appear here with trust scores and full provenance.'
                : 'Try widening your search or lowering the trust threshold.'}
            </div>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-2 font-medium">Loan</th>
                <th className="px-4 py-2 font-medium">Borrower</th>
                <th className="px-4 py-2 font-medium">Trust score</th>
                <th className="px-4 py-2 font-medium">Sources</th>
                <th className="px-4 py-2 font-medium">Verified</th>
                <th className="px-4 py-2 font-medium">Exceptions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((loan) => (
                <tr
                  key={loan.id}
                  onClick={() => setSelected(loan)}
                  className="cursor-pointer hover:bg-slate-50"
                >
                  <td className="px-4 py-2.5 font-medium text-slate-800">{loan.loan_id}</td>
                  <td className="px-4 py-2.5 text-slate-500">{loan.borrower_id}</td>
                  <td className="px-4 py-2.5">
                    {loan.trust_score != null ? <TrustBar score={loan.trust_score} /> : '—'}
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="flex flex-wrap gap-1">
                      {(loan.source_systems ?? []).map((s) => (
                        <span key={s} className={`rounded px-1.5 py-0.5 text-xs ${SOURCE_STYLES[s] ?? 'bg-slate-100 text-slate-600'}`}>
                          {s === 'OriginationCore' ? 'Core' : s === 'ServicerFeed' ? 'Servicer' : s === 'DocumentManifest' ? 'Docs' : s}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-slate-500">
                    {loan.verified_at ? new Date(loan.verified_at).toLocaleString() : '—'}
                  </td>
                  <td className="px-4 py-2.5">
                    {loan.open_exceptions > 0 ? (
                      <span className="rounded bg-red-50 px-2 py-0.5 text-xs text-red-700">{loan.open_exceptions} open</span>
                    ) : (
                      <span className="rounded bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700">clean</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {loans.data?.pagination && (
          <PaginationBar
            page={loans.data.pagination.page}
            pages={loans.data.pagination.pages}
            total={loans.data.pagination.total}
            perPage={loans.data.pagination.per_page}
            onPage={setPage}
          />
        )}
      </div>
    </div>
  )
}
