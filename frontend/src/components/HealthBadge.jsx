import { useQuery } from '@tanstack/react-query'
import api from '../lib/api'

export default function HealthBadge() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['health'],
    queryFn: async () => (await api.get('/health')).data,
    refetchInterval: 15000,
  })

  if (isLoading) return <span className="text-xs text-slate-400">checking…</span>

  if (error) {
    return (
      <span className="rounded bg-red-50 px-2 py-1 text-xs text-red-700">
        API unreachable — {error.code}
      </span>
    )
  }

  const ok = data.status === 'ok'
  return (
    <span
      className={`rounded px-2 py-1 text-xs ${
        ok ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-700'
      }`}
    >
      api {data.checks.api} · db {data.checks.database}
    </span>
  )
}