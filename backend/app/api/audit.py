"""Audit Trail query endpoints.

Read-only access to the append-only, hash-chained audit log.

    GET /api/audit                    -- paginated list with filters
    GET /api/audit/<id>              -- single event detail
    GET /api/audit/loan/<loan_id>    -- events for a specific loan
    GET /api/audit/stats             -- aggregate counts by type/actor

All endpoints are read-only (GET). Writes go through log_event() in the service layer.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.extensions import db
from app.models import AuditEvent, Loan
from app.models.enums import EVENT_TYPES, ACTOR_TYPES

bp = Blueprint("audit", __name__, url_prefix="/api/audit")


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message,
                              "details": details}}), status


def _event_summary(e: AuditEvent) -> dict:
    return {
        "id": str(e.id),
        "seq": e.seq,
        "event_type": e.event_type,
        "entity_type": e.entity_type,
        "entity_id": str(e.entity_id) if e.entity_id else None,
        "loan_id": str(e.loan_id) if e.loan_id else None,
        "actor_id": str(e.actor_id) if e.actor_id else None,
        "actor_type": e.actor_type,
        "before_value": e.before_value,
        "after_value": e.after_value,
        "reason": e.reason,
        "ai_metadata": e.ai_metadata,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        # Hashes are included so clients can verify locally.
        "event_hash": e.event_hash,
        "prev_event_hash": e.prev_event_hash,
    }


def _parse_page():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except (ValueError, TypeError):
        per_page = 50
    return page, per_page


@bp.get("")
@role_required("operator", "reviewer")
def list_audit_events():
    """Paginated audit trail with filters.

    Query params:
      event_type   -- filter by event type (e.g. exception_created)
      entity_type  -- filter by entity type (e.g. exception_record)
      actor_type   -- filter by actor type: human | ai | system
      loan_id      -- UUID of loan
      from_seq     -- only events with seq >= this (for polling)
      to_seq       -- only events with seq <= this
      page         -- default 1
      per_page     -- default 50, max 200
    """
    q = db.select(AuditEvent)

    event_type = request.args.get("event_type")
    if event_type:
        q = q.filter(AuditEvent.event_type == event_type)

    entity_type = request.args.get("entity_type")
    if entity_type:
        q = q.filter(AuditEvent.entity_type == entity_type)

    actor_type = request.args.get("actor_type")
    if actor_type:
        q = q.filter(AuditEvent.actor_type == actor_type)

    loan_id_str = request.args.get("loan_id")
    if loan_id_str:
        try:
            loan_uuid = uuid.UUID(loan_id_str)
            q = q.filter(AuditEvent.loan_id == loan_uuid)
        except ValueError:
            return _err("BAD_UUID", "loan_id is not a valid UUID", 400)

    from_seq = request.args.get("from_seq")
    if from_seq:
        try:
            q = q.filter(AuditEvent.seq >= int(from_seq))
        except ValueError:
            pass

    to_seq = request.args.get("to_seq")
    if to_seq:
        try:
            q = q.filter(AuditEvent.seq <= int(to_seq))
        except ValueError:
            pass

    # Always order by seq (chain order).
    q = q.order_by(AuditEvent.seq.asc())

    page, per_page = _parse_page()
    paginated = db.paginate(q, page=page, per_page=per_page)

    return jsonify({
        "events": [_event_summary(e) for e in paginated.items],
        "pagination": {
            "page": paginated.page,
            "per_page": paginated.per_page,
            "total": paginated.total,
            "pages": paginated.pages,
        },
    })


@bp.get("/<uuid:event_id>")
@role_required("operator", "reviewer")
def get_audit_event(event_id):
    """Single audit event by ID."""
    event = db.session.get(AuditEvent, event_id)
    if event is None:
        return _err("NOT_FOUND", "audit event not found", 404)
    return jsonify(_event_summary(event))


@bp.get("/loan/<uuid:loan_id>")
@role_required("operator", "reviewer")
def loan_audit(loan_id):
    """All audit events for a specific loan, newest first."""
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        return _err("NOT_FOUND", "loan not found", 404)

    page, per_page = _parse_page()
    q = (db.select(AuditEvent)
         .filter(AuditEvent.loan_id == loan_id)
         .order_by(AuditEvent.seq.desc()))
    paginated = db.paginate(q, page=page, per_page=per_page)

    return jsonify({
        "loan_id": str(loan_id),
        "loan_business_id": loan.loan_id,
        "events": [_event_summary(e) for e in paginated.items],
        "pagination": {
            "page": paginated.page,
            "per_page": paginated.per_page,
            "total": paginated.total,
            "pages": paginated.pages,
        },
    })


@bp.get("/stats")
@role_required("operator", "reviewer")
def audit_stats():
    """Aggregate counts: total events, counts by event_type, by actor_type."""
    total = db.session.execute(
        db.select(sa.func.count()).select_from(AuditEvent)
    ).scalar() or 0

    by_type_rows = db.session.execute(
        db.select(AuditEvent.event_type, sa.func.count())
        .group_by(AuditEvent.event_type)
        .order_by(sa.func.count().desc())
    ).all()

    by_actor_rows = db.session.execute(
        db.select(AuditEvent.actor_type, sa.func.count())
        .group_by(AuditEvent.actor_type)
        .order_by(sa.func.count().desc())
    ).all()

    latest = db.session.execute(
        db.select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()

    return jsonify({
        "total_events": total,
        "by_event_type": {row[0]: row[1] for row in by_type_rows},
        "by_actor_type": {row[0]: row[1] for row in by_actor_rows},
        "latest_seq": latest.seq if latest else None,
        "latest_event_type": latest.event_type if latest else None,
        "latest_created_at": latest.created_at.isoformat() if latest and latest.created_at else None,
    })
