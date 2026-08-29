# backend/app/api/rules.py
"""Validation-rule endpoints, including AI-assisted rule creation (Module D).

The flow: a reviewer describes a rule in English -> POST /api/ai/generate-rule
returns a compiled draft (never saved) -> POST /api/rules/preview dry-runs the
draft's condition over every loan_record without writing anything ->
POST /api/rules publishes it, appending a rule_created event to the audit
chain. Deactivation is its own audited event -- a rule that stops firing must
leave the same tamper-evident trail as one that started.

    POST   /api/ai/generate-rule   -- NL text -> draft rule (deterministic stub)
    POST   /api/rules/preview      -- dry-run a condition, zero writes
    GET    /api/rules              -- list all rules
    POST   /api/rules              -- publish a rule (draft or hand-written)
    PATCH  /api/rules/<id>         -- activate / deactivate (audited)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.extensions import db
from app.models import LoanRecord, ReviewerDecision  # noqa: F401 (ReviewerDecision re-exported for routes parity)
from app.models import ValidationRule
from app.models.enums import SEVERITIES
from app.services.ai import generate_rule_draft
from app.services.audit import log_event
from app.validation.dsl import EvalContext, evaluate
from app.validation.runner import _build_scope_map

bp = Blueprint("rules", __name__, url_prefix="/api")


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message,
                              "details": details}}), status


def _preview_condition(condition: dict) -> dict:
    """Dry-run one row-scope condition over every loan_record. Zero writes.

    Mirrors run_row_validation's context construction exactly (scope map,
    EvalContext, ABSENT semantics) so the preview numbers are the same ones a
    published rule would produce.
    """
    now = datetime.now(timezone.utc)
    scope_map = _build_scope_map()
    records = db.session.execute(
        db.select(LoanRecord).order_by(LoanRecord.source_system,
                                       LoanRecord.version)
    ).scalars().all()

    tally = {"pass": 0, "fail": 0, "not_applicable": 0}
    samples = []
    for rec in records:
        if rec.raw_record_id:
            in_scope = scope_map.get(rec.raw_record_id, frozenset())
        else:
            in_scope = frozenset(rec.data or {}) | frozenset(rec.field_errors or {})
        ctx = EvalContext(rec.data, rec.field_errors, in_scope, now,
                          source_system=rec.source_system)
        result, details = evaluate(condition, ctx)
        tally[result] += 1
        if result == "fail" and len(samples) < 20:
            samples.append({
                "loan_record_id": str(rec.id),
                "source_system": rec.source_system,
                "version": rec.version,
                "values": details.get("values", {}),
                "field_errors": details.get("field_errors", {}),
            })
    return {"records_evaluated": len(records), "tally": tally,
            "sample_failures": samples}


@bp.post("/ai/generate-rule")
@role_required("reviewer")
def generate_rule():
    """Natural language -> compiled rule draft. Nothing is saved here.

    Body: {"nl_text": "flag loans where interest rate is above 36"}
    The draft feeds /api/rules/preview, then /api/rules to publish.
    """
    body = request.get_json(silent=True) or {}
    nl = (body.get("nl_text") or "").strip()
    if not nl:
        return _err("EMPTY_TEXT", "nl_text is required", 400)
    if len(nl) > 500:
        return _err("TOO_LONG", "nl_text must be 500 characters or fewer", 400)

    try:
        draft = generate_rule_draft(nl)
    except ValueError as exc:
        return _err("UNPARSEABLE", str(exc), 422)

    # 201: a draft is a created artefact, just not a persisted one.
    return jsonify({"draft": draft}), 201


@bp.post("/rules/preview")
@role_required("reviewer")
def preview_rule():
    """Dry-run a condition against the current dataset. Zero writes.

    Body: {"condition": <DSL tree>, "scope": "row"}
    Returns pass/fail/not_applicable counts plus up to 20 sample failures.
    """
    body = request.get_json(silent=True) or {}
    condition = body.get("condition")
    if not isinstance(condition, dict) or not condition:
        return _err("BAD_CONDITION", "condition must be a non-empty DSL object", 400)
    if body.get("scope", "row") != "row":
        return _err("UNSUPPORTED_SCOPE",
                    "preview supports scope='row' only; dataset rules "
                    "(duplicates) are not previewable", 400)

    try:
        result = _preview_condition(condition)
    except ValueError as exc:
        return _err("BAD_CONDITION", f"condition failed to evaluate: {exc}", 422)

    return jsonify({"preview": result}), 200


@bp.get("/rules")
@role_required("operator", "reviewer", "consumer")
def list_rules():
    """All rules, newest first. Shows provenance (seed vs reviewer vs AI)."""
    rows = db.session.execute(
        db.select(ValidationRule).order_by(
            ValidationRule.created_at.desc(), ValidationRule.rule_code)
    ).scalars().all()
    return jsonify({"rules": [
        {
            "id": str(r.id),
            "rule_code": r.rule_code,
            "version": r.version,
            "scope": r.scope,
            "severity": r.severity,
            "source": r.source,
            "from_natural_language": bool(r.natural_language_source),
            "natural_language_source": r.natural_language_source,
            "is_active": r.is_active,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]})


@bp.post("/rules")
@role_required("reviewer")
def create_rule():
    """Publish a rule. Accepts an AI draft or a hand-written condition.

    Body: {"rule_code", "scope", "severity", "condition", "message_template",
           "natural_language_source"?, "explanation"?}
    Appends a rule_created event to the audit chain.
    """
    body = request.get_json(silent=True) or {}
    reviewer_id = uuid.UUID(get_jwt_identity())

    rule_code = (body.get("rule_code") or "").strip()
    scope = (body.get("scope") or "row").strip()
    severity = (body.get("severity") or "").strip()
    condition = body.get("condition")
    message = (body.get("message_template") or "").strip()

    if not rule_code or not message or not isinstance(condition, dict):
        return _err("MISSING_FIELDS",
                    "rule_code, message_template and condition are required", 400)
    if scope not in ("row", "dataset"):
        return _err("BAD_SCOPE", "scope must be 'row' or 'dataset'", 400)
    if severity not in SEVERITIES:
        return _err("BAD_SEVERITY", f"severity must be one of {SEVERITIES}", 400)
    nl_source = (body.get("natural_language_source") or "").strip() or None

    # Row-scope conditions must at least compile. Dataset conditions use the
    # check-spec vocabulary and are validated on first run.
    if scope == "row":
        try:
            _preview_condition(condition)
        except ValueError as exc:
            return _err("BAD_CONDITION", f"condition failed to evaluate: {exc}", 422)

    # Versioning: same rule_code republished bumps the version rather than
    # colliding (revision_unique_per_source spirit -- a rule's history is a
    # sequence, not an overwrite).
    existing = db.session.execute(
        db.select(sa.func.max(ValidationRule.version))
        .filter(ValidationRule.rule_code == rule_code)
    ).scalar()
    version = (existing or 0) + 1

    rule = ValidationRule(
        rule_code=rule_code,
        version=version,
        scope=scope,
        severity=severity,
        condition=condition,
        message_template=message,
        source="ai_generated" if nl_source else "reviewer",
        natural_language_source=nl_source,
        explanation=body.get("explanation"),
        created_by=reviewer_id,
        is_active=True,
    )
    db.session.add(rule)
    db.session.flush()   # materialise rule.id for the audit event

    log_event(
        event_type="rule_created",
        entity_type="validation_rule",
        entity_id=rule.id,
        actor_id=reviewer_id,
        actor_type="human",
        after_value={
            "rule_code": rule.rule_code,
            "version": rule.version,
            "scope": rule.scope,
            "severity": rule.severity,
            "source": rule.source,
            "natural_language_source": nl_source,
        },
        reason="rule published",
    )
    db.session.commit()

    return jsonify({
        "id": str(rule.id),
        "rule_code": rule.rule_code,
        "version": rule.version,
        "scope": rule.scope,
        "severity": rule.severity,
        "source": rule.source,
        "is_active": rule.is_active,
    }), 201


@bp.patch("/rules/<uuid:rule_id>")
@role_required("reviewer")
def update_rule(rule_id):
    """Activate / deactivate a rule. Both directions are audited --
    deactivation as rule_deactivated, reactivation as rule_created with a
    reactivation marker (the enum has no separate 'rule_activated' type).
    """
    body = request.get_json(silent=True) or {}
    reviewer_id = uuid.UUID(get_jwt_identity())

    rule = db.session.get(ValidationRule, rule_id)
    if rule is None:
        return _err("NOT_FOUND", "no such rule", 404)

    if "is_active" not in body:
        return _err("MISSING_FIELD", "is_active (bool) is required", 400)
    active = bool(body["is_active"])
    if active == rule.is_active:
        return _err("NO_CHANGE", f"rule is already {'active' if active else 'inactive'}", 409)

    rule.is_active = active
    log_event(
        event_type="rule_deactivated" if not active else "rule_created",
        entity_type="validation_rule",
        entity_id=rule.id,
        actor_id=reviewer_id,
        actor_type="human",
        after_value={
            "rule_code": rule.rule_code,
            "version": rule.version,
            "is_active": active,
            **({} if active else {"reactivation": False}),
            **({"reactivation": True} if active else {}),
        },
        reason="rule deactivated" if not active else "rule reactivated",
    )
    db.session.commit()

    return jsonify({
        "id": str(rule.id),
        "rule_code": rule.rule_code,
        "version": rule.version,
        "is_active": rule.is_active,
    })
