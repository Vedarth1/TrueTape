import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../lib/api'


// Pretty-print a DSL condition tree as an English sentence — the "you asked
// for X, here is the tree we compiled" side-by-side the schema promised.
function prettyCondition(node) {
  if (!node) return ''
  const t = node.type
  if (t === 'comparison') {
    const op = { '==': 'equals', '!=': 'does not equal', '>': 'is above', '>=': 'is at least', '<': 'is below', '<=': 'is at most' }[node.operator] ?? node.operator
    const left = node.left?.name ?? '?'
    const right = node.right?.type === 'literal' ? String(node.right.value) : '?'
    return `${left} ${op} ${right}`
  }
  if (t === 'func' && node.name === 'not_null') {
    const f = node.args?.[0]?.name ?? '?'
    return `${f} is present`
  }
  if (t === 'func' && node.name === 'in_set') {
    const f = node.args?.[0]?.name ?? '?'
    const set = node.args?.[1]?.value ?? []
    return `${f} is one of ${set.join(', ')}`
  }
  if (t === 'not') return `NOT (${prettyCondition(node.operand)})`
  if (t === 'and') return node.operands.map(prettyCondition).join(' AND ')
  if (t === 'or') return node.operands.map(prettyCondition).join(' OR ')
  return JSON.stringify(node).slice(0, 80)
}

