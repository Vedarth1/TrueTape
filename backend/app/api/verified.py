# backend/app/api/verified.py
"""Verified record endpoints: verify a loan, list verified records, verify chain.

These are the output side of the pipeline. A reviewer verifies a loan after
resolving its blocking exceptions; the system creates a hash-chained
verified_record with a trust score. The chain can be independently verified
to prove no verified record was tampered with.
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.extensions import db
from app.models import Loan, VerifiedRecord
from app.services.audit import verify_chain
from app.services.verification import verify_eligible_loans, verify_loan, verify_record_chain

bp = Blueprint("verified", __name__, url_prefix="/api")


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message,
                              "details": details}}), status


def _verified_summary(vr: VerifiedRecord, loan_business_id: str | None = None) -> dict:
    return {
        "id": str(vr.id),
        "loan_id": str(vr.loan_id),
        "loan_business_id": loan_business_id,
        "version": vr.version,
        "trust_score": float(vr.trust_score),
        "trust_score_breakdown": vr.trust_score_breakdown,
        "validation_summary": vr.validation_summary,
        "source_files": vr.source_files,
        "decision_ids": vr.decision_ids or [],
        "record_hash": vr.record_hash,
        "prev_record_hash": vr.prev_record_hash,
        "verified_by": str(vr.verified_by),
        "verified_at": vr.verified_at.isoformat() if vr.verified_at else None,
    }


@bp.post("/loans/<uuid:loan_id>/verify")
@role_required("reviewer")
def verify_one_loan(loan_id):
    """Verify a single loan. Requires all blocking exceptions resolved."""
    reviewer_id = uuid.UUID(get_jwt_identity())
    try:
        vr = verify_loan(loan_id, reviewer_id)
    except ValueError:
        return _err("NOT_FOUND", "loan not found", 404)
    except RuntimeError as exc:
        return _err("NOT_ELIGIBLE", str(exc), 409)

    db.session.commit()

    loan = db.session.get(Loan, loan_id)
    return jsonify(_verified_summary(vr, loan.loan_id if loan else None)), 201


@bp.post("/verify-batch")
@role_required("reviewer")
def verify_batch():
    """Verify all eligible loans (no open blocking exceptions, not yet verified).

    Query params: limit (optional, default 0 = all).
    """
    reviewer_id = uuid.UUID(get_jwt_identity())
    limit = request.args.get("limit", 0, type=int)

    result = verify_eligible_loans(reviewer_id, limit=limit)
    db.session.commit()

    status = 200
    if result["errors"]:
        status = 207  # multi-status: some succeeded, some skipped

    return jsonify(result), status


@bp.get("/verified")
@role_required("operator", "reviewer", "consumer")
def list_verified():
    """List verified records, newest first, with optional filters."""
    q = db.select(VerifiedRecord)

    min_score = request.args.get("min_trust_score", type=float)
    if min_score is not None:
        q = q.filter(VerifiedRecord.trust_score >= min_score)

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(200, max(1, request.args.get("per_page", 50, type=int)))

    total = db.session.execute(
        db.select(sa.func.count()).select_from(q.subquery())
    ).scalar()

    rows = db.session.execute(
        q.order_by(VerifiedRecord.verified_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    ).scalars().all()

    loan_ids = {r.loan_id for r in rows}
    loan_map = {}
    if loan_ids:
        loan_map = dict(db.session.execute(
            db.select(Loan.id, Loan.loan_id).filter(Loan.id.in_(loan_ids))
        ).all())

    return jsonify({
        "verified_records": [
            _verified_summary(r, loan_map.get(r.loan_id))
            for r in rows
        ],
        "pagination": {
            "page": page, "per_page": per_page,
            "total": total, "pages": (total + per_page - 1) // per_page,
        },
    })


@bp.get("/verified/<uuid:loan_id>")
@role_required("operator", "reviewer", "consumer")
def get_verified(loan_id):
    """Get the latest verified record for a loan."""
    vr = db.session.execute(
        db.select(VerifiedRecord)
        .filter_by(loan_id=loan_id)
        .order_by(VerifiedRecord.version.desc())
        .limit(1)
    ).scalars().first()

    if vr is None:
        return _err("NOT_FOUND", "no verified record for this loan", 404)

    loan = db.session.get(Loan, loan_id)
    return jsonify(_verified_summary(vr, loan.loan_id if loan else None))


@bp.get("/verify")
@role_required("operator", "reviewer", "consumer")
def verify_chains():
    """Verify the integrity of the audit chain AND the verified-record chains.

    Returns both results so a single endpoint proves the whole immutability
    story: the audit chain is unforgeable, and every loan's verified-record
    chain is independently unforgeable.
    """
    audit_ok, audit_detail = verify_chain()
    vr_ok, vr_detail = verify_record_chain()

    return jsonify({
        "audit_chain": {"ok": audit_ok, "detail": audit_detail},
        "verified_record_chains": {"ok": vr_ok, "detail": vr_detail},
        "ok": audit_ok and vr_ok,
    })
