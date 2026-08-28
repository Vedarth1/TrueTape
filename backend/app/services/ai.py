"""AI Review Assistant service.

Produces DETERMINISTIC analysis for the five AI action types, writes
AiRecommendation rows with full provenance, and logs audit events. There is no
LLM in v1: every field below is computed from structured data by rule --
`model_name` is "deterministic-stub-v1" and `provider` is "deterministic", and
the recommendation rows say so on their face.

Why a stub rather than a live model: the thing worth demonstrating is the
*control surface* around AI, not the prose. Recommendations are stored as
immutable evidence, kept separate from the human decision, logged into the
audit hash chain with their model/prompt metadata, and structurally unable to
write canonical loan data. Those controls are identical whether the reasoning
text comes from a rule or from Gemini.

The seam for a real provider is deliberate but honest about not existing yet:
evidence assembly (`_build_*`) and the confidence formula
(`_compute_confidence`) are already provider-independent, so wiring an actual
model later means adding one call that returns the reasoning text and setting
`provider`/`model_name` to the truth of what served it. No `_call_provider`
method exists today; do not describe one as if it did.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.extensions import db
from app.models import (
    AiRecommendation, ExceptionCluster, ExceptionComment, ExceptionRecord,
    Loan, LoanCanonical, LoanRecord, SourceTrustConfig, ValidationRule,
)
from app.models.enums import AI_ACTION_TYPES, SEVERITIES
from app.services.audit import log_event

# Single source of truth for "who produced this recommendation". Written both
# onto the AiRecommendation row AND into the audit event's ai_metadata, so the
# provenance in the mutable rec table and the provenance in the tamper-evident
# hash chain are guaranteed to agree.
_MODEL_NAME = "deterministic-stub-v1"
_MODEL_VERSION = "1.0.0"
_PROVIDER = "deterministic"
_PROMPT_VERSION = "deterministic-v1"


def _ai_metadata() -> dict:
    """Model/prompt provenance for the audit chain.

    audit._HASH_FIELDS includes 'ai_metadata', so this dict is folded into the
    event's SHA-256 and read back verbatim by verify_chain -- which is the
    whole point: the model that produced a recommendation becomes an
    unforgeable part of the log, not just an editable column.
    """
    return {
        "model_name": _MODEL_NAME,
        "model_version": _MODEL_VERSION,
        "provider": _PROVIDER,
        "prompt_version": _PROMPT_VERSION,
    }

# Fields where a mismatch is financially material.
_HIGH_IMPACT_FIELDS = {
    "original_principal", "current_balance", "interest_rate",
    "loan_term_months", "payment_status", "dti_ratio", "credit_score",
    "ltv_ratio",
}


def _compute_confidence(exc: ExceptionRecord, context: dict) -> tuple[float, dict]:
    type_scores = {
        "validation_failure": 0.15, "duplicate": 0.20,
        "source_conflict": 0.10, "staleness": 0.18,
        "import_error": 0.05,
    }
    sev_factors = {"LOW": 0.05, "MEDIUM": 0.10, "HIGH": 0.15, "CRITICAL": 0.20}

    type_f = type_scores.get(exc.exception_type, 0.08)
    sev_f = sev_factors.get(exc.severity, 0.10)
    field = exc.field_name or ""
    impact_f = 0.20 if field in _HIGH_IMPACT_FIELDS else 0.08

    sources = context.get("source_records", [])
    src_sys = {s.get("source_system") for s in sources if s.get("source_system")}
    evidence_f = 0.20 if len(src_sys) >= 2 else (0.10 if len(src_sys) == 1 else 0.03)

    canon = context.get("canonical_data", {}) or {}
    completeness = (0.15 if canon.get(field) is not None else 0.0) +                   (0.05 if any(s.get("data", {}).get(field) is not None for s in sources) else 0.0)

    confidence = round(max(0.0, min(1.0, 0.5 + type_f + sev_f + impact_f + evidence_f + completeness)), 3)
    return confidence, {
        "base": 0.5, "type_factor": round(type_f, 3),
        "severity_factor": round(sev_f, 3), "impact_factor": round(impact_f, 3),
        "evidence_factor": round(evidence_f, 3), "completeness": round(completeness, 3),
    }


def _build_problem_text(exc: ExceptionRecord, context: dict) -> str:
    field = exc.field_name or "unknown"
    if exc.exception_type == "source_conflict":
        return f"Conflicting values for field '{field}' across data sources."
    if exc.exception_type == "staleness":
        return f"Data for field '{field}' may be stale (last updated > 180 days ago)."
    if exc.exception_type == "duplicate":
        return "Potential duplicate loan detected based on borrower fingerprint."
    rule_code = context.get("rule_code", "unknown")
    return f"Validation rule '{rule_code}' failed on field '{field}'."


def _build_evidence_dict(exc: ExceptionRecord, context: dict) -> dict:
    field = exc.field_name or ""
    sources = context.get("source_records", [])
    canon = context.get("canonical_data", {}) or {}
    evidence: dict[str, Any] = {"field": field, "canonical_value": canon.get(field)}
    sv = {}
    for s in sources:
        val = s.get("data", {}).get(field)
        if val is not None:
            sv[s["source_system"]] = val
    if sv:
        evidence["source_values"] = sv
    if exc.detail:
        evidence["detail"] = exc.detail
    return evidence


def _build_suggestion(exc: ExceptionRecord, context: dict) -> tuple[str, str, Optional[str]]:
    field = exc.field_name or ""
    canon = context.get("canonical_data", {}) or {}
    sources = context.get("source_records", [])
    trust_idx = context.get("trust_index", {})
    current = canon.get(field)

    candidates = []
    for src in sources:
        val = src.get("data", {}).get(field)
        if val is not None:
            ss = src.get("source_system", "")
            t = trust_idx.get((ss, field)) or trust_idx.get((ss, None)) or 0
            candidates.append((ss, str(val), t))
    if not candidates:
        return field, "", None

    if exc.exception_type == "source_conflict" and len(candidates) >= 2:
        candidates.sort(key=lambda c: c[2], reverse=True)
        return field, candidates[0][1], candidates[0][0]
    if exc.exception_type == "validation_failure":
        for ss, val, _ in candidates:
            if val != (str(current) if current is not None else None):
                return field, val, ss
    candidates.sort(key=lambda c: c[2], reverse=True)
    return field, candidates[0][1], candidates[0][0]


def _build_reasoning(exc, context, suggestion, confidence) -> str:
    field = exc.field_name or "unknown"
    canon = context.get("canonical_data", {}) or {}
    rule_code = context.get("rule_code", "unknown")
    parts = [f"Field '{field}'"]

    if exc.exception_type == "source_conflict":
        vals = [f"{s['source_system']}={s['data'].get(field, 'N/A')}" for s in context.get("source_records", []) if s.get("data", {}).get(field) is not None]
        parts.append(f"has conflicting values: {', '.join(vals)}.")
    elif exc.exception_type == "staleness":
        parts.append("may be stale (last update exceeds the staleness threshold).")
    elif exc.exception_type == "duplicate":
        parts.append("shares a borrower fingerprint with another loan.")
    else:
        parts.append(f"failed validation rule '{rule_code}'.")

    cur = canon.get(field, "N/A")
    parts.append(f"Current canonical value: {cur}.")

    sf, sv, ss = suggestion
    if sv:
        parts.append(f"Suggested correction: {sf} -> {sv}")
        if ss:
            parts.append(f"(source: {ss}).")
    parts.append(f"Confidence: {confidence:.0%} based on exception type, field impact, source coverage, and data completeness.")
    return " ".join(parts)


def _load_exception_context(exc_id: uuid.UUID) -> tuple[ExceptionRecord, dict[str, Any]]:
    exc = db.session.get(ExceptionRecord, exc_id)
    if exc is None:
        raise ValueError(f"exception {exc_id} not found")
    ctx: dict[str, Any] = {"exception": exc}

    if exc.rule_id:
        rule = db.session.get(ValidationRule, exc.rule_id)
        if rule:
            ctx["rule_code"] = rule.rule_code
            ctx["rule"] = rule

    if exc.loan_id:
        loan = db.session.get(Loan, exc.loan_id)
        if loan:
            ctx["loan"] = loan
            ctx["loan_business_id"] = loan.loan_id
        canon = db.session.get(LoanCanonical, exc.loan_id)
        if canon:
            ctx["canonical_data"] = canon.data or {}
            ctx["field_provenance"] = canon.field_provenance or {}

    if exc.loan_id:
        recs = db.session.execute(
            db.select(LoanRecord).filter_by(loan_id=exc.loan_id)
            .order_by(LoanRecord.source_system, LoanRecord.version)
        ).scalars().all()
        ctx["source_records"] = [
            {"source_system": r.source_system, "version": r.version, "data": r.data or {}}
            for r in recs
        ]

    trust_rows = db.session.execute(
        db.select(SourceTrustConfig.source_system, SourceTrustConfig.field_name, SourceTrustConfig.trust_score)
    ).all()
    ctx["trust_index"] = {(r[0], r[1]): r[2] for r in trust_rows}
    return exc, ctx


def analyze_exception(exc_id: uuid.UUID, reviewer_id: uuid.UUID) -> AiRecommendation:
    exc, ctx = _load_exception_context(exc_id)
    confidence, breakdown = _compute_confidence(exc, ctx)
    suggestion = _build_suggestion(exc, ctx)
    reasoning = _build_reasoning(exc, ctx, suggestion, confidence)
    sf, sv, ss = suggestion

    now = datetime.now(timezone.utc)
    rec_id = uuid.uuid4()
    start = time.monotonic()

    ai_rec = AiRecommendation(
        id=rec_id, action_type="explain_failure",
        exception_id=exc_id, cluster_id=exc.cluster_id,
        model_name=_MODEL_NAME, model_version=_MODEL_VERSION,
        provider=_PROVIDER, prompt_version=_PROMPT_VERSION,
        prompt_text="(deterministic: no LLM prompt)",
        evidence_bundle=ctx.get("canonical_data", {}),
        problem=_build_problem_text(exc, ctx),
        evidence=_build_evidence_dict(exc, ctx),
        reasoning=reasoning,
        suggested_field=sf or None, suggested_value=sv or None, suggested_source=ss,
        confidence=confidence, confidence_breakdown=breakdown,
        raw_model_response={"stub": True},
        latency_ms=int((time.monotonic() - start) * 1000),
        created_at=now,
    )
    db.session.add(ai_rec)

    log_event(
        event_type="ai_recommendation_generated", entity_type="ai_recommendation",
        entity_id=rec_id, loan_id=exc.loan_id,
        actor_id=reviewer_id, actor_type="system",
        after_value={"action_type": "explain_failure", "confidence": confidence, "suggested_field": sf},
        ai_metadata=_ai_metadata(),
        reason=f"deterministic analysis for exception {exc_id}",
    )
    return ai_rec


def classify_severity(exc_id: uuid.UUID, reviewer_id: uuid.UUID) -> AiRecommendation:
    exc, ctx = _load_exception_context(exc_id)
    field = exc.field_name or ""

    severity_order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    if field not in _HIGH_IMPACT_FIELDS and severity_order.get(exc.severity, 2) >= 3:
        suggested = "MEDIUM"
    else:
        suggested = exc.severity

    confidence, breakdown = _compute_confidence(exc, ctx)
    reasoning = (f"Rule-assigned severity is {exc.severity}. Field '{field}' "
                f"{'is' if field in _HIGH_IMPACT_FIELDS else 'is not'} in the high-impact set. "
                f"Suggested severity: {suggested}.")

    now = datetime.now(timezone.utc)
    rec_id = uuid.uuid4()
    ai_rec = AiRecommendation(
        id=rec_id, action_type="classify_severity",
        exception_id=exc_id, cluster_id=exc.cluster_id,
        model_name=_MODEL_NAME, model_version=_MODEL_VERSION,
        provider=_PROVIDER, prompt_version=_PROMPT_VERSION,
        prompt_text="(deterministic: no LLM prompt)",
        evidence_bundle={"rule_severity": exc.severity, "field": field},
        suggested_severity=suggested, reasoning=reasoning,
        confidence=confidence, confidence_breakdown=breakdown,
        raw_model_response={"stub": True}, latency_ms=0, created_at=now,
    )
    db.session.add(ai_rec)

    log_event(
        event_type="ai_recommendation_generated", entity_type="ai_recommendation",
        entity_id=rec_id, loan_id=exc.loan_id,
        actor_id=reviewer_id, actor_type="system",
        after_value={"action_type": "classify_severity", "suggested": suggested, "rule": exc.severity},
        ai_metadata=_ai_metadata(),
        reason=f"deterministic severity for exception {exc_id}",
    )
    return ai_rec


def generate_reviewer_note(exc_id: uuid.UUID, reviewer_id: uuid.UUID) -> tuple[AiRecommendation, ExceptionComment]:
    exc, ctx = _load_exception_context(exc_id)
    field = exc.field_name or "unknown"
    loan_bid = ctx.get("loan_business_id", "unknown")
    rule_code = ctx.get("rule_code", "unknown")

    note = (f"Exception on loan {loan_bid}: {exc.exception_type} on field '{field}' "
            f"(rule: {rule_code}). Current value: {ctx.get('canonical_data', {}).get(field, 'N/A')}. "
            f"Severity: {exc.severity}. Reviewer should review source records and decide.")

    confidence, breakdown = _compute_confidence(exc, ctx)
    now = datetime.now(timezone.utc)
    rec_id = uuid.uuid4()

    ai_rec = AiRecommendation(
        id=rec_id, action_type="reviewer_note",
        exception_id=exc_id, cluster_id=exc.cluster_id,
        model_name=_MODEL_NAME, model_version=_MODEL_VERSION,
        provider=_PROVIDER, prompt_version=_PROMPT_VERSION,
        prompt_text="(deterministic: no LLM prompt)",
        evidence_bundle={"loan_business_id": loan_bid, "field": field},
        note_text=note, reasoning=note,
        confidence=confidence, confidence_breakdown=breakdown,
        raw_model_response={"stub": True}, latency_ms=0, created_at=now,
    )
    db.session.add(ai_rec)

    comment = ExceptionComment(
        id=uuid.uuid4(), exception_id=exc_id,
        author_id=reviewer_id, body=note, ai_drafted=True,
    )
    db.session.add(comment)

    log_event(
        event_type="ai_recommendation_generated", entity_type="ai_recommendation",
        entity_id=rec_id, loan_id=exc.loan_id,
        actor_id=reviewer_id, actor_type="system",
        after_value={"action_type": "reviewer_note", "note_length": len(note)},
        ai_metadata=_ai_metadata(),
        reason=f"deterministic note for exception {exc_id}",
    )
    return ai_rec, comment


def summarize_batch(cluster_id: uuid.UUID, reviewer_id: uuid.UUID) -> AiRecommendation:
    cluster = db.session.get(ExceptionCluster, cluster_id)
    if cluster is None:
        raise ValueError(f"cluster {cluster_id} not found")

    excs = db.session.execute(
        db.select(ExceptionRecord).filter_by(cluster_id=cluster_id)
    ).scalars().all()
    if not excs:
        raise ValueError(f"cluster {cluster_id} has no exceptions")

    type_counts: dict[str, int] = {}
    sev_counts: dict[str, int] = {}
    fields: set[str] = set()
    for e in excs:
        type_counts[e.exception_type] = type_counts.get(e.exception_type, 0) + 1
        sev_counts[e.severity] = sev_counts.get(e.severity, 0) + 1
        if e.field_name:
            fields.add(e.field_name)
    top_type = max(type_counts, key=type_counts.get)

    summary = (f"Cluster '{cluster.cluster_label}': {len(excs)} exceptions. "
               f"Primary type: {top_type} ({type_counts[top_type]}). "
               f"Severity: {sev_counts}. Fields: {', '.join(sorted(fields))}. "
               f"Review as a group -- {top_type} exceptions often share a root cause.")

    now = datetime.now(timezone.utc)
    rec_id = uuid.uuid4()
    loan_id = next((e.loan_id for e in excs if e.loan_id), None)

    ai_rec = AiRecommendation(
        id=rec_id, action_type="batch_summary", cluster_id=cluster_id,
        model_name=_MODEL_NAME, model_version=_MODEL_VERSION,
        provider=_PROVIDER, prompt_version=_PROMPT_VERSION,
        prompt_text="(deterministic: no LLM prompt)",
        evidence_bundle={"cluster": cluster.cluster_label, "exception_count": len(excs)},
        summary_text=summary, reasoning=summary,
        confidence=0.85, confidence_breakdown={"aggregation_confidence": 0.85},
        raw_model_response={"stub": True}, latency_ms=0, created_at=now,
    )
    db.session.add(ai_rec)

    log_event(
        event_type="ai_recommendation_generated", entity_type="ai_recommendation",
        entity_id=rec_id, loan_id=loan_id,
        actor_id=reviewer_id, actor_type="system",
        after_value={"action_type": "batch_summary", "cluster_id": str(cluster_id), "count": len(excs)},
        ai_metadata=_ai_metadata(),
        reason=f"deterministic batch summary for cluster {cluster_id}",
    )
    return ai_rec
