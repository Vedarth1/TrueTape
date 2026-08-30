# backend/app/services/verification.py
"""Verified record creation: the output artefact of the whole pipeline.

A verified record is the signed-off snapshot of a loan's canonical data at
the moment a reviewer approves it. It is:
  - hash-chained per loan (prev_record_hash), so the version history of one
    loan is independently verifiable
  - immutable (UPDATE/DELETE revoked at the grant level -- see harden-db)
  - scored with a trust_score that decomposes into four explainable factors

The trust score is a weighted blend, not a magic number:

    40%  validation pass rate   severity-weighted pass / (pass + fail)
    30%  exception health       severity-weighted resolved / total (or 100 if none)
    15%  source coverage        distinct sources / 3
    15%  source trust average   mean trust of the winning source per field

Pass rate and exception health are SEVERITY-WEIGHTED (a CRITICAL result counts
8x a LOW one), so one critical data defect cannot be averaged away by a crowd
of trivial passes. A loan with no validation evidence at all is capped low
rather than inheriting a reassuring score from source coverage alone -- absence
of failures is not evidence of correctness. Each factor is 0-100, so the
composite is 0-100, and the breakdown column stores every factor -- plus the
severity weights and whether the no-evidence cap fired -- so the UI can show
WHY the score is 73, not just that it is.

A loan is eligible for verification when it has no open BLOCKING exceptions
(HIGH/CRITICAL). Non-blocking exceptions (LOW/MEDIUM) may remain open --
they are surfaced but do not gate the verified record.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import text

from app.extensions import db
from app.models import (ExceptionRecord, Loan, LoanCanonical, LoanRecord,
                        RawFile, RawRecord, ReviewerDecision, ValidationResult,
                        ValidationRule, VerifiedRecord)
from app.services.audit import canonical_json, log_event

# Fixed advisory-lock key space for per-loan verified-record chains.
# Loan-id is hashed to a positive int32 for pg_advisory_xact_lock, which
# accepts a single bigint or two int4s. Two int4s gives a wider key space
# without collisions: a fixed class key + a per-loan key.
_VR_LOCK_CLASS = 42

# Severity weights for the trust score: how far a single pass/fail of each
# severity moves the validation and exception factors. A CRITICAL result counts
# 8x a LOW one, so one critical defect cannot be diluted by a crowd of trivial
# passes. Data, not magic -- echoed into the score breakdown for the UI.
_SEVERITY_WEIGHT = {"LOW": 1.0, "MEDIUM": 2.0, "HIGH": 4.0, "CRITICAL": 8.0}

# Ceiling for a loan with NO validation evidence at all. An unvalidated loan is
# capped here rather than inheriting a reassuring composite from source coverage
# and trust alone -- "nothing has been checked" must not read as "all clear".
_NO_EVIDENCE_CAP = 25.0


def _loan_lock_key(loan_id: uuid.UUID) -> int:
    """Derive a positive int32 from the loan UUID for advisory locking."""
    h = hashlib.sha256(loan_id.bytes).hexdigest()
    return abs(int(h[:8], 16)) % (2**31)


def _compute_trust_score(canon: LoanCanonical, loan_id: uuid.UUID) -> tuple[float, dict]:
    """Return (score, breakdown) for a loan's canonical record.

    Four factors, each 0-100, weighted to a 0-100 composite:
      40% validation pass rate   (severity-weighted)
      30% exception health       (severity-weighted)
      15% source coverage
      15% source trust average

    A CRITICAL pass/fail moves the first two factors 8x as far as a LOW one,
    and a loan with no validation evidence at all is capped at _NO_EVIDENCE_CAP
    so an unchecked loan cannot masquerade as trustworthy.
    """
    # --- Factor 1: severity-weighted validation pass rate ---
    # Join to the rule so each pass/fail carries its severity. A flat pass/fail
    # ratio scores a loan with a negative current_balance the same as one with
    # an unrecognised payment-status code; severity weighting makes a CRITICAL
    # failure cost 8x a LOW one, which is how the risk actually reads.
    rows = db.session.execute(
        db.select(ValidationResult.result, ValidationRule.severity)
        .join(ValidationRule, ValidationResult.rule_id == ValidationRule.id)
        .filter(ValidationResult.loan_id == loan_id)
        .filter(ValidationResult.loan_record_id.isnot(None))
    ).all()
    evidence_count = len(rows)
    passed = sum(1 for r, _ in rows if r == "pass")
    failed = sum(1 for r, _ in rows if r == "fail")
    not_applicable = evidence_count - passed - failed
    w_pass = sum(_SEVERITY_WEIGHT.get(sev, 1.0) for r, sev in rows if r == "pass")
    w_fail = sum(_SEVERITY_WEIGHT.get(sev, 1.0) for r, sev in rows if r == "fail")
    w_total = w_pass + w_fail
    if w_total:
        pass_rate = w_pass / w_total * 100
    elif evidence_count:
        # Rules ran but every one was not_applicable (e.g. a document-only
        # loan): nothing was checkable, so nothing failed -- not a red flag.
        pass_rate = 100.0
    else:
        # No validation results at all: the loan was never validated. Absence
        # of failure is not evidence of correctness, so this factor earns zero
        # and the no-evidence cap below keeps the composite honest.
        pass_rate = 0.0
    has_validation_evidence = evidence_count > 0

    # --- Factor 2: severity-weighted exception health ---
    # Weighted the same way: an unresolved CRITICAL exception drags health down
    # far more than an unresolved LOW one, instead of counting one-for-one.
    excs = db.session.execute(
        db.select(ExceptionRecord.status, ExceptionRecord.severity)
        .filter_by(loan_id=loan_id)
    ).all()
    total_exc = len(excs)
    resolved_exc = sum(1 for st, _ in excs if st in ("resolved", "rejected"))
    w_exc_total = sum(_SEVERITY_WEIGHT.get(sev, 1.0) for _, sev in excs)
    w_exc_resolved = sum(_SEVERITY_WEIGHT.get(sev, 1.0)
                         for st, sev in excs if st in ("resolved", "rejected"))
    exc_health = (w_exc_resolved / w_exc_total * 100) if w_exc_total else 100.0

    # --- Factor 3: source coverage ---
    source_systems = set()
    for prov in (canon.field_provenance or {}).values():
        if isinstance(prov, dict):
            src = prov.get("source_system")
            if src and src != "human_override":
                source_systems.add(src)
    coverage = (len(source_systems) / 3 * 100) if source_systems else 0.0

    # --- Factor 4: source trust average ---
    trust_scores = []
    for prov in (canon.field_provenance or {}).values():
        if isinstance(prov, dict):
            t = prov.get("trust_score")
            if t is not None:
                trust_scores.append(t)
    trust_avg = (sum(trust_scores) / len(trust_scores)) if trust_scores else 0.0

    score = (
        pass_rate * 0.40
        + exc_health * 0.30
        + coverage * 0.15
        + trust_avg * 0.15
    )

    # No-evidence cap: a loan that was never validated must not reach a
    # reassuring score on the strength of source coverage and trust alone.
    # Without this, an unvalidated loan with three sources scored ~60/100 --
    # "looks fine" purely because nothing had been checked yet.
    capped = not has_validation_evidence and score > _NO_EVIDENCE_CAP
    if capped:
        score = _NO_EVIDENCE_CAP
    score = round(max(0.0, min(100.0, score)), 2)

    breakdown = {
        "validation_pass_rate": round(pass_rate, 2),
        "exception_health": round(exc_health, 2),
        "source_coverage": round(coverage, 2),
        "source_trust_average": round(trust_avg, 2),
        "weights": {
            "validation_pass_rate": 0.40,
            "exception_health": 0.30,
            "source_coverage": 0.15,
            "source_trust_average": 0.15,
        },
        "severity_weights": _SEVERITY_WEIGHT,
        "validation_counts": {
            "pass": passed, "fail": failed,
            "not_applicable": not_applicable,
        },
        "exception_counts": {
            "total": total_exc, "resolved": resolved_exc,
            "open": total_exc - resolved_exc,
        },
        "sources": sorted(source_systems),
        "has_validation_evidence": has_validation_evidence,
        "no_evidence_cap": _NO_EVIDENCE_CAP if capped else None,
    }
    return score, breakdown


def _build_source_files(loan_id: uuid.UUID) -> list[dict]:
    """The source files that contributed to this loan, with SHA-256 hashes.

    Walks loan_records -> raw_records -> raw_files so the verified record
    carries provable file-level lineage without a join at read time.
    """
    raw_file_ids = {
        rr_id[0]
        for rr_id in db.session.execute(
            db.select(LoanRecord.raw_record_id).filter_by(loan_id=loan_id)
        ).all()
        if rr_id[0]
    }
    if not raw_file_ids:
        return []

    raw_file_map = dict(db.session.execute(
        db.select(RawRecord.id, RawRecord.raw_file_id)
        .filter(RawRecord.id.in_(raw_file_ids))
    ).all())

    file_ids = {fid for fid in raw_file_map.values() if fid}
    if not file_ids:
        return []

    files = db.session.execute(
        db.select(RawFile)
        .filter(RawFile.id.in_(file_ids))
    ).scalars().all()

    return [
        {
            "file_id": str(f.id),
            "filename": f.filename,
            "file_kind": f.file_kind,
            "source_system": f.source_system,
            "file_hash": f.file_hash,
        }
        for f in files
    ]


def _build_validation_summary(loan_id: uuid.UUID) -> dict:
    """Compact summary of validation outcomes for this loan."""
    results = db.session.execute(
        db.select(
            ValidationResult.result,
            sa.func.count(ValidationResult.id),
        )
        .filter_by(loan_id=loan_id)
        .filter(ValidationResult.loan_record_id.isnot(None))
        .group_by(ValidationResult.result)
    ).all()

    counts = {r: n for r, n in results}
    total = sum(counts.values())

    excs = db.session.execute(
        db.select(
            ExceptionRecord.exception_type,
            ExceptionRecord.severity,
            ExceptionRecord.status,
            sa.func.count(ExceptionRecord.id),
        )
        .filter_by(loan_id=loan_id)
        .group_by(
            ExceptionRecord.exception_type,
            ExceptionRecord.severity,
            ExceptionRecord.status,
        )
    ).all()

    return {
        "validation": {
            "total": total,
            "pass": counts.get("pass", 0),
            "fail": counts.get("fail", 0),
            "not_applicable": counts.get("not_applicable", 0),
        },
        "exceptions": {
            "total": sum(n for _, _, _, n in excs),
            "by_type": {
                t: sum(n for et, _, st, n in excs if et == t)
                for t, _, _, _ in excs
            },
            "blocking_open": sum(
                n for _, sev, st, n in excs
                if sev in ("HIGH", "CRITICAL") and st == "open"
            ),
        },
    }


def _digest_verified_record(fields: dict, prev_hash: Optional[str]) -> str:
    """SHA-256 over the canonical JSON of the verified record's chain fields."""
    material = {
        "id": fields["id"],
        "loan_id": fields["loan_id"],
        "version": fields["version"],
        "canonical_data": fields["canonical_data"],
        "field_provenance": fields["field_provenance"],
        "validation_summary": fields["validation_summary"],
        "source_files": fields["source_files"],
        "trust_score": fields["trust_score"],
        "trust_score_breakdown": fields["trust_score_breakdown"],
        "verified_by": fields["verified_by"],
        "verified_at": fields["verified_at"],
        "prev_record_hash": prev_hash,
    }
    return hashlib.sha256(
        canonical_json(material).encode("utf-8")
    ).hexdigest()


