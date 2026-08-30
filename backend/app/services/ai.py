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

# Loan-identity fields: a defect here breaks joins, dedup, and provenance, so a
# severity classifier must never ease it below the rule's own assessment.
_IDENTITY_FIELDS = {"loan_id", "borrower_id"}


def _compute_confidence(exc: ExceptionRecord, context: dict) -> tuple[float, dict]:
    type_scores = {
        "source_conflict": 0.13, "duplicate": 0.11,
        "staleness": 0.09, "validation_failure": 0.07,
        "import_error": 0.04,
    }
    sev_factors = {"LOW": 0.02, "MEDIUM": 0.03, "HIGH": 0.05, "CRITICAL": 0.07}

    type_f = type_scores.get(exc.exception_type, 0.06)
    sev_f = sev_factors.get(exc.severity, 0.03)
    field = exc.field_name or ""
    impact_f = 0.10 if field in _HIGH_IMPACT_FIELDS else 0.04

    sources = context.get("source_records", [])
    src_sys = {s.get("source_system") for s in sources if s.get("source_system")}
    evidence_f = 0.22 if len(src_sys) >= 2 else (0.11 if len(src_sys) == 1 else 0.03)

    canon = context.get("canonical_data", {}) or {}
    completeness = (0.10 if canon.get(field) is not None else 0.0) +                   (0.05 if any(s.get("data", {}).get(field) is not None for s in sources) else 0.0)

    confidence = round(max(0.0, min(1.0, 0.25 + type_f + sev_f + impact_f + evidence_f + completeness)), 3)
    return confidence, {
        "base": 0.25, "type_factor": round(type_f, 3),
        "severity_factor": round(sev_f, 3), "impact_factor": round(impact_f, 3),
        "evidence_factor": round(evidence_f, 3), "completeness": round(completeness, 3),
    }


def _build_problem_text(exc, context) -> str:
    rule_code = context.get("rule_code", "unknown")
    is_dataset_rule = exc.field_name is None and (
        rule_code.startswith("DUPLICATE_") or rule_code.startswith("REPEATED_"))
    if is_dataset_rule:
        return (f"Dataset-scope check '{rule_code}' flagged this record against "
                f"others in the dataset (no single field is at fault).")
    field = exc.field_name or "unknown"
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
        current_str = str(current) if current is not None else None
        for ss, val, _ in candidates:
            if val != current_str:
                return field, val, ss
        # Every source carries the same failing value. Suggesting it back
        # would be nonsense -- the honest answer is that no source offers a
        # valid alternative, so a human must supply the correct value via
        # the edit/manual-resolution path.
        return field, "", None
    candidates.sort(key=lambda c: c[2], reverse=True)
    return field, candidates[0][1], candidates[0][0]


