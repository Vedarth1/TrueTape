# backend/app/ingestion/aliases.py
"""Canonical field contract + strict coercion for normalization.

data/generate_dataset.py is the authoritative contract. Clean rows write
numerics via round(v, 2) -> plain floats (no commas/$) and dates/timestamps via
.isoformat() -> ISO 8601. Coercion is therefore deliberately STRICT: it accepts
exactly what a clean row emits and rejects every injected defect, which is
precisely how INVALID_NUMERIC_FORMAT / INVALID_DATE_FORMAT get caught downstream.
"""
from datetime import date, datetime, time

# The 21 canonical fields and their target types.
#   str    -> stripped text (blank => absent, not an error)
#   int    -> whole number (integral floats like "720.0" allowed; "720.5" rejected)
#   float  -> real number
#   date   -> ISO date; stored in `data` as an ISO string
#   ts     -> ISO timestamp; stored in `data` as ISO string, and drives effective_at
#   status -> payment_status: canon-mapped, never a coercion error
CANONICAL_FIELD_TYPES = {
    "loan_id": "str",
    "borrower_id": "str",
    "borrower_name": "str",
    "original_principal": "float",
    "current_balance": "float",
    "interest_rate": "float",
    "origination_date": "date",
    "maturity_date": "date",
    "loan_term_months": "int",
    "payment_status": "status",
    "days_past_due": "int",
    "property_state": "str",
    "loan_purpose": "str",
    "property_type": "str",
    "credit_score": "int",
    "ltv_ratio": "float",
    "dti_ratio": "float",
    "document_status": "str",
    "last_updated_at": "ts",
    "source_system": "str",
    "servicer_name": "str",
}

CANONICAL_FIELDS = frozenset(CANONICAL_FIELD_TYPES)

# Raw header -> canonical field. Canonical names pass through by membership; this
# table holds only *real* renames. document_manifest names its timestamp
# `received_date`; `document_type` has no canonical home (=> unmapped_columns).
# Kept intentionally minimal: the generator emits canonical headers, so every
# speculative synonym here would be a latent mis-mapping risk, not a feature.
ALIASES = {
    "received_date": "last_updated_at",
}

# raw payment_status (lower+strip) -> canonical token. Unmapped values are kept
# as their lowercased raw text so VALID_PAYMENT_STATUS fails them (a validation
# problem) rather than being silently coerced away or flagged as malformed.
PAYMENT_STATUS_CANON = {
    "current": "current",
    "30-59 days": "dpd_30_59",
    "60-89 days": "dpd_60_89",
    "90+ days": "dpd_90_plus",
    "default": "default",
    "paid off": "paid_off",
    "closed": "closed",
}


def resolve_header(header):
    """Map a raw CSV header to a canonical field, or None if unmapped."""
    h = header.strip()
    if h in ALIASES:
        return ALIASES[h]
    if h in CANONICAL_FIELDS:
        return h
    return None


def _coerce_int(v):
    f = float(v)                    # "720"/"720.0" ok; "5,000"/"N/A"/"abc" -> ValueError
    if f != int(f):
        raise ValueError("not integral")
    return int(f)

def _coerce_float(v):
    return float(v)                 # "118500.0" ok; "5,000"/"N/A"/"abc" -> ValueError

def _coerce_date(v):
    return date.fromisoformat(v).isoformat()   # ISO only; slash-dates -> ValueError

def _coerce_ts(v):                  # last_updated_at: ISO datetime or bare ISO date
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return datetime.combine(date.fromisoformat(v), time.min)

def _coerce_status(v):
    key = v.strip().lower()
    return PAYMENT_STATUS_CANON.get(key, key)  # unmappable kept as lowercased raw


def coerce_value(field, raw):
    """Coerce one raw cell for `field`. Returns (kind, value):
        ("absent", None)     blank/missing -> omit from data & field_errors
        ("value", py_value)  JSON-safe value to store in `data`
        ("ts", datetime)     timestamp: ISO copy -> data, datetime -> effective_at
        ("error", raw)       present but malformed -> record in field_errors
    """
    if raw is None:
        return ("absent", None)
    s = raw.strip() if isinstance(raw, str) else raw
    if s == "":
        return ("absent", None)

    t = CANONICAL_FIELD_TYPES.get(field, "str")
    if t == "str":
        return ("value", s)
    if t == "status":
        return ("value", _coerce_status(s))
    try:
        if t == "int":
            return ("value", _coerce_int(s))
        if t == "float":
            return ("value", _coerce_float(s))
        if t == "date":
            return ("value", _coerce_date(s))
        if t == "ts":
            return ("ts", _coerce_ts(s))
    except (ValueError, TypeError):
        return ("error", raw)
    return ("value", s)