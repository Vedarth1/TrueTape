"""AI Review Assistant endpoints.

Four endpoints trigger the deterministic AI analysis actions:

    POST /api/exceptions/<id>/analyze      -- explain_failure + suggest_correction
    POST /api/exceptions/<id>/classify     -- classify_severity
    POST /api/exceptions/<id>/note         -- generate_reviewer_note
    POST /api/ai/batch-summary            -- summarize_batch (body: {cluster_id})
"""
from __future__ import annotations

import uuid

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.models import AiRecommendation, ExceptionCluster
from app.services import ai as ai_svc

bp = Blueprint("ai", __name__, url_prefix="/api")


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message, "details": details}}), status


def _rec_summary(r: AiRecommendation) -> dict:
    return {
        "id": str(r.id),
        "action_type": r.action_type,
        "provider": r.provider,
        "model_name": r.model_name,
        "problem": r.problem,
        "evidence": r.evidence,
        "reasoning": r.reasoning,
        "suggested_field": r.suggested_field,
        "suggested_value": r.suggested_value,
        "suggested_source": r.suggested_source,
        "suggested_severity": r.suggested_severity,
        "note_text": r.note_text,
        "summary_text": r.summary_text,
        "confidence": r.confidence,
        "confidence_breakdown": r.confidence_breakdown,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@bp.post("/exceptions/<uuid:exc_id>/analyze")
@role_required("reviewer")
def analyze(exc_id):
    reviewer_id = uuid.UUID(get_jwt_identity())
    try:
        rec = ai_svc.analyze_exception(exc_id, reviewer_id)
    except ValueError:
        return _err("NOT_FOUND", "exception not found", 404)
    from app.extensions import db
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify(_rec_summary(rec)), 201


@bp.post("/exceptions/<uuid:exc_id>/classify")
@role_required("reviewer")
def classify(exc_id):
    reviewer_id = uuid.UUID(get_jwt_identity())
    try:
        rec = ai_svc.classify_severity(exc_id, reviewer_id)
    except ValueError:
        return _err("NOT_FOUND", "exception not found", 404)
    from app.extensions import db
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify(_rec_summary(rec)), 201


@bp.post("/exceptions/<uuid:exc_id>/note")
@role_required("reviewer")
def note(exc_id):
    reviewer_id = uuid.UUID(get_jwt_identity())
    try:
        rec, comment = ai_svc.generate_reviewer_note(exc_id, reviewer_id)
    except ValueError:
        return _err("NOT_FOUND", "exception not found", 404)
    from app.extensions import db
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify({
        "ai_recommendation": _rec_summary(rec),
        "comment": {"id": str(comment.id), "body": comment.body, "ai_drafted": comment.ai_drafted},
    }), 201


@bp.post("/ai/batch-summary")
@role_required("reviewer", "operator")
def batch_summary():
    body = request.get_json(silent=True) or {}
    cid = body.get("cluster_id")
    if not cid:
        return _err("MISSING", "cluster_id is required", 400)
    try:
        cluster_uuid = uuid.UUID(cid)
    except ValueError:
        return _err("BAD_UUID", "cluster_id is not a valid UUID", 400)

    reviewer_id = uuid.UUID(get_jwt_identity())
    try:
        rec = ai_svc.summarize_batch(cluster_uuid, reviewer_id)
    except ValueError as e:
        return _err("NOT_FOUND", str(e), 404)
    from app.extensions import db
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return jsonify(_rec_summary(rec)), 201
