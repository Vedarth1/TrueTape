# backend/app/services/canonical.py
"""Per-field survivorship: blend every source's view of a loan into one
canonical record, picking the most trusted source for each field.

This is the heart of TrueTape's value proposition. A loan appears in up to
three sources -- OriginationCore (authoritative at closing), ServicerFeed
(authoritative on current state), DocumentManifest (authoritative on
documents) -- and the canonical record is the per-field blend that takes each
field from the source most likely to be right about it. The field_provenance
column records which source won each field, so the answer to "why is
current_balance 457789.32?" is a one-column lookup, not a cross-join through
loan_records.

Trust scores come from source_trust_config (seeded from
data/seed/trust_config.json), not from a hardcoded dict, so a policy change
is a data change, not a code change or a redeploy.

Pinned fields: if loan_canonical already exists for a loan with entries in
pinned_fields, those values are carried forward untouched. The survivorship
engine SKIPS them, which is what makes a reviewer's decision sticky: tomorrow's
import cannot quietly revert today's human override.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

import sqlalchemy as sa

from app.extensions import db
from app.models import Loan, LoanCanonical, LoanRecord, SourceTrustConfig
from app.services.audit import log_event


def _load_trust_index():
    """{(source_system, field_name|None): trust_score} from source_trust_config.

    Two entries per source are expected: one with field_name=None (the base
    score) and optionally one or more with specific field names (overrides).
    The survivorship lookup falls back to the base score when no field-specific
    row exists.
    """
    rows = db.session.execute(
        db.select(SourceTrustConfig.source_system,
                  SourceTrustConfig.field_name,
                  SourceTrustConfig.trust_score)
    ).all()
    return {(src, fld): score for src, fld, score in rows}


def _effective_trust(trust_index, source_system, field_name):
    """Trust score for a (source, field) pair, falling back to the source base.

    A source that has no row at all is absent from the index and returns
    None -- the caller treats None as 'not a candidate', which is correct: a
    source the admin never configured should never win a field by accident.
    """
    score = trust_index.get((source_system, field_name))
    if score is not None:
        return score
    return trust_index.get((source_system, None))


def build_canonical():
    """Blend all loan_records into loan_canonical rows. Does NOT commit.

    For every loan in the loans table, gather its loan_records (all sources,
    all versions), and for each of the 21 canonical fields pick the value from
    the record whose source_system has the highest trust score for that field.
    Fields carried by no source are simply absent from the canonical dict --
    they are not coerced to None, because None and 'absent' mean different
    things downstream (REQUIRED_DOCUMENT_STATUS treats absent as
    not_applicable and None as fail).
    """
    trust_index = _load_trust_index()

    # Pre-load existing canonical rows so pinned fields survive the rebuild.
    existing = {
        row.loan_id: row
        for row in db.session.execute(db.select(LoanCanonical)).scalars().all()
    }

    loans = db.session.execute(
        db.select(Loan).order_by(Loan.loan_id)
    ).scalars().all()

    # Group all loan_records by their loan FK (UUID), latest version first.
    records_by_loan = defaultdict(list)
    for rec in db.session.execute(
        db.select(LoanRecord).order_by(
            LoanRecord.loan_id, LoanRecord.source_system,
            LoanRecord.version.desc())
    ).scalars().all():
        records_by_loan[rec.loan_id].append(rec)

    canonical_rows = []
    fields_blended = 0
    loans_processed = 0

    for loan in loans:
        recs = records_by_loan.get(loan.id, [])
        if not recs:
            continue
        loans_processed += 1

        prev = existing.get(loan.id)
        pinned = (prev.pinned_fields or {}) if prev else {}

        canonical_data = {}
        provenance = {}

        # Carry forward pinned fields before the blend -- a human override is
        # sticky by design and must not be re-derived from machine sources.
        for field, value in pinned.items():
            canonical_data[field] = value
            provenance[field] = {
                "source_system": "human_override",
                "trust_score": 100,
                "pinned": True,
            }

        # For each field, find the highest-trust source that actually carries
        # a value. Records are ordered version DESC, so the first match for a
        # given source is its latest revision -- a stale v1 cannot win over a
        # corrected v2 from the same source.
        seen_sources = {}
        for rec in recs:
            if rec.source_system not in seen_sources:
                seen_sources[rec.source_system] = rec

        for source_system, rec in seen_sources.items():
            data = rec.data or {}
            for field, value in data.items():
                if field in pinned:
                    continue
                score = _effective_trust(trust_index, source_system, field)
                if score is None:
                    continue
                current = provenance.get(field)
                if current is None or score > current["trust_score"]:
                    canonical_data[field] = value
                    provenance[field] = {
                        "source_system": source_system,
                        "trust_score": score,
                        "record_id": str(rec.id),
                        "record_version": rec.version,
                    }
                    fields_blended += 1

        canonical_rows.append({
            "loan_id": loan.id,
            "data": canonical_data,
            "field_provenance": provenance,
            "pinned_fields": pinned,
            "computed_at": sa.func.now(),
        })

    # Upsert: update existing rows in place, insert new ones. The shared PK
    # (loan_id) makes this a natural upsert; deleting-all-then-inserting would
    # drop pinned_fields and break the stickiness contract.
    if canonical_rows:
        for row in canonical_rows:
            lid = row["loan_id"]
            if lid in existing:
                existing[lid].data = row["data"]
                existing[lid].field_provenance = row["field_provenance"]
                existing[lid].pinned_fields = row["pinned_fields"]
                existing[lid].computed_at = sa.func.now()
            else:
                db.session.add(LoanCanonical(
                    loan_id=row["loan_id"],
                    data=row["data"],
                    field_provenance=row["field_provenance"],
                    pinned_fields=row["pinned_fields"],
                ))

    log_event(
        event_type="validation_executed",
        entity_type="canonical_blend",
        actor_type="system",
        after_value={
            "loans_processed": loans_processed,
            "fields_blended": fields_blended,
            "canonical_rows": len(canonical_rows),
        },
        reason="canonical survivorship blend",
    )

    return {"loans_processed": loans_processed,
            "fields_blended": fields_blended,
            "canonical_rows": len(canonical_rows)}