def _loan_has_open_blocking(loan_id: uuid.UUID) -> int:
    """Count of open blocking exceptions for a loan. 0 means eligible."""
    return db.session.execute(
        db.select(sa.func.count(ExceptionRecord.id))
        .filter(
            ExceptionRecord.loan_id == loan_id,
            ExceptionRecord.is_blocking.is_(True),
            ExceptionRecord.status == "open",
        )
    ).scalar()


def verify_loan(loan_id: uuid.UUID, reviewer_id: uuid.UUID) -> tuple[VerifiedRecord, bool]:
    """Create a verified_record for one loan. Does NOT commit.

    Returns (record, created). `created` is False when the loan's latest
    verified record already captures the identical canonical snapshot, trust
    score, validation summary and source files -- re-verifying an unchanged
    loan is a no-op that returns the existing record instead of minting a
    duplicate version, so a double-clicked "Verify" cannot fork the chain into
    v2, v3, ... of byte-identical content.

    Raises RuntimeError if the loan has open blocking exceptions.
    The caller (request handler) commits so the verified record, the audit
    event, and any canonical update are atomic.
    """
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        raise ValueError(f"loan {loan_id} not found")

    blocking = _loan_has_open_blocking(loan_id)
    if blocking:
        raise RuntimeError(
            f"loan {loan.loan_id} has {blocking} open blocking exception(s); "
            "resolve them before verifying")

    canon = db.session.get(LoanCanonical, loan_id)
    if canon is None:
        raise RuntimeError(
            f"loan {loan.loan_id} has no canonical record; "
            "run build_canonical first")

    trust_score, trust_breakdown = _compute_trust_score(canon, loan_id)
    source_files = _build_source_files(loan_id)
    val_summary = _build_validation_summary(loan_id)

    # Per-loan advisory lock so two verifications of the same loan can't
    # read the same tip and fork the chain. Transaction-scoped.
    lock_key = _loan_lock_key(loan_id)
    db.session.execute(
        text("SELECT pg_advisory_xact_lock(:class, :key)"),
        {"class": _VR_LOCK_CLASS, "key": lock_key},
    )

    # Find the latest version for this loan to chain from.
    prev = db.session.execute(
        db.select(VerifiedRecord)
        .filter_by(loan_id=loan_id)
        .order_by(VerifiedRecord.version.desc())
        .limit(1)
    ).scalars().first()

    # Idempotency: if the latest verified record already captures this exact
    # snapshot, re-verifying changes nothing -- return it rather than appending
    # a byte-identical v+1. source_files is compared order-insensitively (its
    # query has no ORDER BY) and trust_score is float-coerced (it returns from
    # NUMERIC as Decimal). Anything that genuinely changed -- an edited field, a
    # resolved exception that moved the score -- falls through and mints a new
    # version, which is exactly what should happen.
    if prev is not None and (
        (canon.data or {}) == (prev.canonical_data or {})
        and (canon.field_provenance or {}) == (prev.field_provenance or {})
        and float(prev.trust_score) == trust_score
        and val_summary == prev.validation_summary
        and sorted(source_files, key=lambda f: f["file_id"])
        == sorted(prev.source_files or [], key=lambda f: f["file_id"])
    ):
        return prev, False

    version = (prev.version + 1) if prev else 1

    # Collect decision IDs that touched this loan's exceptions.
    decision_ids = [
        str(d) for d in db.session.execute(
            db.select(ReviewerDecision.id)
            .filter_by(loan_id=loan_id)
            .order_by(ReviewerDecision.decided_at)
        ).scalars().all()
    ]

    now = datetime.now(timezone.utc)
    rec_id = uuid.uuid4()

    fields = {
        "id": rec_id,
        "loan_id": loan_id,
        "version": version,
        "canonical_data": canon.data,
        "field_provenance": canon.field_provenance,
        "validation_summary": val_summary,
        "source_files": source_files,
        "trust_score": trust_score,
        "trust_score_breakdown": trust_breakdown,
        "verified_by": reviewer_id,
        "verified_at": now,
    }
    record_hash = _digest_verified_record(fields, prev.record_hash if prev else None)

    vr = VerifiedRecord(
        id=rec_id,
        loan_id=loan_id,
        version=version,
        canonical_data=canon.data,
        field_provenance=canon.field_provenance,
        validation_summary=val_summary,
        source_files=source_files,
        decision_ids=decision_ids or None,
        ai_recommendation_ids=None,
        trust_score=trust_score,
        trust_score_breakdown=trust_breakdown,
        record_hash=record_hash,
        prev_record_hash=prev.record_hash if prev else None,
        verified_by=reviewer_id,
        verified_at=now,
    )
    db.session.add(vr)

    # Update loan status
    loan.status = "verified"

    # Stringify money/dates for the audit payload (audit contract: JSON-primitive)
    audit_after = {
        "version": version,
        "trust_score": trust_score,
        "trust_score_breakdown": trust_breakdown,
        "validation_summary": val_summary,
        "source_file_count": len(source_files),
        "record_hash": record_hash,
        "prev_record_hash": prev.record_hash if prev else None,
    }
    log_event(
        event_type="verified_record_created",
        entity_type="verified_record",
        entity_id=rec_id,
        loan_id=loan_id,
        actor_id=reviewer_id,
        actor_type="human",
        after_value=audit_after,
        reason=f"loan {loan.loan_id} verified (v{version}, trust={trust_score})",
    )

    return vr, True