def _build_reasoning(exc, context, suggestion, confidence) -> str:
    rule_code = context.get("rule_code", "unknown")
    is_dataset_rule = exc.field_name is None and (
        rule_code.startswith("DUPLICATE_") or rule_code.startswith("REPEATED_"))
    field = exc.field_name or ("dataset-level check" if is_dataset_rule else "unknown")
    canon = context.get("canonical_data", {}) or {}
    parts = []

    if is_dataset_rule:
        parts.append(f"This is a dataset-scope check ({rule_code}): it compares "
                     "records against each other rather than one field's value, "
                     "so there is no single field to correct.")
    else:
        parts.append(f"Field '{field}'")

    if exc.exception_type == "source_conflict":
        vals = [f"{s['source_system']}={s['data'].get(field, 'N/A')}" for s in context.get("source_records", []) if s.get("data", {}).get(field) is not None]
        parts.append(f"has conflicting values: {', '.join(vals)}.")
    elif exc.exception_type == "staleness":
        parts.append("may be stale (last update exceeds the staleness threshold).")
    elif exc.exception_type == "duplicate":
        parts.append("shares a borrower fingerprint with another loan.")
    elif not is_dataset_rule:
        parts.append(f"failed validation rule '{rule_code}'.")

    if not is_dataset_rule:
        cur = canon.get(field, "N/A")
        parts.append(f"Current canonical value: {cur}.")

    sf, sv, ss = suggestion
    if sv:
        parts.append(f"Suggested correction: {sf} -> {sv}")
        if ss:
            parts.append(f"(source: {ss}).")
    elif exc.exception_type == "validation_failure" and not is_dataset_rule:
        parts.append("No source offers a valid alternative for this field -- "
                     "every source repeats the failing value, so a manual "
                     "correction is required (use Edit value).")
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

    order = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    inv = {1: "LOW", 2: "MEDIUM", 3: "HIGH", 4: "CRITICAL"}
    dataset_scope = exc.field_name is None
    material = (field in _HIGH_IMPACT_FIELDS or field in _IDENTITY_FIELDS
                or dataset_scope)
    rank = order.get(exc.severity, 2)

    if material:
        suggested = exc.severity
        basis = ("a financially material field" if field in _HIGH_IMPACT_FIELDS
                 else "a loan-identity field" if field in _IDENTITY_FIELDS
                 else "a dataset-scope / structural check")
        reasoning = (f"Rule-assigned severity is {exc.severity}. This exception concerns "
                     f"{basis}, so the rule-assigned severity stands. "
                     f"Suggested severity: {suggested}.")
    elif rank >= 3:
        suggested = inv[max(2, rank - 1)]
        reasoning = (f"Rule-assigned severity is {exc.severity}, but field '{field}' is "
                     f"neither financially material nor identity/structural, so the "
                     f"severity can be eased one level. Suggested severity: {suggested}.")
    else:
        suggested = exc.severity
        reasoning = (f"Rule-assigned severity is {exc.severity} and field '{field}' is "
                     f"low-impact; no change warranted. Suggested severity: {suggested}.")

    confidence, breakdown = _compute_confidence(exc, ctx)

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

    # Human prose, no raw dicts: severity and type breakdowns render as
    # "12 CRITICAL" style fragments, fields as a readable list.
    sev_part = ", ".join(f"{n} {sev}" for sev, n in
                         sorted(sev_counts.items(), key=lambda kv: -kv[1]))
    field_part = (", ".join(sorted(fields)) if fields
                  else "no single field (dataset-level or structural checks)")
    loans = len({e.loan_id for e in excs if e.loan_id})

    summary = (
        f"{len(excs)} exceptions share the root cause '{cluster.cluster_label}' "
        f"across {loans} loans. All are {top_type.replace('_', ' ')} findings "
        f"— severity mix: {sev_part}. Fields involved: {field_part}. "
        f"Reviewing them as one group usually resolves a single systematic "
        f"problem rather than {len(excs)} separate defects.")

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

# ======================================================================
# Natural-language rule generation (Module D, bullet 7)
# ======================================================================
# The stub compiles a reviewer's English sentence into the SAME JSON DSL the
# seed rules use -- the exact tree the validation runner evaluates. Nothing is
# eval'd, ever: the compiler only emits the node vocabulary below, which is
# the whole security argument for AI-authored rules (see validation_rules.
# condition's column comment).
#
# Convention: seed conditions are PASS predicates ("the row is fine when this
# holds"), so the compiler translates the reviewer's VIOLATION phrasing into
# its complement. "interest rate above 36" -> PASS: interest_rate <= 36.

import re as _re

# Canonical-field synonyms, lowercase -> canonical field name.
_FIELD_SYNONYMS = {
    "interest rate": "interest_rate", "rate": "interest_rate",
    "current balance": "current_balance", "balance": "current_balance",
    "original principal": "original_principal", "principal": "original_principal",
    "credit score": "credit_score",
    "dti": "dti_ratio", "debt to income": "dti_ratio",
    "ltv": "ltv_ratio", "loan to value": "ltv_ratio",
    "days past due": "days_past_due", "dpd": "days_past_due",
    "payment status": "payment_status", "loan status": "payment_status",
    "origination date": "origination_date",
    "maturity date": "maturity_date", "maturity": "maturity_date",
    "loan term": "loan_term_months", "term months": "loan_term_months",
    "property state": "property_state", "state": "property_state",
    "document status": "document_status", "doc status": "document_status",
    "last updated": "last_updated_at", "updated at": "last_updated_at",
    "loan id": "loan_id", "borrower id": "borrower_id",
    "borrower name": "borrower_name", "borrower": "borrower_name",
}

# Longest-first so "current balance" wins over "balance".
_SYNONYM_ORDER = sorted(_FIELD_SYNONYMS, key=len, reverse=True)

