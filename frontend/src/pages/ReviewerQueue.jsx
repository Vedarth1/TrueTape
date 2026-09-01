import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../lib/api'
import { formatValue, humanize, fieldLabel, fmtMoney, isMoney, isEmptyish, isNoisyKey, FIELD_LABELS } from '../lib/format'

function fmtSuggested(field, value) {
  if (value == null || value === '') return null
  return isMoney(field) ? fmtMoney(value) : String(value)
}

const SEV_STYLES = {
  CRITICAL: 'bg-red-100 text-red-800',
  HIGH: 'bg-orange-100 text-orange-800',
  MEDIUM: 'bg-amber-100 text-amber-800',
  LOW: 'bg-slate-100 text-slate-600',
}

const EXCEPTION_TYPES = [
  'validation_failure', 'source_conflict', 'import_error',
]

// ---------------- AI panel ----------------
const AI_ACTIONS = [
  { key: 'analyze', path: (id) => `/exceptions/${id}/analyze`, label: 'Analyze failure', hint: 'explain why this rule fired' },
  { key: 'classify', path: (id) => `/exceptions/${id}/classify`, label: 'Classify severity', hint: 'independent severity check' },
  { key: 'note', path: (id) => `/exceptions/${id}/note`, label: 'Draft comment', hint: 'AI-drafted reviewer note' },
]

// Renders any evidence value recursively — scalars formatted, objects as
// indented sub-rows, nothing ever rendered as [object Object] or {N fields}.
function EvidenceValue({ value, depth = 0 }) {
  if (value == null || value === '') return <span className="text-slate-300">none</span>
  if (typeof value === 'boolean') return <span>{value ? 'yes' : 'no'}</span>
  if (Array.isArray(value)) {
    if (value.every((v) => typeof v !== 'object')) return <span>{value.join(', ')}</span>
    return (
      <div className="space-y-1">
        {value.map((v, i) => <EvidenceValue key={i} value={v} depth={depth + 1} />)}
      </div>
    )
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value).filter(([, v]) => !isEmptyish(v))
    if (entries.length === 0) return <span className="text-slate-300">none</span>
    return (
      <div className={`space-y-1 ${depth > 0 ? 'border-l border-slate-200 pl-2.5' : ''}`}>
        {entries.map(([k, v]) => (
          <div key={k} className="flex flex-wrap gap-x-1.5">
            <span className="text-slate-500">{humanize(k)}:</span>
            <EvidenceValue value={v} depth={depth + 1} />
          </div>
        ))}
      </div>
    )
  }
  return <span>{String(value)}</span>
}

