# backend/app/services/audit.py
"""Append-only, hash-chained audit log.

Every state change in TrueTape is recorded here via ``log_event``. Each event
stores a SHA-256 over its own contents plus the previous event's hash, forming
a chain: altering any past event breaks every hash after it, which ``verify_chain``
detects. The DB enforces append-only at the grant level (see harden-db); this
module makes the appends *unforgeable*.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text

from app.extensions import db
from app.models import AuditEvent
from app.models.enums import ACTOR_TYPES, EVENT_TYPES

# Fixed, app-reserved advisory-lock key for the single global audit chain.
# Any int is fine as long as it never changes and no other feature reuses it.
AUDIT_CHAIN_LOCK = 657_687

# The exact fields, in no particular order (canonical_json sorts them), that go
# into every event's hash. Verification rebuilds this same dict from the stored
# row, so every field here MUST be reconstructable by reading the row back.
_HASH_FIELDS = (
    "id", "event_type", "entity_type", "entity_id", "loan_id",
    "actor_id", "actor_type", "before_value", "after_value",
    "reason", "ai_metadata", "created_at",
)


def _json_default(o):
    """Deterministic fallback for types JSON can't serialise on its own."""
    if isinstance(o, datetime):
        # Normalise to UTC so an aware value and its UTC equivalent hash the
        # same; a naive value is assumed to already be UTC.
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        return o.astimezone(timezone.utc).isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)          # str, never float — no precision drift
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"cannot canonicalise {type(o).__name__} for the audit hash")


def canonical_json(payload) -> str:
    """One and only one byte-string for a given logical value.

    sorted keys + tight separators + a fixed default handler => the same input
    always hashes identically, on any machine, in any Python dict order.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _digest(fields: dict, prev_hash: Optional[str]) -> str:
    material = {k: fields.get(k) for k in _HASH_FIELDS}
    material["prev_event_hash"] = prev_hash
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


def log_event(
    *,
    event_type: str,
    entity_type: str,
    actor_type: str,
    entity_id: Optional[uuid.UUID] = None,
    loan_id: Optional[uuid.UUID] = None,
    actor_id: Optional[uuid.UUID] = None,
    before_value: Optional[dict] = None,
    after_value: Optional[dict] = None,
    reason: Optional[str] = None,
    ai_metadata: Optional[dict] = None,
) -> AuditEvent:
    """Append one event to the chain. Does NOT commit — the caller's request
    handler commits, so the event is atomic with the change it describes.

    before_value / after_value / ai_metadata must be JSON-primitive dicts
    (stringify money and dates before passing) so the value read back from
    JSONB equals what was hashed.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event_type {event_type!r}")
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"unknown actor_type {actor_type!r}")

    # Serialise all appends so two workers can't read the same tip and fork the
    # chain. Transaction-scoped: released on the caller's commit/rollback.
    db.session.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                       {"k": AUDIT_CHAIN_LOCK})

    # Flush pending events from this same transaction so they carry a seq and
    # the tip query sees them — multi-event requests then chain correctly.
    db.session.flush()
    prev_hash = db.session.execute(
        text("SELECT event_hash FROM audit_events ORDER BY seq DESC LIMIT 1")
    ).scalar()

    fields = {
        "id": uuid.uuid4(),
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "loan_id": loan_id,
        "actor_id": actor_id,
        "actor_type": actor_type,
        "before_value": before_value,
        "after_value": after_value,
        "reason": reason,
        "ai_metadata": ai_metadata,
        # Python-side, not the DB default, so the exact value is hashed and
        # re-readable for verification.
        "created_at": datetime.now(timezone.utc),
    }
    event_hash = _digest(fields, prev_hash)

    event = AuditEvent(**fields, event_hash=event_hash, prev_event_hash=prev_hash)
    db.session.add(event)
    db.session.flush()   # assigns seq; a CHECK violation surfaces here, not at commit
    return event


def verify_chain(loan_id: Optional[uuid.UUID] = None):
    """Walk the whole chain in seq order, recomputing every hash and checking
    each link. Returns (ok, detail). detail is None when ok, else the first
    broken seq with the reason. The chain is global, so a loan_id filter is not
    applied to the walk itself — links span loans.
    """
    rows = db.session.execute(text(
        "SELECT id, seq, event_type, entity_type, entity_id, loan_id, actor_id, "
        "actor_type, before_value, after_value, reason, ai_metadata, created_at, "
        "event_hash, prev_event_hash FROM audit_events ORDER BY seq ASC"
    )).mappings().all()

    prev = None
    for r in rows:
        fields = {
            "id": r["id"], "event_type": r["event_type"],
            "entity_type": r["entity_type"], "entity_id": r["entity_id"],
            "loan_id": r["loan_id"], "actor_id": r["actor_id"],
            "actor_type": r["actor_type"], "before_value": r["before_value"],
            "after_value": r["after_value"], "reason": r["reason"],
            "ai_metadata": r["ai_metadata"], "created_at": r["created_at"],
        }
        if r["prev_event_hash"] != prev:
            return False, {"seq": r["seq"], "reason": "broken link",
                           "expected_prev": prev, "stored_prev": r["prev_event_hash"]}
        if _digest(fields, r["prev_event_hash"]) != r["event_hash"]:
            return False, {"seq": r["seq"], "reason": "hash mismatch",
                           "recomputed": _digest(fields, r["prev_event_hash"]),
                           "stored": r["event_hash"]}
        prev = r["event_hash"]
    return True, None