# Violation phrases -> the PASS-predicate operator they imply.
#   "above 36"  means the row is BAD when X > 36, so the rule must PASS
#               when X <= 36.
_VIOLATION_TO_PASS = [
    (r"\b(?:above|over|greater than|more than|exceeds|higher than)\s+", "<="),
    (r"\b(?:below|under|less than|lower than)\s+", ">="),
    (r"\b(?:at least|no less than)\s+", "<"),
    (r"\b(?:at most|no more than)\s+", ">"),
    (r"\b(?:equal to|equals)\s+", "!="),
]

_MISSING_PAT = _re.compile(
    r"\b(?:missing|absent|blank|empty)\b|\bis\s+missing\b", _re.IGNORECASE)
_ONE_OF_PAT = _re.compile(
    r"\b(?:one of|in)\s+(?:the\s+)?(?:set\s+)?[\[{(]?(.+?)[\]})]?\s*$",
    _re.IGNORECASE)
_NOT_PAT = _re.compile(r"\b(?:not|isn'?t|is not)\b", _re.IGNORECASE)


def _find_field(text: str) -> tuple[Optional[str], int, int]:
    """Longest-synonym match -> (field, start, end) or (None, -1, -1)."""
    low = text.lower()
    for syn in _SYNONYM_ORDER:
        idx = low.find(syn)
        if idx >= 0:
            return _FIELD_SYNONYMS[syn], idx, idx + len(syn)
    return None, -1, -1


def _parse_value(token: str):
    """'36', '36.5', '0' -> float; 'current' -> str; strips % and commas."""
    t = token.strip().rstrip("%").replace(",", "").strip()
    try:
        return float(t) if "." in t else int(t)
    except ValueError:
        return t.strip("'\"")


def _compile_clause(clause: str) -> tuple[dict, list[str]]:
    """One violation clause -> PASS-predicate DSL node + parse notes."""
    notes: list[str] = []
    field, _, after = _find_field(clause)
    if field is None:
        raise ValueError(f"no recognised field in: {clause!r}")
    rest = clause[after:].strip()
    notes.append(f"field={field}")

    # -- missing / blank field: violation is null-ness, PASS is not_null --
    if _MISSING_PAT.search(rest) or _MISSING_PAT.search(clause[:_find_field(clause)[1]] or ""):
        return ({"type": "func", "name": "not_null",
                 "args": [{"type": "field", "name": field}]},
                notes + ["violation=missing field"])

    tail = _re.sub(r"^\s*(?:is|are|was|were|must be|should be|to be)?\s*",
                   "", rest, flags=_re.IGNORECASE).strip()

    # -- set membership, both directions --
    m = _ONE_OF_PAT.search(tail)
    if m:
        raw_items = [x.strip(" '\"") for x in m.group(1).split(",") if x.strip()]
        in_set_node = {"type": "func", "name": "in_set",
                       "args": [{"type": "field", "name": field},
                                {"type": "literal", "value": raw_items}]}
        if _NOT_PAT.search(tail):
            # violation: value NOT in the set -> PASS: value in set
            return in_set_node, notes + [f"violation=not in {raw_items}"]
        # violation: value IN the set -> PASS: value not in set
        return ({"type": "not", "operand": in_set_node},
                notes + [f"violation=in {raw_items}"])

    # -- simple equality / inequality --
    m = _re.match(r"^(?:is\s+)?(not\s+)?(?:equal\s+to\s+|equals\s+)?(.+?)\s*$",
                  tail, flags=_re.IGNORECASE)
    if m and m.group(1):   # explicit "not X"
        return ({"type": "comparison", "operator": "==",
                 "left": {"type": "field", "name": field},
                 "right": {"type": "literal", "value": _parse_value(m.group(2))}},
                notes + [f"violation=!={m.group(2)}"])

    # -- ordered comparisons --
    for pat, pass_op in _VIOLATION_TO_PASS:
        m = _re.search(pat, tail, flags=_re.IGNORECASE)
        if m:
            value = _parse_value(tail[m.end():])
            return ({"type": "comparison", "operator": pass_op,
                     "left": {"type": "field", "name": field},
                     "right": {"type": "literal", "value": value}},
                    notes + [f"violation op -> PASS {pass_op} {value!r}"])

    # -- bare "X 36" or "X is 36" treated as exact-match violation --
    value = _parse_value(m.group(2)) if m else None
    if value is not None:
        return ({"type": "comparison", "operator": "!=",
                 "left": {"type": "field", "name": field},
                 "right": {"type": "literal", "value": value}},
                notes + [f"violation=!={value!r}"])

    raise ValueError(f"could not interpret the condition after {field!r}: {rest!r}")