def verify_eligible_loans(reviewer_id: uuid.UUID, limit: int = 0) -> dict:
    """Verify every loan with no open blocking exceptions. Does NOT commit.

    Skips loans that already have a verified_record (re-verification is a
    per-loan explicit action, not a batch one). Returns a summary dict.
    """
    # Loans with open blocking exceptions are excluded.
    blocked_loan_ids = {
        r[0] for r in db.session.execute(
            db.select(ExceptionRecord.loan_id)
            .filter(
                ExceptionRecord.is_blocking.is_(True),
                ExceptionRecord.status == "open",
            )
        ).all()
        if r[0]
    }

    # Loans that already have a verified record.
    already_verified = {
        r[0] for r in db.session.execute(
            db.select(VerifiedRecord.loan_id)
        ).all()
    }

    loans = db.session.execute(
        db.select(Loan)
        .filter(Loan.id.notin_(blocked_loan_ids | already_verified))
        .order_by(Loan.loan_id)
    ).scalars().all()

    if limit:
        loans = loans[:limit]

    verified = 0
    skipped = 0
    errors = []

    for loan in loans:
        try:
            _vr, created = verify_loan(loan.id, reviewer_id)
            if created:
                verified += 1
            else:
                # Already verified at this exact snapshot (idempotent no-op).
                # Cannot occur here today -- already-verified loans are excluded
                # above -- but counting it keeps the tuple contract honest.
                skipped += 1
        except (RuntimeError, ValueError) as exc:
            skipped += 1
            errors.append({"loan_id": str(loan.id),
                           "loan_business_id": loan.loan_id,
                           "reason": str(exc)})

    return {
        "verified": verified,
        "skipped": skipped,
        "errors": errors,
        # Why the batch may be zero-sized: everything either already has a
        # verified record or is still blocked by an open blocking exception.
        "already_verified": len(already_verified),
        "blocked": len(blocked_loan_ids),
        "eligible_seen": len(loans),
    }