function PreviewPanel({ preview }) {
  if (!preview) return null
  const t = preview.tally
  return (
    <div className="rounded-xl border border-sky-200 bg-sky-50/50 p-4">
      <div className="text-xs font-semibold uppercase tracking-wide text-sky-700">
        Dry run — {preview.records_evaluated.toLocaleString()} records, nothing saved
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs">
        <span className="rounded-full bg-emerald-100 px-2.5 py-1 font-medium text-emerald-800">
          {t.pass.toLocaleString()} would pass
        </span>
        <span className="rounded-full bg-red-100 px-2.5 py-1 font-medium text-red-800">
          {t.fail.toLocaleString()} would fail
        </span>
        <span className="rounded-full bg-slate-100 px-2.5 py-1 text-slate-600">
          {t.not_applicable.toLocaleString()} not applicable
        </span>
      </div>
      {preview.sample_failures?.length > 0 && (
        <div className="mt-3">
          <div className="text-[11px] uppercase tracking-wide text-slate-400">Sample failures</div>
          <div className="mt-1 space-y-1">
            {preview.sample_failures.slice(0, 5).map((s, i) => (
              <div key={i} className="rounded bg-white px-2 py-1 text-xs text-slate-600">
                <span className="font-medium">{s.source_system} v{s.version}</span>
                {' — '}
                {Object.entries(s.values).map(([k, v]) => `${k}=${v}`).join(', ') || '(no referenced values)'}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default function RulesPage() {
  const queryClient = useQueryClient()
  const [nlText, setNlText] = useState('')
  const [draft, setDraft] = useState(null)
  const [preview, setPreview] = useState(null)
  const [ruleCode, setRuleCode] = useState('')
  const [published, setPublished] = useState(null)

  const generate = useMutation({
    mutationFn: async () => (await api.post('/ai/generate-rule', { nl_text: nlText })).data,
    onSuccess: (res) => {
      setDraft(res.draft)
      setPreview(null)
      setPublished(null)
      setRuleCode(res.draft.rule_code)
    },
  })

  const runPreview = useMutation({
    mutationFn: async () =>
      (await api.post('/rules/preview', { scope: 'row', condition: draft.condition })).data,
    onSuccess: (res) => setPreview(res.preview),
  })

  const publish = useMutation({
    mutationFn: async () =>
      (await api.post('/rules', {
        rule_code: ruleCode || draft.rule_code,
        scope: 'row',
        severity: draft.severity,
        condition: draft.condition,
        message_template: draft.message_template,
        natural_language_source: draft.natural_language_source,
        explanation: draft.explanation,
      })).data,
    onSuccess: (res) => {
      setPublished(res)
      setDraft(null)
      setPreview(null)
      setNlText('')
      queryClient.invalidateQueries({ queryKey: ['rules-list'] })
    },
  })

  const rules = useQuery({
    queryKey: ['rules-list'],
    queryFn: async () => (await api.get('/rules')).data,
  })

  const toggle = useMutation({
    mutationFn: async ({ id, is_active }) =>
      api.patch(`/rules/${id}`, { is_active }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['rules-list'] }),
  })

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-lg font-semibold">Rule studio</h2>
        <p className="text-sm text-slate-500">
          Describe a validation rule in plain English — the compiler turns it into the same
          DSL the engine runs, you preview who it would flag, then publish it. Published
          rules land in the audit chain with your name on them.
        </p>
      </div>

      {/* NL input */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <textarea
          value={nlText}
          onChange={(e) => setNlText(e.target.value)}
          rows={2}
          maxLength={500}
          placeholder='e.g. "flag loans where interest rate is above 36" or "flag loans missing days past due"'
          className="w-full rounded-lg border border-slate-300 px-3 py-2 text-sm outline-none focus:border-slate-900"
        />
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs text-slate-400">{nlText.length}/500 · row-scope rules</span>
          <button
            onClick={() => generate.mutate()}
            disabled={!nlText.trim() || generate.isPending}
            className="rounded-lg bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {generate.isPending ? 'Compiling…' : 'Compile rule'}
          </button>
        </div>
        {generate.isError && (
          <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            {generate.error.message}
          </div>
        )}
      </div>

      {/* draft */}
      {draft && (
        <div className="rounded-xl border border-violet-300 bg-violet-50/40 p-5 shadow-sm">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="rounded bg-violet-100 px-2 py-0.5 text-xs font-semibold text-violet-800">
              Compiled draft
            </span>
            <span className="text-xs text-slate-500">
              confidence <span className="font-semibold text-slate-800">{Number(draft.confidence).toFixed(2)}</span>
              {' · '}suggested severity{' '}
              <span className="font-semibold text-slate-800">{draft.severity}</span>
            </span>
          </div>
          <div className="mt-3 rounded-lg border border-slate-200 bg-white p-3">
            <div className="text-xs text-slate-400">You asked for</div>
            <div className="text-sm italic text-slate-700">“{draft.natural_language_source}”</div>
            <div className="mt-2 text-xs text-slate-400">The engine will pass records where</div>
            <div className="font-mono text-sm text-slate-900">{prettyCondition(draft.condition)}</div>
          </div>
          {draft.parse_notes?.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-400">
              {draft.parse_notes.map((n, i) => <span key={i}>{n}</span>)}
            </div>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              onClick={() => runPreview.mutate()}
              disabled={runPreview.isPending}
              className="rounded-lg border border-sky-400 bg-white px-3 py-1.5 text-xs font-medium text-sky-700 hover:bg-sky-50 disabled:opacity-40"
            >
              {runPreview.isPending ? 'Dry-running…' : 'Preview against dataset'}
            </button>
            <label className="flex items-center gap-1.5 text-xs text-slate-500">
              Rule code
              <input
                value={ruleCode}
                onChange={(e) => setRuleCode(e.target.value)}
                className="w-56 rounded border border-slate-300 px-2 py-1 font-mono text-xs"
              />
            </label>
            <button
              onClick={() => publish.mutate()}
              disabled={publish.isPending}
              className="rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-40"
            >
              {publish.isPending ? 'Publishing…' : 'Publish rule'}
            </button>
          </div>

          {runPreview.isError && (
            <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{runPreview.error.message}</div>
          )}
          <div className="mt-3"><PreviewPanel preview={preview} /></div>
        </div>
      )}

      {publish.isSuccess && (
        <div className="rounded-xl border border-emerald-300 bg-emerald-50 p-4 text-sm text-emerald-800">
          ✓ <span className="font-mono font-medium">{published.rule_code}</span> v{published.version} is live
          and active — it fires on the next validation run, and the publish is in the audit chain.
        </div>
      )}

      {/* rules list */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        <div className="border-b border-slate-100 px-4 py-3 text-xs font-semibold uppercase tracking-wide text-slate-400">
          All rules ({rules.data?.rules?.length ?? 0})
        </div>
        {rules.isLoading ? (
          <div className="p-6 text-sm text-slate-400">Loading…</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-400">
              <tr>
                <th className="px-4 py-2 font-medium">Rule</th>
                <th className="px-4 py-2 font-medium">Scope</th>
                <th className="px-4 py-2 font-medium">Severity</th>
                <th className="px-4 py-2 font-medium">Origin</th>
                <th className="px-4 py-2 font-medium">State</th>
                <th className="px-4 py-2 font-medium"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {(rules.data?.rules ?? []).map((r) => (
                <tr key={r.id} className="hover:bg-slate-50">
                  <td className="px-4 py-2.5">
                    <div className="font-mono font-medium text-slate-800">{r.rule_code}</div>
                    {r.from_natural_language && (
                      <div className="max-w-72 truncate text-xs italic text-slate-400" title={r.natural_language_source}>
                        “{r.natural_language_source}”
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-2.5 text-slate-500">{r.scope}</td>
                  <td className="px-4 py-2.5 text-slate-500">{r.severity}</td>
                  <td className="px-4 py-2.5">
                    <span className={`rounded px-1.5 py-0.5 text-xs ${
                      r.source === 'seed' ? 'bg-slate-100 text-slate-600'
                      : r.source === 'ai_generated' ? 'bg-violet-50 text-violet-700'
                      : 'bg-sky-50 text-sky-700'}`}>
                      {r.source.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={`rounded px-2 py-0.5 text-xs ${
                      r.is_active ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'}`}>
                      {r.is_active ? 'active' : 'inactive'}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      onClick={() => toggle.mutate({ id: r.id, is_active: !r.is_active })}
                      disabled={toggle.isPending}
                      className="rounded-lg border border-slate-300 px-2.5 py-1 text-xs text-slate-600 hover:border-slate-400 disabled:opacity-40"
                    >
                      {r.is_active ? 'Deactivate' : 'Reactivate'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