def _suggest_rule_code(field: str, node: dict) -> str:
    """CUSTOM_<FIELD>_<KIND>[_N] -- unique-ified by the caller if taken."""
    kind = "MISSING"
    if node["type"] == "comparison":
        kind = {"<=": "MAX", ">=": "MIN", "<": "BELOW", ">": "ABOVE",
                "==": "IS_NOT", "!=": "MUST_EQUAL"}[node["operator"]]
        val = node["right"]["value"]
        kind = f"{kind}_{str(val).replace('.', '_').replace('-', 'NEG')}"
    elif node["type"] == "not":
        kind = "NOT_IN_SET"
    elif node["type"] == "func" and node["name"] == "in_set":
        kind = "IN_SET"
    code = "CUSTOM_" + field.upper() + "_" + kind
    return _re.sub(r"[^A-Z0-9_]", "_", code)[:60]


def generate_rule_draft(nl_text: str) -> dict:
    """Compile a reviewer's English sentence into a rule draft.

    Deterministic stub: no model call, every output traceable to a regex
    decision recorded in `parse_notes`. Returns the draft ready for
    POST /api/rules/preview then /api/rules publish.
    """
    text = (nl_text or "").strip()
    if not text:
        raise ValueError("empty rule description")

    # Split on AND (case-insensitive, word boundary) into clauses.
    clauses = [c.strip(" .") for c in _re.split(r"\s+and\s+", text,
                                               flags=_re.IGNORECASE) if c.strip()]
    nodes, notes = [], []
    for c in clauses:
        node, n = _compile_clause(c)
        nodes.append(node)
        notes.extend(n)

    if len(nodes) == 1:
        condition = nodes[0]
    else:
        condition = {"type": "and", "operands": nodes}

    # Which fields does the rule touch? Drives severity suggestion.
    def _fields(n, acc):
        if isinstance(n, dict):
            if n.get("type") == "field":
                acc.append(n["name"])
            for v in n.values():
                if isinstance(v, (dict, list)):
                    _fields(v, acc)
        elif isinstance(n, list):
            for x in n:
                _fields(x, acc)
        return acc

    touched = sorted(set(_fields(condition, [])))
    high = [f for f in touched if f in _HIGH_IMPACT_FIELDS]
    if "loan_id" in touched or "original_principal" in touched:
        severity = "CRITICAL"
    elif high:
        severity = "HIGH"
    else:
        severity = "MEDIUM"

    # Deterministic confidence: recognised field (+), compiled condition (+),
    # single unambiguous clause (+), all-clauses-parsed (+).
    confidence = 0.5
    confidence += 0.15 if touched else 0.0
    confidence += 0.15
    confidence += 0.1 if len(nodes) == 1 else 0.05
    confidence = round(min(confidence, 0.95), 2)

    primary_field = touched[0] if touched else "record"
    rule_code = _suggest_rule_code(primary_field, nodes[0])

    return {
        "rule_code": rule_code,
        "scope": "row",          # dataset-scope rules are out of stub scope
        "severity": severity,
        "condition": condition,
        "message_template": f"{primary_field} {{{primary_field}}} violates: {text}",
        "natural_language_source": text,
        "explanation": (
            "Compiled from the reviewer's description by deterministic-stub-v1. "
            "Conditions are PASS predicates: the rule fires when the complement "
            "of this tree evaluates false on a record."),
        "fields_referenced": touched,
        "suggested_severity": severity,
        "confidence": confidence,
        "confidence_breakdown": {
            "field_recognised": 0.15 if touched else 0.0,
            "condition_compiled": 0.15,
            "single_clause": 0.1 if len(nodes) == 1 else 0.05,
            "base": 0.5,
        },
        "parse_notes": notes,
        "ai_metadata": _ai_metadata(),
    }