def verify_record_chain(loan_id: Optional[uuid.UUID] = None) -> tuple[bool, dict | None]:
    """Walk the per-loan verified_record chain and check every link.

    If loan_id is given, checks only that loan's chain. Otherwise checks
    every loan's chain independently (each loan has its own chain).
    """
    query = db.select(VerifiedRecord)
    if loan_id:
        query = query.filter_by(loan_id=loan_id)
    query = query.order_by(VerifiedRecord.loan_id, VerifiedRecord.version)

    rows = db.session.execute(query).scalars().all()

    # Group by loan so each chain is walked independently.
    chains: dict[uuid.UUID, list[VerifiedRecord]] = {}
    for vr in rows:
        chains.setdefault(vr.loan_id, []).append(vr)

    for lid, chain in chains.items():
        prev = None
        for vr in chain:
            if vr.prev_record_hash != (prev.record_hash if prev else None):
                return False, {
                    "loan_id": str(lid),
                    "version": vr.version,
                    "reason": "broken link",
                    "expected_prev": prev.record_hash if prev else None,
                    "stored_prev": vr.prev_record_hash,
                }
            fields = {
                "id": vr.id,
                "loan_id": vr.loan_id,
                "version": vr.version,
                "canonical_data": vr.canonical_data,
                "field_provenance": vr.field_provenance,
                "validation_summary": vr.validation_summary,
                "source_files": vr.source_files,
                "trust_score": vr.trust_score,
                "trust_score_breakdown": vr.trust_score_breakdown,
                "verified_by": vr.verified_by,
                "verified_at": vr.verified_at,
            }
            recomputed = _digest_verified_record(
                fields, vr.prev_record_hash)
            if recomputed != vr.record_hash:
                return False, {
                    "loan_id": str(lid),
                    "version": vr.version,
                    "reason": "hash mismatch",
                    "recomputed": recomputed,
                    "stored": vr.record_hash,
                }
            prev = vr

    return True, None