// Curated evidence: the human story first (the rule's own message, the values
// that disagree), plumbing (rule internals, empty buckets) hidden. No
// mid-word truncation anywhere — long text wraps or scrolls.
function AiEvidence({ evidence, suggestedField }) {
  const entries = Object.entries(evidence ?? {}).filter(
    ([k, v]) => !isNoisyKey(k) && !isEmptyish(v))
  if (entries.length === 0) return null

  // surface the rendered rule message first when present (it lives inside
  // the exception detail blob)
  const detail = evidence.detail ?? {}
  const message = detail.message ?? evidence.message
  const ordered = entries
    .filter(([k]) => !(k === 'detail' && message))
    .sort(([a], [b]) => {
      const rank = (k) => (k === 'field' ? 0 : k === 'canonical value' ? 1 : k === 'source values' ? 2 : 3)
      return rank(a) - rank(b)
    })

  return (
    <div className="mt-2.5 rounded-lg border border-slate-200 bg-white/90 px-3 py-2.5 text-xs">
      <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        Evidence
      </div>
      {message && (
        <div className="mb-2 border-l-2 border-violet-300 pl-2.5 text-sm italic text-slate-700">
          “{message}”
        </div>
      )}
      <dl className="space-y-1.5">
        {ordered.map(([k, v]) => (
          <div key={k} className="grid grid-cols-[auto_1fr] gap-x-2.5">
            <dt className="whitespace-nowrap text-slate-400">{humanize(k)}</dt>
            <dd className="min-w-0 text-slate-700">
              {k === 'canonical value' && suggestedField
                ? formatValue(suggestedField, v)
                : k === 'field'
                  ? fieldLabel(String(v))
                  : <EvidenceValue value={v} />}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

const AI_TYPE_META = {
  explain_failure: { title: 'Failure explanation', accent: 'border-violet-300 bg-violet-50/60', pill: 'bg-violet-100 text-violet-800' },
  classify_severity: { title: 'Severity check', accent: 'border-sky-300 bg-sky-50/60', pill: 'bg-sky-100 text-sky-800' },
  reviewer_note: { title: 'Drafted comment', accent: 'border-emerald-300 bg-emerald-50/60', pill: 'bg-emerald-100 text-emerald-800' },
  batch_summary: { title: 'Cluster summary', accent: 'border-amber-300 bg-amber-50/60', pill: 'bg-amber-100 text-amber-800' },
}

function AiRecommendationCard({ rec }) {
  const meta = AI_TYPE_META[rec.action_type] ?? { title: rec.action_type, accent: 'border-slate-300 bg-slate-50', pill: 'bg-slate-100 text-slate-700' }
  const suggestedDisplay = fmtSuggested(rec.suggested_field, rec.suggested_value)
  return (
    <div className={`rounded-xl border p-4 transition-shadow hover:shadow-md ${meta.accent}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className={`rounded-md px-2 py-0.5 text-xs font-semibold ${meta.pill}`}>
          {meta.title}
        </span>
        <span className="flex flex-wrap items-center gap-1.5 text-xs text-slate-500">
          <span className="rounded bg-white/80 px-1.5 py-0.5 font-mono text-[10px] text-slate-500" title="Prompt version used to produce this recommendation">
            prompt: {rec.prompt_version ?? 'n/a'}
          </span>
          <span>{rec.model_name}</span>
          <span>· confidence{' '}
            <span className="font-semibold text-slate-800">{Number(rec.confidence).toFixed(2)}</span>
          </span>
        </span>
      </div>

      {rec.problem && (
        <p className="mt-2.5 text-sm text-slate-800">
          <span className="font-medium">Problem: </span>{rec.problem}
        </p>
      )}
      {rec.reasoning && <p className="mt-1.5 text-sm leading-relaxed text-slate-700">{rec.reasoning}</p>}

      <AiEvidence evidence={rec.evidence} suggestedField={rec.suggested_field} />

      {rec.suggested_field && suggestedDisplay && (
        <div className="mt-2.5 flex flex-wrap items-center gap-2 rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm">
          <span className="text-slate-500">Suggested fix:</span>
          <span className="font-medium text-slate-900">{fieldLabel(rec.suggested_field)}</span>
          <span className="font-mono font-medium text-slate-800">→ {suggestedDisplay}</span>
          {rec.suggested_source && (
            <span className={`rounded px-1.5 py-0.5 text-xs ${rec.suggested_source === 'human_override' ? 'bg-emerald-100 text-emerald-700' : 'bg-white/80 text-slate-500'}`}>
              from {rec.suggested_source === 'human_override' ? 'reviewer' : rec.suggested_source}
            </span>
          )}
        </div>
      )}

      {rec.suggested_severity && (
        <div className="mt-2 text-xs text-slate-600">
          AI severity call: <span className="font-semibold text-slate-800">{rec.suggested_severity}</span>
        </div>
      )}
      {rec.note_text && (
        <div className="mt-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
          “{rec.note_text}”
        </div>
      )}
      {rec.confidence_breakdown && Object.keys(rec.confidence_breakdown).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-400">
          {Object.entries(rec.confidence_breakdown).map(([k, v]) => (
            <span key={k}>{humanize(k)}: {typeof v === 'number' ? v.toFixed(2) : String(v)}</span>
          ))}
        </div>
      )}
      <div className="mt-1.5 text-[11px] text-slate-400">{new Date(rec.created_at).toLocaleString()}</div>
    </div>
  )
}

// Groups every recommendation by action type: newest card per type, with a
// "+N earlier runs" note — so three different actions read as three clearly
// different sections instead of identical stacked boxes.
function AiRecommendations({ recs }) {
  const byType = new Map()
  for (const rec of recs) {
    const list = byType.get(rec.action_type) ?? []
    list.push(rec)
    byType.set(rec.action_type, list)
  }
  if (byType.size === 0) return null
  const order = Object.keys(AI_TYPE_META)
  const sortedTypes = [...byType.entries()].sort(
    (a, b) => (order.indexOf(a[0]) + 1 || 99) - (order.indexOf(b[0]) + 1 || 99)
  )
  return (
    <div className="space-y-4">
      {sortedTypes.map(([type, list]) => {
        const sorted = [...list].sort((a, b) => new Date(b.created_at) - new Date(a.created_at))
        const meta = AI_TYPE_META[type] ?? { title: type }
        return (
          <div key={type}>
            <div className="mb-1.5 flex items-center gap-2">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">{meta.title}</span>
              {sorted.length > 1 && (
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-500">
                  +{sorted.length - 1} earlier run{sorted.length > 2 ? 's' : ''}
                </span>
              )}
            </div>
            <AiRecommendationCard rec={sorted[0]} />
          </div>
        )
      })}
    </div>
  )
}

// ---------------- resolve form ----------------
// The form is EXPLICITLY coupled to the AI suggestion: the reviewer can
// accept it in one click (pre-filling the correction), dismiss it (recorded
// as disagreeing), or ignore it and decide freely. Nothing is written until
// "Record decision" — the AI never mutates data on its own.
function ResolveForm({ exc, onDone }) {
  const queryClient = useQueryClient()
  const [action, setAction] = useState('accept')
  const [comment, setComment] = useState('')
  const [requestCorrection, setRequestCorrection] = useState(false)
  const [fieldName, setFieldName] = useState(exc.field_name ?? '')  // pre-blame the field the rule flagged
  const [afterValue, setAfterValue] = useState('')
  const [aiRef, setAiRef] = useState(null)          // rec id the decision responds to
  const [aiVerdict, setAiVerdict] = useState(null)  // 'accepted' | 'dismissed'

  // Latest AI recommendation that actually proposes a correction.
  const aiSuggestion = [...(exc.ai_recommendations ?? [])]
    .filter((r) => r.suggested_field && r.suggested_value != null && r.suggested_value !== '')
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0]

  const aiNote = [...(exc.ai_recommendations ?? [])]
    .filter((r) => r.action_type === 'reviewer_note' && r.note_text)
    .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0]

  function useAiFix() {
    setAction('edit')
    setFieldName(aiSuggestion.suggested_field)
    setAfterValue(String(aiSuggestion.suggested_value))
    setAiRef(aiSuggestion.id)
    setAiVerdict('accepted')
  }
  function dismissAiFix() {
    setAiRef(aiSuggestion?.id ?? null)
    setAiVerdict('dismissed')
  }

  const resolve = useMutation({
    mutationFn: async () => {
      const body = { action, comment: comment || undefined }
      if (action === 'reject' && requestCorrection) body.request_correction = true
      if (action === 'edit' || action === 'manual_resolution') {
        body.changes = [{ field: fieldName, after: afterValue }]
      }
      const recId = aiRef ?? exc.ai_recommendations?.[0]?.id
      if (recId) {
        body.ai_recommendation_id = recId
        if (aiVerdict === 'accepted') body.agreed_with_ai = true
        if (aiVerdict === 'dismissed') body.agreed_with_ai = false
      }
      return api.post(`/exceptions/${exc.id}/resolve`, body)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['exceptions'] })
      queryClient.invalidateQueries({ queryKey: ['clusters'] })
      queryClient.invalidateQueries({ queryKey: ['exception-detail', exc.id] })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
      onDone?.()
    },
  })

  const needsChanges = action === 'edit' || action === 'manual_resolution'

  return (
    <div className="overflow-hidden rounded-xl border-2 border-violet-300 bg-white shadow-md">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-violet-100 bg-violet-50 px-5 py-3">
        <span className="text-sm font-semibold text-violet-900">Resolve this exception</span>
        <span className="text-xs text-violet-500">record your decision — nothing is saved until you press Record decision</span>
      </div>
      <div className="p-5">

      {!aiSuggestion && (
        <div className="mb-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-500">
          {exc.rule_code?.startsWith('DUPLICATE_') || exc.rule_code?.startsWith('REPEATED_')
            ? 'No AI correction is suggested here — this is a dataset-level check (rows compared against other rows), so the decision is about the records, not a field value.'
            : exc.exception_type === 'source_conflict' && (exc.ai_recommendations ?? []).every((r) => r.action_type !== 'explain_failure')
              ? 'Run “Analyze failure” above and the AI will recommend the most-trusted source value for the conflicting field.'
              : (exc.ai_recommendations ?? []).some((r) => r.action_type === 'explain_failure')
                ? 'The AI analyzed this exception and found no valid alternative in any source — every source repeats the same missing or invalid value, so a manual correction is required. Use “Edit value” below to supply the correct one.'
                : 'Run “Analyze failure” above — the AI explains the failure and, if any source holds a valid alternative, proposes it here for one-click acceptance.'}
        </div>
      )}

      {aiSuggestion && (
        <div className={`mb-3 rounded-lg border p-3 text-sm transition ${
          aiVerdict === 'accepted' ? 'border-emerald-300 bg-emerald-50'
          : aiVerdict === 'dismissed' ? 'border-slate-200 bg-slate-50'
          : 'border-violet-300 bg-violet-50'}`}>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-slate-700">
              AI suggests{' '}
              <span className="font-medium">{fieldLabel(aiSuggestion.suggested_field)}</span>{' '}
              <span className="font-mono">→ {fmtSuggested(aiSuggestion.suggested_field, aiSuggestion.suggested_value)}</span>
              {aiSuggestion.suggested_source && (
                <span className="ml-1.5 text-xs text-slate-400">from {aiSuggestion.suggested_source}</span>
              )}
            </span>
            <span className="flex gap-2">
              <button
                type="button"
                onClick={useAiFix}
                className={`rounded-lg px-2.5 py-1 text-xs font-medium transition ${
                  aiVerdict === 'accepted'
                    ? 'bg-emerald-600 text-white'
                    : 'border border-emerald-400 bg-white text-emerald-700 hover:bg-emerald-50'}`}
              >
                {aiVerdict === 'accepted' ? '✓ fix pre-filled below' : 'Accept AI fix'}
              </button>
              <button
                type="button"
                onClick={dismissAiFix}
                className={`rounded-lg px-2.5 py-1 text-xs font-medium transition ${
                  aiVerdict === 'dismissed'
                    ? 'bg-slate-600 text-white'
                    : 'border border-slate-300 bg-white text-slate-600 hover:bg-slate-50'}`}
              >
                {aiVerdict === 'dismissed' ? 'dismissed' : 'Dismiss'}
              </button>
            </span>
          </div>
          {aiVerdict === 'accepted' && (
            <div className="mt-1.5 text-xs text-emerald-700">
              Decision will be recorded as agreeing with the AI recommendation.
            </div>
          )}
          {aiVerdict === 'dismissed' && (
            <div className="mt-1.5 text-xs text-slate-500">
              Decision will be recorded as disagreeing with the AI recommendation.
            </div>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {[
          ['accept', 'Accept', 'defect confirmed — keep as history'],
          ['edit', 'Edit value', 'correct the data (pins the field)'],
          ['manual_resolution', 'Manual resolution', 'decided outside the AI suggestion'],
          ['reject', 'Reject', 'not a real defect'],
        ].map(([key, label, hint]) => (
          <button
            key={key}
            type="button"
            onClick={() => setAction(key)}
            title={hint}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              action === key
                ? 'bg-slate-900 font-medium text-white'
                : 'border border-slate-300 text-slate-600 hover:border-slate-400'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {needsChanges && (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <select
            value={fieldName}
            onChange={(e) => setFieldName(e.target.value)}
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          >
            <option value="">Select field to correct…</option>
            {(exc.canonical_data && Object.keys(exc.canonical_data).length
              ? Object.keys(exc.canonical_data)
              : Object.keys(FIELD_LABELS)
            ).map((f) => (
              <option key={f} value={f}>{fieldLabel(f)}</option>
            ))}
          </select>
          <input
            value={afterValue}
            onChange={(e) => setAfterValue(e.target.value)}
            placeholder="Corrected value"
            className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <p className="text-xs text-slate-400 sm:col-span-2">
            The corrected value is validated like an import, pinned to the canonical
            record, and appended to the source lineage as a human_override revision.
          </p>
        </div>
      )}

      {action === 'reject' && (
        <label className="mt-3 flex items-center gap-2 text-sm text-slate-700">
          <input
            type="checkbox"
            checked={requestCorrection}
            onChange={(e) => setRequestCorrection(e.target.checked)}
            className="h-4 w-4 rounded border-slate-300"
          />
          Request correction from the operator (bounces the loan back to in_review)
        </label>
      )}

      {aiNote && (
        <div className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50/60 p-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-semibold text-emerald-700">AI-drafted comment</span>
            <button
              type="button"
              onClick={() => setComment(aiNote.note_text)}
              className="rounded-lg border border-emerald-400 bg-white px-2.5 py-1 text-xs font-medium text-emerald-700 transition hover:bg-emerald-50"
            >
              Use this comment
            </button>
          </div>
          <p className="mt-1 text-sm italic text-slate-600">“{aiNote.note_text}”</p>
        </div>
      )}

      <textarea
        value={comment}
        onChange={(e) => setComment(e.target.value)}
        placeholder="Decision comment (stored in the audit trail)…"
        rows={2}
        className="mt-3 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-violet-500"
      />

      {resolve.isError && (
        <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
          {resolve.error.code === 'ALREADY_RESOLVED'
            ? 'This exception was already decided.'
            : resolve.error.message}
        </div>
      )}
      {resolve.isSuccess && (
        <div className="mt-2 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
          Decision recorded — the audit chain captured it.
        </div>
      )}

      <button
        onClick={() => resolve.mutate()}
        disabled={resolve.isPending || (needsChanges && (!fieldName || !afterValue))}
        className="mt-3 rounded-lg bg-violet-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-violet-700 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {resolve.isPending ? 'Recording…' : 'Record decision'}
      </button>
      </div>
    </div>
  )
}

// ---------------- exception detail page ----------------
function ExceptionDetailPage({ excId, onBack }) {
  const queryClient = useQueryClient()
  const [recs, setRecs] = useState([])

  const detail = useQuery({
    queryKey: ['exception-detail', excId],
    queryFn: async () => (await api.get(`/exceptions/${excId}`)).data,
  })

  const runAi = useMutation({
    mutationFn: async ({ path }) => (await api.post(path)).data,
    onSuccess: (data) => {
      const rec = data.ai_recommendation ?? data
      if (rec?.id) setRecs((prev) => [rec, ...prev.filter((r) => r.id !== rec.id)])
      queryClient.invalidateQueries({ queryKey: ['exception-detail', excId] })
    },
  })

  const verifyLoan = useMutation({
    mutationFn: async () => api.post(`/loans/${detail.data.loan_id}/verify`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['exceptions'] })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
    },
  })

  if (detail.isLoading) return <div className="text-sm text-slate-400">Loading exception…</div>
  if (detail.isError) return <div className="text-sm text-red-600">{detail.error.message}</div>

  const d = detail.data
  const decided = d.status === 'resolved' || d.status === 'rejected'

  return (
    <div className="space-y-5">
      <button
        onClick={onBack}
        className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:border-slate-400 hover:bg-slate-50"
      >
        ← Back to queue
      </button>

      {/* header */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <span className={`rounded px-2 py-0.5 text-xs font-medium ${SEV_STYLES[d.severity] ?? 'bg-slate-100'}`}>
                {d.severity}
              </span>
              <span className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600">
                {d.exception_type?.replace(/_/g, ' ')}
              </span>
              {d.rule_code && <span className="font-mono text-xs text-slate-500">{d.rule_code}</span>}
              <span className={`rounded px-2 py-0.5 text-xs ${
                d.status === 'open' ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-600'}`}>
                {d.status}
              </span>
              {d.is_blocking && <span className="rounded bg-red-50 px-2 py-0.5 text-xs text-red-700">blocking</span>}
            </div>
            <h3 className="mt-2 text-lg font-semibold text-slate-900">{d.message}</h3>
            <div className="mt-1 text-sm text-slate-500">
              Loan <span className="font-medium text-slate-700">{d.loan_business_id}</span>
              {d.field_name && <> · field <span className="font-mono text-slate-600">{d.field_name}</span></>}
              {d.loan_status && <> · loan is <span className="font-medium">{d.loan_status}</span></>}
              {d.cluster_label && <> · cluster <span className="text-slate-600">{d.cluster_label}</span></>}
            </div>
          </div>
          {!decided && (
            <button
              onClick={() => verifyLoan.mutate()}
              disabled={verifyLoan.isPending}
              title="Verify the loan now (blocked if any blocking exception is still open)"
              className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 transition hover:bg-emerald-100 disabled:opacity-40"
            >
              {verifyLoan.isPending ? 'Verifying…' : 'Verify loan'}
            </button>
          )}
        </div>
        {verifyLoan.isError && (
          <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            {verifyLoan.error.code === 'NOT_ELIGIBLE'
              ? `Not eligible: ${verifyLoan.error.message}`
              : verifyLoan.error.message}
          </div>
        )}
        {verifyLoan.isSuccess && (
          <div className="mt-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
            Loan verified — see it under Verified records. Trust score recorded with a hash-chained verified record.
          </div>
        )}
      </div>

      {/* AI panel */}
      {!decided && (
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-xs font-semibold uppercase tracking-wide text-slate-400">
              AI review assistant
            </div>
            <div className="flex flex-wrap gap-2">
              {AI_ACTIONS.map((a) => (
                <button
                  key={a.key}
                  onClick={() => runAi.mutate({ path: a.path(excId) })}
                  disabled={runAi.isPending}
                  title={a.hint}
                  className="rounded-lg border border-violet-300 bg-violet-50 px-3 py-1.5 text-xs font-medium text-violet-700 transition hover:bg-violet-100 disabled:opacity-40"
                >
                  {a.label}
                </button>
              ))}
            </div>
          </div>
          {runAi.isPending && <div className="mt-3 animate-pulse text-sm text-slate-400">Thinking…</div>}
          {runAi.isError && (
            <div className="mt-3 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{runAi.error.message}</div>
          )}
          <div className="mt-3">
            <AiRecommendations recs={[...recs, ...(d.ai_recommendations ?? [])]} />
            {recs.length === 0 && (d.ai_recommendations ?? []).length === 0 && (
              <div className="text-sm text-slate-400">
                No AI recommendations yet — run one of the actions above.
              </div>
            )}
          </div>
        </div>
      )}

      {/* resolve */}
      {!decided ? (
        <ResolveForm exc={d} />
      ) : (
        <div className="rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500">
          This exception was <span className="font-medium">{d.status}</span> — the decision
          above is final and hash-chained. Related defects on newer record versions
          appear as their own open exceptions.
        </div>
      )}

      {/* what each source said + canonical value */}
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
            What each source said
          </div>
          <div className="space-y-3">
            {(d.source_records ?? []).map((r) => (
              <div key={`${r.source_system}-${r.version}`} className="rounded-lg border border-slate-100 p-2.5">
                <div className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-xs ${r.source_system === 'human_override' ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-600'}`}>
                    {r.source_system === 'human_override' ? 'Reviewer' : r.source_system}
                  </span>
                  <span className="text-xs text-slate-400">v{r.version} · {r.origin}</span>
                </div>
                {r.field_errors && Object.keys(r.field_errors).length > 0 && (
                  <div className="mt-1.5 space-y-1">
                    {Object.entries(r.field_errors).map(([f, e]) => {
                      const raw = e && typeof e === 'object' ? e.raw : e
                      const expected = e && typeof e === 'object' ? e.expected : null
                      return (
                        <div
                          key={f}
                          className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs"
                          title={expected ? `expected type: ${expected}` : undefined}
                        >
                          <span className="font-medium text-amber-800">value unparsed</span>
                          <span className="text-slate-500">{fieldLabel(f)}</span>
                          <span className="rounded bg-white px-1.5 py-0.5 font-mono text-[11px] text-slate-600">
                            {String(raw)}
                          </span>
                          {expected && <span className="text-slate-400">expected {expected}</span>}
                        </div>
                      )
                    })}
                  </div>
                )}
                <div className="mt-1.5 grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
                  {Object.entries(r.data ?? {}).slice(0, 8).map(([k, val]) => (
                    <div key={k} className="truncate" title={`${k}: ${val}`}>
                      <span className="text-slate-400">{fieldLabel(k)}: </span>
                      <span className="text-slate-700">{formatValue(k, val)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-5">
          {/* comments */}
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Comments
            </div>
            {(d.comments ?? []).length === 0 && (
              <div className="text-sm text-slate-400">No comments yet.</div>
            )}
            <div className="space-y-2">
              {(d.comments ?? []).map((c) => (
                <div key={c.id} className="rounded-lg bg-slate-50 p-2.5 text-sm text-slate-700">
                  {c.body}
                  <div className="mt-0.5 text-[11px] text-slate-400">
                    {new Date(c.created_at).toLocaleString()}
                    {c.ai_drafted && ' · AI-drafted'}
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* decision history */}
          <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Decision history
            </div>
            {(d.decision_history ?? []).length === 0 && (
              <div className="text-sm text-slate-400">No decisions recorded yet.</div>
            )}
            <div className="space-y-2">
              {(d.decision_history ?? []).map((h) => (
                <div key={h.id} className="rounded-lg border border-slate-100 p-2.5 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="font-medium capitalize text-slate-800">{h.action.replace(/_/g, ' ')}</span>
                    <span className="text-xs text-slate-400">{h.reviewer} · {new Date(h.decided_at).toLocaleString()}</span>
                  </div>
                  {h.request_correction && (
                    <span className="mt-1 inline-block rounded bg-amber-50 px-1.5 py-0.5 text-xs text-amber-700">
                      correction requested
                    </span>
                  )}
                  {h.comment && <div className="mt-1 text-slate-600">{h.comment}</div>}
                  {h.changes?.length > 0 && (
                    <div className="mt-1 text-xs text-slate-500">
                      {h.changes.map((c) => `${c.field}: ${c.after}`).join('; ')}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>

    </div>
  )
}

// ---------------- queue ----------------
export default function ReviewerQueue() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState(null)
  const [clusterFilter, setClusterFilter] = useState(null)
  const [search, setSearch] = useState('')
  const [severity, setSeverity] = useState('')
  const [excType, setExcType] = useState('')
  const [blockingOnly, setBlockingOnly] = useState(false)
  const [statusTab, setStatusTab] = useState('open')   // open | resolved | rejected | all
  const [page, setPage] = useState(1)
  const PER_PAGE = 25

  const params = useMemo(() => {
    const p = { page, per_page: PER_PAGE }
    if (statusTab !== 'all') p.status = statusTab
    if (clusterFilter) p.cluster_id = clusterFilter
    if (search.trim()) p.search = search.trim()
    if (severity) p.severity = severity
    if (excType) p.exception_type = excType
    if (blockingOnly) p.is_blocking = true
    return p
  }, [page, clusterFilter, search, severity, excType, blockingOnly, statusTab])

  // tab badges: global counts by status, one cheap call
  const statusCounts = useQuery({
    queryKey: ['exception-status-counts'],
    queryFn: async () => (await api.get('/exceptions/stats')).data,
    refetchInterval: 30000,
  })

  const clusters = useQuery({
    queryKey: ['clusters'],
    queryFn: async () => (await api.get('/exceptions/clusters')).data,
    refetchInterval: 30000,
  })

  const queue = useQuery({
    queryKey: ['exceptions', params],
    queryFn: async () => (await api.get('/exceptions', { params })).data,
  })

  const batchVerify = useMutation({
    mutationFn: async () => api.post('/verify-batch', {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['exceptions'] })
      queryClient.invalidateQueries({ queryKey: ['clusters'] })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
    },
  })

  // AI cluster summary (Required AI bullet: summarise a batch of exceptions)
  const [clusterSummary, setClusterSummary] = useState(null)
  const summarize = useMutation({
    mutationFn: async (cid) => (await api.post('/ai/batch-summary', { cluster_id: cid })).data,
    onSuccess: (rec) => setClusterSummary(rec),
  })

  const [selectedIds, setSelectedIds] = useState(() => new Set())
  const [allMatching, setAllMatching] = useState(false)
  const [gathering, setGathering] = useState(false)
  const [bulkAction, setBulkAction] = useState(null)
  const [bulkComment, setBulkComment] = useState('')
  const [bulkResult, setBulkResult] = useState(null)

  const filterKey = `${clusterFilter ?? ''}|${search.trim()}|${severity}|${excType}|${blockingOnly}`
  const [selectionKey, setSelectionKey] = useState(filterKey)
  if (filterKey !== selectionKey) {
    setSelectionKey(filterKey)
    setSelectedIds(new Set())
    setAllMatching(false)
    setBulkAction(null)
  }

  const bulkResolve = useMutation({
    mutationFn: async ({ action, ids, comment }) =>
      (await api.post('/exceptions/batch', {
        action,
        exception_ids: [...ids],
        comment: comment || undefined,
      })).data,
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ['exceptions'] })
      queryClient.invalidateQueries({ queryKey: ['clusters'] })
      queryClient.invalidateQueries({ queryKey: ['summary'] })
      setBulkResult(data)
      setSelectedIds(new Set())
      setAllMatching(false)
      setBulkAction(null)
      setBulkComment('')
    },
  })

  async function selectAllMatching() {
    setGathering(true)
    try {
      const ids = new Set()
      for (let p = 1; p <= 25; p += 1) {
        const { data } = await api.get('/exceptions', {
          params: { ...params, status: 'open', page: p, per_page: 200 },
        })
        for (const e of data.exceptions ?? []) ids.add(e.id)
        if (p >= (data.pagination?.pages ?? 1)) break
      }
      setSelectedIds(ids)
      setAllMatching(true)
    } finally {
      setGathering(false)
    }
  }

  if (selected) {
    return <ExceptionDetailPage excId={selected} onBack={() => setSelected(null)} />
  }

  const clusterRows = clusters.data?.clusters ?? []
  const rows = queue.data?.exceptions ?? []
  const pg = queue.data?.pagination
  const openPageIds = rows.filter((r) => r.status === 'open').map((r) => r.id)
  const allPageSelected = openPageIds.length > 0 && openPageIds.every((id) => selectedIds.has(id))
  const somePageSelected = openPageIds.some((id) => selectedIds.has(id))

  function toggleRow(id) {
    setAllMatching(false)
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  function togglePage() {
    setAllMatching(false)
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (allPageSelected) openPageIds.forEach((id) => next.delete(id))
      else openPageIds.forEach((id) => next.add(id))
      return next
    })
  }
  function clearSelection() {
    setSelectedIds(new Set())
    setAllMatching(false)
    setBulkAction(null)
    setBulkComment('')
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Exception queue</h2>
        <button
          onClick={() => batchVerify.mutate()}
          disabled={batchVerify.isPending}
          title="Verify every loan with no open blocking exceptions"
          className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 transition hover:bg-emerald-100 disabled:opacity-40"
        >
          {batchVerify.isPending ? 'Verifying…' : 'Verify all eligible loans'}
        </button>
      </div>
      {batchVerify.isSuccess && (() => {
        const r = batchVerify.data?.data ?? {}
        const upToDate = r.verified === 0 && r.eligible_seen === 0
        return (
          <div className={`rounded-lg px-3 py-2 text-sm ${
            r.errors?.length ? 'bg-amber-50 text-amber-800' : 'bg-emerald-50 text-emerald-700'}`}>
            {upToDate ? (
              <>
                Nothing new to verify — {r.already_verified} loans already have verified
                records{r.blocked ? `, and ${r.blocked} are still blocked by open blocking exceptions`
                  : ''}. Resolve blocking exceptions to make more loans eligible.
              </>
            ) : (
              <>
                Batch done — verified <span className="font-semibold">{r.verified}</span> loans.
                {r.already_verified ? ` ${r.already_verified} were already verified.` : ''}
                {r.blocked ? ` ${r.blocked} remain blocked by open exceptions.` : ''}
                {r.errors?.length ? ` ${r.errors.length} errors.` : ''}
              </>
            )}
          </div>
        )
      })()}
      {batchVerify.isError && (
        <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{batchVerify.error.message}</div>
      )}
      {bulkResult && (
        <div className="flex items-center justify-between gap-3 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
          <span>
            Batch {bulkResult.action === 'accept' ? 'accepted' : 'rejected'} — resolved{' '}
            <span className="font-semibold">{bulkResult.exceptions_resolved}</span>{' '}
            exception{bulkResult.exceptions_resolved === 1 ? '' : 's'} in one hash-chained batch.
          </span>
          <button onClick={() => setBulkResult(null)} className="text-xs text-emerald-600 underline">dismiss</button>
        </div>
      )}

      {/* status tabs — open work stays separate from what is already decided */}
      <div className="flex flex-wrap items-center gap-2">
        {[
          ['open', 'Open'],
          ['resolved', 'Resolved'],
          ['rejected', 'Rejected'],
          ['all', 'All'],
        ].map(([key, label]) => {
          const active = statusTab === key
          const n = statusCounts.data?.by_status?.[key]
          return (
            <button
              key={key}
              onClick={() => { setStatusTab(key); setPage(1) }}
              className={`rounded-lg px-3 py-1.5 text-sm transition ${
                active
                  ? 'bg-slate-900 font-medium text-white'
                  : 'border border-slate-300 text-slate-600 hover:border-slate-400'
              }`}
            >
              {label}
              {typeof n === 'number' && (
                <span className={`ml-1.5 rounded px-1.5 py-0.5 text-xs ${
                  active ? 'bg-white/20' : 'bg-slate-100 text-slate-500'}`}>
                  {n}
                </span>
              )}
            </button>
          )
        })}
      </div>

      {/* cluster cards */}
      <div>
        <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
          Root-cause clusters
        </div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
          <button
            onClick={() => { setClusterFilter(null); setPage(1) }}
            className={`rounded-xl border p-3 text-left shadow-sm transition ${
              clusterFilter === null ? 'border-slate-900 bg-slate-900 text-white' : 'border-slate-200 bg-white hover:border-slate-400'
            }`}
          >
            <div className="text-2xl font-semibold">
              {(clusterRows ?? []).reduce((a, c) => a + (c.open_count ?? 0), 0)}
            </div>
            <div className={`text-xs ${clusterFilter === null ? 'text-slate-300' : 'text-slate-500'}`}>all open</div>
          </button>
          {clusterRows.map((c) => (
            <div
              key={c.id}
              onClick={() => { setClusterFilter(clusterFilter === c.id ? null : c.id); setPage(1) }}
              className={`group cursor-pointer rounded-xl border p-3 text-left shadow-sm transition ${
                clusterFilter === c.id ? 'border-slate-900 bg-slate-900 text-white' : 'border-slate-200 bg-white hover:border-slate-400'
              }`}
            >
              <div className="flex items-center justify-between">
                <span className="text-2xl font-semibold">{c.open_count}</span>
                <span className="flex items-center gap-1">
                  <button
                    onClick={(ev) => { ev.stopPropagation(); summarize.mutate(c.id) }}
                    disabled={summarize.isPending}
                    title="AI summary of this whole cluster"
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium opacity-0 transition group-hover:opacity-100 ${
                      clusterFilter === c.id ? 'bg-white/20 text-white hover:bg-white/30' : 'bg-violet-50 text-violet-700 hover:bg-violet-100'
                    } disabled:opacity-30`}
                  >
                    ✦ summarize
                  </button>
                  {c.highest_severity && (
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${clusterFilter === c.id ? 'bg-white/20' : SEV_STYLES[c.highest_severity]}`}>
                      {c.highest_severity}
                    </span>
                  )}
                </span>
              </div>
              <div className={`truncate text-xs ${clusterFilter === c.id ? 'text-slate-300' : 'text-slate-500'}`} title={c.cluster_label}>
                {c.cluster_label}
              </div>
            </div>
          ))}
        </div>
      </div>

      {summarize.isPending && (
        <div className="animate-pulse rounded-xl border border-violet-200 bg-violet-50 p-4 text-sm text-violet-700">
          Summarizing the cluster…
        </div>
      )}
      {clusterSummary && (
        <div className="rounded-xl border border-violet-200 bg-violet-50/40 p-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-violet-700">
              AI cluster summary
            </span>
            <button
              onClick={() => setClusterSummary(null)}
              className="text-xs text-slate-400 hover:text-slate-600"
            >
              close
            </button>
          </div>
          <AiRecommendationCard rec={clusterSummary} />
        </div>
      )}

      {/* filters */}
      <div className="flex flex-wrap gap-3">
        <input
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1) }}
          placeholder="Search loan or borrower ID…"
          className="w-56 rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
        />
        <select
          value={severity}
          onChange={(e) => { setSeverity(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        >
          <option value="">Any severity</option>
          {Object.keys(SEV_STYLES).map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <select
          value={excType}
          onChange={(e) => { setExcType(e.target.value); setPage(1) }}
          className="rounded-lg border border-slate-300 px-3 py-2 text-sm"
        >
          <option value="">Any type</option>
          {EXCEPTION_TYPES.map((t) => <option key={t} value={t}>{t.replace(/_/g, ' ')}</option>)}
        </select>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input
            type="checkbox"
            checked={blockingOnly}
            onChange={(e) => { setBlockingOnly(e.target.checked); setPage(1) }}
            className="h-4 w-4 rounded border-slate-300"
          />
          Blocking only
        </label>
        <span className="self-center text-xs text-slate-400">{pg?.total ?? 0} exceptions</span>
      </div>

      {selectedIds.size > 0 && (
        <div className="rounded-xl border border-slate-700 bg-slate-900 p-3 text-white shadow-md">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-semibold">{selectedIds.size} selected</span>
              {allMatching ? (
                <span className="text-slate-300">· all open exceptions matching this filter</span>
              ) : pg && pg.pages > 1 ? (
                <button
                  onClick={selectAllMatching}
                  disabled={gathering}
                  className="rounded-md border border-slate-500 px-2 py-0.5 text-xs text-slate-200 transition hover:bg-slate-700 disabled:opacity-40"
                >
                  {gathering ? 'Selecting…' : 'Select all matching this filter'}
                </button>
              ) : null}
              <button
                onClick={clearSelection}
                className="text-xs text-slate-400 underline-offset-2 hover:text-white hover:underline"
              >
                clear
              </button>
            </div>
            {!bulkAction && (
              <div className="flex gap-2">
                <button
                  onClick={() => { setBulkResult(null); setBulkAction('accept') }}
                  className="rounded-lg bg-emerald-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-emerald-400"
                >
                  Accept {selectedIds.size}
                </button>
                <button
                  onClick={() => { setBulkResult(null); setBulkAction('reject') }}
                  className="rounded-lg bg-red-500 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-red-400"
                >
                  Reject {selectedIds.size}
                </button>
              </div>
            )}
          </div>

          {bulkAction && (
            <div className="mt-3 rounded-lg bg-slate-800 p-3">
              <div className="text-sm text-slate-100">
                {bulkAction === 'accept' ? (
                  <>Confirm each of these <span className="font-semibold">{selectedIds.size}</span> exceptions as a real defect and close it — no data is corrected (use Edit on a single exception for that).</>
                ) : (
                  <>Mark each of these <span className="font-semibold">{selectedIds.size}</span> exceptions as not a real defect and close it.</>
                )}{' '}
                Each gets its own hash-chained decision under one batch. This can’t be undone.
              </div>
              <input
                value={bulkComment}
                onChange={(e) => setBulkComment(e.target.value)}
                placeholder="Reason / comment (recorded on every decision)…"
                className="mt-2 w-full rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-white placeholder-slate-500 outline-none focus:border-slate-400"
              />
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <button
                  onClick={() => bulkResolve.mutate({ action: bulkAction, ids: selectedIds, comment: bulkComment })}
                  disabled={bulkResolve.isPending}
                  className={`rounded-lg px-3 py-1.5 text-sm font-medium text-white transition disabled:opacity-40 ${
                    bulkAction === 'accept' ? 'bg-emerald-500 hover:bg-emerald-400' : 'bg-red-500 hover:bg-red-400'
                  }`}
                >
                  {bulkResolve.isPending ? 'Working…' : `Confirm ${bulkAction} ${selectedIds.size}`}
                </button>
                <button
                  onClick={() => setBulkAction(null)}
                  disabled={bulkResolve.isPending}
                  className="rounded-lg border border-slate-500 px-3 py-1.5 text-sm text-slate-200 transition hover:bg-slate-700 disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}

          {bulkResolve.isError && (
            <div className="mt-2 rounded-lg bg-red-500/20 px-3 py-2 text-sm text-red-200">
              {bulkResolve.error.message}
            </div>
          )}
        </div>
      )}

      {/* table */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        {queue.isLoading ? (
          <div className="p-6 text-sm text-slate-400">Loading queue…</div>
        ) : rows.length === 0 ? (
          <div className="p-8 text-center">
            <div className="text-sm font-medium text-slate-600">No exceptions match these filters</div>
            <div className="mt-1 text-xs text-slate-400">Clear a filter or pick a different cluster.</div>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="w-10 px-4 py-2">
                  <input
                    type="checkbox"
                    checked={allPageSelected}
                    ref={(el) => { if (el) el.indeterminate = !allPageSelected && somePageSelected }}
                    onChange={togglePage}
                    disabled={openPageIds.length === 0}
                    title="Select all open exceptions on this page"
                    className="h-4 w-4 rounded border-slate-300 align-middle"
                  />
                </th>
                <th className="px-4 py-2 font-medium">Severity</th>
                <th className="px-4 py-2 font-medium">Exception</th>
                <th className="px-4 py-2 font-medium">Loan</th>
                <th className="px-4 py-2 font-medium">Message</th>
                <th className="px-4 py-2 font-medium">Status</th>
                <th className="px-4 py-2 font-medium">Raised</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((e) => (
                <tr
                  key={e.id}
                  onClick={() => setSelected(e.id)}
                  className={`cursor-pointer hover:bg-slate-50 ${selectedIds.has(e.id) ? 'bg-violet-50/70' : ''}`}
                >
                  <td className="px-4 py-2.5" onClick={(ev) => ev.stopPropagation()}>
                    {e.status === 'open' && (
                      <input
                        type="checkbox"
                        checked={selectedIds.has(e.id)}
                        onChange={() => toggleRow(e.id)}
                        title="Select this exception"
                        className="h-4 w-4 rounded border-slate-300 align-middle"
                      />
                    )}
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={`rounded px-2 py-0.5 text-xs font-medium ${SEV_STYLES[e.severity] ?? 'bg-slate-100'}`}>
                      {e.severity}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    <div className="font-medium text-slate-800">{e.exception_type?.replace(/_/g, ' ')}</div>
                    {e.rule_code && <div className="font-mono text-xs text-slate-400">{e.rule_code}</div>}
                  </td>
                  <td className="px-4 py-2.5 font-medium text-slate-700">{e.loan_business_id ?? '—'}</td>
                  <td className="max-w-72 px-4 py-2.5">
                    <div className="truncate text-slate-600" title={e.message}>{e.message}</div>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={`rounded px-2 py-0.5 text-xs ${
                      e.status === 'open' ? 'bg-amber-50 text-amber-700' : 'bg-slate-100 text-slate-500'}`}>
                      {e.status}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-slate-400">
                    {new Date(e.created_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {pg && pg.pages > 1 && (
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-slate-100 px-4 py-3">
            <span className="text-xs text-slate-400">
              Showing {(pg.page - 1) * pg.per_page + 1}–{Math.min(pg.page * pg.per_page, pg.total)} of {pg.total.toLocaleString()}
            </span>
            <div className="flex gap-1">
              <button
                onClick={() => setPage(page - 1)}
                disabled={page <= 1}
                className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-slate-400 disabled:opacity-40"
              >← Prev</button>
              <button
                onClick={() => setPage(page + 1)}
                disabled={page >= pg.pages}
                className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs text-slate-600 hover:border-slate-400 disabled:opacity-40"
              >Next →</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
