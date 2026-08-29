import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../lib/api'

const FILE_KINDS = [
  { kind: 'loan_tape', label: 'Loan tape', source: 'OriginationCore', hint: 'Primary borrower + loan rows' },
  { kind: 'servicer_update', label: 'Servicer update', source: 'ServicerFeed', hint: 'Latest balances and statuses' },
  { kind: 'document_manifest', label: 'Document manifest', source: 'DocumentManifest', hint: 'Document status per loan' },
]

const STATUS_STYLES = {
  completed: 'bg-emerald-50 text-emerald-700',
  processing: 'bg-amber-50 text-amber-700',
  failed: 'bg-red-50 text-red-700',
}

function StatCard({ label, value, sub, tone = 'slate' }) {
  const tones = {
    slate: 'text-slate-900',
    emerald: 'text-emerald-600',
    red: 'text-red-600',
    violet: 'text-violet-600',
    amber: 'text-amber-600',
  }
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-2xl font-semibold ${tones[tone]}`}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-slate-400">{sub}</div>}
    </div>
  )
}

function UploadRow({ kind, label, source, hint, latestFile }) {
  const fileRef = useRef(null)
  const [file, setFile] = useState(null)
  const queryClient = useQueryClient()

  const upload = useMutation({
    mutationFn: async ({ force = false } = {}) => {
      const form = new FormData()
      form.append('file', file)
      form.append('file_kind', kind)
      return api.post(`/files${force ? '?force=true' : ''}`, form,
        { headers: { 'Content-Type': 'multipart/form-data' } })
    },
    onSuccess: () => {
      setFile(null)
      if (fileRef.current) fileRef.current.value = ''
      queryClient.invalidateQueries({ queryKey: ['files'] })
    },
  })

  // The upload POST returns status processing and that response is frozen.
  // The LIVE status comes from the files list (polled while anything is
  // in flight), matched to the file this mutation created.
  const uploadedId = upload.data?.data?.id
  const liveFile = uploadedId && latestFile?.id === uploadedId ? latestFile : undefined
  const statusText = liveFile?.status ?? (uploadedId ? 'processing' : undefined)
  const isDuplicate = upload.error?.code === 'DUPLICATE_FILE'

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:flex-row sm:items-center">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="font-medium">{label}</span>
          <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-500">{source}</span>
        </div>
        <div className="text-xs text-slate-400">{hint}</div>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept=".csv"
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        className="text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-slate-100 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-slate-700 hover:file:bg-slate-200"
      />

      <button
        onClick={() => upload.mutate()}
        disabled={!file || upload.isPending}
        className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {upload.isPending ? 'Uploading…' : 'Upload'}
      </button>

      {isDuplicate && (
        <button
          onClick={() => upload.mutate({ force: true })}
          disabled={upload.isPending}
          className="whitespace-nowrap rounded-lg border border-amber-400 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800 transition hover:bg-amber-100 disabled:opacity-40"
          title="Re-ingest the same bytes: every row is appended as a new version with fresh lineage. For a clean re-test use make db-reset instead."
        >
          Re-ingest as new version
        </button>
      )}

      {upload.isError && (
        <div className="max-w-48 text-xs text-red-600">
          {isDuplicate
            ? 'Already ingested — re-upload to version the data again'
            : upload.error.message}
        </div>
      )}
      {statusText && (
        <span className={`whitespace-nowrap rounded px-2 py-1 text-xs ${STATUS_STYLES[statusText] ?? 'bg-slate-100'}`}>
          {statusText === 'completed' && liveFile
            ? `${liveFile.row_count ?? 0} rows ingested`
            : statusText === 'processing' ? 'processing…' : statusText}
        </span>
      )}
    </div>
  )
}

const STAGE_META = {
  row_validation: { label: 'Row validation', detail: '15 row-scope rules over every record' },
  dataset_validation: { label: 'Dataset rules', detail: 'duplicates, fingerprints, repeat patterns' },
  cross_source_validation: { label: 'Cross-source conflicts', detail: 'OriginationCore vs ServicerFeed' },
  canonical_blend: { label: 'Canonical blend', detail: 'per-field survivorship across sources' },
  cluster_grouping: { label: 'Cluster grouping', detail: 'root-cause groups for the queue' },
}

function StageCard({ name, result }) {
  const meta = STAGE_META[name] ?? { label: name, detail: '' }
  const skipped = result && result.skipped !== undefined

  const rows = []
  if (skipped) {
    rows.push(['Reason', result.skipped === 'already_validated' ? 'already validated — nothing changed' : 'already detected'])
    if (result.existing_results != null) rows.push(['Existing results', result.existing_results.toLocaleString()])
    if (result.existing_exceptions != null) rows.push(['Existing exceptions', result.existing_exceptions.toLocaleString()])
  } else if (result) {
    const fmt = (v) => (typeof v === 'number' ? v.toLocaleString() : v)
    if (result.rules != null) rows.push(['Rules', fmt(result.rules)])
    if (result.records != null) rows.push(['Records', fmt(result.records)])
    if (result.results_written != null) rows.push(['Results written', fmt(result.results_written)])
    if (result.pass != null) rows.push(['Pass', fmt(result.pass)])
    if (result.fail != null) rows.push(['Fail', fmt(result.fail)])
    if (result.not_applicable != null) rows.push(['Not applicable', fmt(result.not_applicable)])
    if (result.loans_compared != null) rows.push(['Loans compared', fmt(result.loans_compared)])
    if (result.conflicts_found != null) rows.push(['Conflicts found', fmt(result.conflicts_found)])
    if (result.loans_processed != null) rows.push(['Loans processed', fmt(result.loans_processed)])
    if (result.fields_blended != null) rows.push(['Fields blended', fmt(result.fields_blended)])
    if (result.canonical_rows != null) rows.push(['Canonical rows', fmt(result.canonical_rows)])
    if (result.clusters_created != null) rows.push(['Clusters created', fmt(result.clusters_created)])
    if (result.exceptions_assigned != null) rows.push(['Exceptions clustered', fmt(result.exceptions_assigned)])
    if (result.exceptions_created != null)
      rows.push(['Exceptions created', fmt(result.exceptions_created), 'strong'])
    if (result.decisions_preserved)
      rows.push(['Reviewer decisions preserved', fmt(result.decisions_preserved), 'strong'])
    if (result.decided_skipped)
      rows.push(['Decided defects not reopened', fmt(result.decided_skipped)])
  }

  return (
    <div className={`rounded-xl border p-4 shadow-sm ${skipped ? 'border-slate-200 bg-slate-50' : 'border-emerald-200 bg-white'}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold">{meta.label}</span>
        <span className={`rounded px-2 py-0.5 text-xs ${skipped ? 'bg-slate-200 text-slate-600' : 'bg-emerald-50 text-emerald-700'}`}>
          {skipped ? 'skipped' : 'done'}
        </span>
      </div>
      <div className="text-xs text-slate-400">{meta.detail}</div>
      <dl className="mt-2 space-y-1 text-xs">
        {rows.map(([k, v, cls]) => (
          <div key={k} className="flex justify-between gap-3">
            <dt className="text-slate-400">{k}</dt>
            <dd className={cls === 'strong' ? 'font-semibold text-slate-900' : 'text-slate-700'}>{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

export default function OperatorDashboard() {
  const queryClient = useQueryClient()
  const [pipelineResult, setPipelineResult] = useState(null)

  const files = useQuery({
    queryKey: ['files'],
    queryFn: async () => (await api.get('/files')).data,
    refetchInterval: (query) => {
      const rows = query.state.data?.files ?? []
      return rows.some((f) => f.status === 'processing') ? 2000 : false
    },
  })

  const summary = useQuery({
    queryKey: ['summary'],
    queryFn: async () => (await api.get('/summary')).data,
    refetchInterval: 15000,
  })

  const pipeline = useMutation({
    mutationFn: async (force) =>
      api.post('/pipeline/run', { force: Boolean(force) }),
    onSuccess: (res) => {
      setPipelineResult({ ok: true, stages: res.data.stages })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
      queryClient.invalidateQueries({ queryKey: ['files'] })
    },
    onError: (err) => setPipelineResult({ ok: false, error: err }),
  })

  const fileRows = files.data?.files ?? []
  const recent = fileRows.slice(0, 6)
  const s = summary.data

  return (
    <div className="space-y-6">
      {/* Summary */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
          Dataset health
        </h2>
        {!s ? (
          <div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-400">
            Loading summary…
          </div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
              <StatCard label="Loans" value={s.loans.total} sub={`${s.loans.by_status.verified ?? 0} verified`} />
              <StatCard label="Open exceptions" value={s.exceptions.by_status.open ?? 0} tone="red"
                sub={`${s.exceptions.loans_affected} loans affected`} />
              <StatCard label="Source conflicts" value={s.exceptions.by_type.source_conflict ?? 0} tone="amber"
                sub="cross-source disagreements" />
              <StatCard label="Validation failures" value={s.exceptions.by_type.validation_failure ?? 0} tone="violet"
                sub="rule-based findings" />
              <StatCard label="Verified" value={s.verification.verified_loans} tone="emerald"
                sub={s.verification.avg_trust_score != null ? `avg trust ${Number(s.verification.avg_trust_score).toFixed(1)}` : 'no verifications yet'} />
            </div>
            <div className="mt-3 flex flex-wrap gap-2 text-xs text-slate-500">
              {Object.entries(s.sources).map(([src, n]) => (
                <span key={src} className="rounded-full bg-slate-100 px-3 py-1">
                  {src}: {n} loans
                </span>
              ))}
              <span className="rounded-full bg-slate-100 px-3 py-1">
                validation: {s.validation.pass ?? 0} pass · {s.validation.fail ?? 0} fail · {s.validation.not_applicable ?? 0} n/a
              </span>
            </div>
          </>
        )}
      </section>

      {/* Upload */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
          Import source files
        </h2>
        <div className="grid gap-3">
          {FILE_KINDS.map((k) => {
            const latest = (fileRows || []).find((f) => f.file_kind === k.kind)
            return <UploadRow key={k.kind} {...k} latestFile={latest} />
          })}
        </div>
      </section>

      {/* Pipeline */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
          Validation pipeline
        </h2>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-sm text-slate-500">
            Runs row validation, dataset rules, cross-source conflict detection,
            canonical blending and cluster grouping over every imported record.
            Stages with existing results are skipped unless you force a re-run.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              onClick={() => pipeline.mutate(false)}
              disabled={pipeline.isPending}
              className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {pipeline.isPending ? 'Running pipeline…' : 'Run pipeline'}
            </button>
            <button
              onClick={() => pipeline.mutate(true)}
              disabled={pipeline.isPending}
              className="rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:border-slate-400 disabled:cursor-not-allowed disabled:opacity-40"
              title="Clear open exceptions and re-run every stage. Reviewer decisions are preserved as history; decided defects are not reopened."
            >
              Force re-run
            </button>
          </div>

          {pipeline.isPending && (
            <div className="mt-3 animate-pulse text-sm text-slate-400">
              Validating 2,500+ records — this takes a few seconds…
            </div>
          )}

          {pipeline.isError && (
            <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
              {pipeline.error.code === 'PIPELINE_BLOCKED'
                ? `Pipeline blocked at "${pipeline.error.details?.stage ?? 'unknown stage'}": ${pipeline.error.message}`
                : pipeline.error.message}
            </div>
          )}

          {pipelineResult?.ok && (
            <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {Object.entries(pipelineResult.stages).map(([stage, res]) => (
                <StageCard key={stage} name={stage} result={res} />
              ))}
            </div>
          )}
        </div>
      </section>

      {/* Recent files */}
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
          Recent uploads
        </h2>
        <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          {recent.length === 0 ? (
            <div className="p-6 text-sm text-slate-400">No files uploaded yet.</div>
          ) : (
            <table className="w-full text-sm">
              <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-2 font-medium">File</th>
                  <th className="px-4 py-2 font-medium">Kind</th>
                  <th className="px-4 py-2 font-medium">Status</th>
                  <th className="px-4 py-2 font-medium">Rows</th>
                  <th className="px-4 py-2 font-medium">Parsed</th>
                  <th className="px-4 py-2 font-medium">Failed</th>
                  <th className="px-4 py-2 font-medium">Uploaded</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {recent.map((f) => (
                  <tr key={f.id} className="hover:bg-slate-50">
                    <td className="px-4 py-2 font-medium">{f.filename}</td>
                    <td className="px-4 py-2 text-slate-500">{f.file_kind}</td>
                    <td className="px-4 py-2">
                      <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLES[f.status] ?? 'bg-slate-100'}`}>
                        {f.status}
                      </span>
                    </td>
                    <td className="px-4 py-2 text-slate-500">{f.row_count ?? '—'}</td>
                    <td className="px-4 py-2 text-slate-500">{f.parsed_count ?? '—'}</td>
                    <td className="px-4 py-2 text-slate-500">{f.failed_count ?? '—'}</td>
                    <td className="px-4 py-2 text-slate-400">
                      {new Date(f.uploaded_at).toLocaleString()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>
    </div>
  )
}
