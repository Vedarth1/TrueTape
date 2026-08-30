// Shared, human-readable formatting for every page. Nothing in the UI should
// ever print a raw key or a [object Object] — everything goes through here.

export const FIELD_LABELS = {
  loan_id: 'Loan ID',
  borrower_id: 'Borrower ID',
  borrower_name: 'Borrower',
  credit_score: 'Credit score',
  current_balance: 'Current balance',
  days_past_due: 'Days past due',
  document_status: 'Document status',
  dti_ratio: 'DTI ratio',
  interest_rate: 'Interest rate',
  last_updated_at: 'Last updated',
  loan_purpose: 'Purpose',
  loan_term_months: 'Term (months)',
  ltv_ratio: 'LTV ratio',
  maturity_date: 'Maturity date',
  original_principal: 'Original principal',
  origination_date: 'Origination date',
  payment_status: 'Payment status',
  property_state: 'State',
  property_type: 'Property type',
  servicer_name: 'Servicer',
  source_system: 'Primary source',
}

export const PAYMENT_STATUS_LABELS = {
  current: 'Current',
  dpd_30_59: 'DPD 30–59 days',
  dpd_60_89: 'DPD 60–89 days',
  dpd_90_plus: 'DPD 90+ days',
  default: 'In default',
  paid_off: 'Paid off',
  closed: 'Closed',
}

const MONEY = new Set(['current_balance', 'original_principal'])
const PERCENT_OF_ONE = new Set(['dti_ratio', 'ltv_ratio']) // stored 0.31 → 31%
const PERCENT = new Set(['interest_rate'])                 // stored 7.85 → 7.85%
const DATE_ONLY = new Set(['origination_date', 'maturity_date'])
const DATETIME = new Set(['last_updated_at'])

export function isMoney(field) {
  return MONEY.has(field)
}

export function fmtMoney(v) {
  const n = Number(v)
  if (Number.isNaN(n)) return String(v)
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

export function humanize(key) {
  return String(key)
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

export function fieldLabel(field) {
  return FIELD_LABELS[field] ?? humanize(field)
}

// Format one field's value for display. Unknown fields fall back to a tidy
// generic rendering — never raw JSON.
export function formatValue(field, value) {
  if (value == null || value === '') return '—'
  if (field === 'payment_status' && PAYMENT_STATUS_LABELS[value]) return PAYMENT_STATUS_LABELS[value]
  if (MONEY.has(field)) return fmtMoney(value)
  if (PERCENT_OF_ONE.has(field)) return `${(Number(value) * 100).toFixed(1)}%`
  if (PERCENT.has(field)) return `${Number(value).toFixed(2)}%`
  if (DATE_ONLY.has(field)) {
    const d = new Date(value)
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' })
  }
  if (DATETIME.has(field)) {
    const d = new Date(value)
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString()
  }
  if (typeof value === 'number') return value.toLocaleString('en-US', { maximumFractionDigits: 2 })
  if (typeof value === 'string' && value.includes('_') && !value.includes(' ')) return humanize(value)
  return String(value)
}

// Keys that are pipeline plumbing — never worth showing to a reviewer.
const NOISY_KEYS = new Set(['out_of_scope', 'referenced'])

// True when a value carries nothing worth rendering.
export function isEmptyish(v) {
  if (v == null || v === '') return true
  if (Array.isArray(v)) return v.length === 0
  if (typeof v === 'object') return Object.keys(v).length === 0
  return false
}

export function isNoisyKey(key) {
  return NOISY_KEYS.has(String(key).toLowerCase())
}
