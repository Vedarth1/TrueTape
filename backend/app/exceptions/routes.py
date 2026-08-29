# backend/app/exceptions/routes.py
"""Exception queue endpoints.

The exception queue is the reviewer's workspace: every validation failure
and source conflict surfaces here as an open exception, and the reviewer
resolves it with a decision that may correct canonical data.

Four endpoints, all JWT-protected:

    GET  /api/exceptions               -- paginated list with filters
    GET  /api/exceptions/<id>          -- detail (exception + canonical + AI recs)
    POST /api/exceptions/<id>/resolve  -- single-exception reviewer decision
    POST /api/exceptions/batch         -- batch resolve by cluster or rule_code

The resolve flow is the ONLY write path to loan_canonical.pinned_fields.
A reviewer's edit pins the corrected value so the next canonical blend
cannot revert it -- this is what makes "human-decided" a checkable claim
rather than a label.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity

from app.auth.decorators import role_required
from app.extensions import db
from app.models import (AiRecommendation, ExceptionCluster, ExceptionComment,
                        ExceptionRecord, Loan, LoanCanonical, LoanRecord,
                        ReviewerDecision, ValidationResult, ValidationRule)
from app.services.audit import log_event
from app.validation.runner import revalidate_record

bp = Blueprint("exceptions", __name__, url_prefix="/api/exceptions")

# Severity ordering for "highest severity" display; CRITICAL sorts first.
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message,
                              "details": details}}), status


def _exception_summary(exc: ExceptionRecord, loan_business_id: str | None = None,
                       rule_code: str | None = None) -> dict:
    detail = exc.detail or {}
    return {
        "id": str(exc.id),
        "loan_id": str(exc.loan_id) if exc.loan_id else None,
        "loan_business_id": loan_business_id,
        "exception_type": exc.exception_type,
        "rule_code": (rule_code or detail.get("rule_code")
                      or detail.get("check")),
        "field_name": exc.field_name,
        "severity": exc.severity,
        "is_blocking": exc.is_blocking,
        "status": exc.status,
        "cluster_id": str(exc.cluster_id) if exc.cluster_id else None,
        "message": detail.get("message"),
        "created_at": exc.created_at.isoformat() if exc.created_at else None,
    }


@bp.get("")
@role_required("operator", "reviewer")
def list_exceptions():
    """Paginated list with optional filters.

    Query params: status, severity, exception_type, loan_id (UUID),
    cluster_id (UUID), is_blocking (bool), page (default 1), per_page (default 50).
    """
    q = db.select(ExceptionRecord)

    status = request.args.get("status")
    if status:
        q = q.filter(ExceptionRecord.status == status)

    severity = request.args.get("severity")
    if severity:
        q = q.filter(ExceptionRecord.severity == severity)

    exc_type = request.args.get("exception_type")
    if exc_type:
        q = q.filter(ExceptionRecord.exception_type == exc_type)

    loan_id = request.args.get("loan_id")
    if loan_id:
        try:
            q = q.filter(ExceptionRecord.loan_id == uuid.UUID(loan_id))
        except ValueError:
            return _err("BAD_UUID", "loan_id is not a valid UUID", 400)

    cluster_id = request.args.get("cluster_id")
    if cluster_id:
        try:
            q = q.filter(ExceptionRecord.cluster_id == uuid.UUID(cluster_id))
        except ValueError:
            return _err("BAD_UUID", "cluster_id is not a valid UUID", 400)

    blocking = request.args.get("is_blocking")
    if blocking and blocking.lower() in ("true", "1", "yes"):
        q = q.filter(ExceptionRecord.is_blocking.is_(True))

    # Free-text search over the loan's business id and borrower id. Joins on
    # the FK, so quarantined rows (loan_id NULL, e.g. import errors) never
    # match -- they have no loan to search for.
    search = request.args.get("search")
    if search:
        pattern = f"%{search}%"
        q = q.join(Loan, Loan.id == ExceptionRecord.loan_id).filter(
            sa.or_(Loan.loan_id.ilike(pattern),
                   Loan.borrower_id.ilike(pattern)))

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(200, max(1, request.args.get("per_page", 50, type=int)))

    total = db.session.execute(
        db.select(sa.func.count()).select_from(q.subquery())
    ).scalar()

    rows = db.session.execute(
        q.order_by(ExceptionRecord.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    ).scalars().all()

    # Batch-fetch loan business ids and rule codes to avoid N+1 queries.
    loan_ids = {r.loan_id for r in rows if r.loan_id}
    rule_ids = {r.rule_id for r in rows if r.rule_id}

    loan_map = {}
    if loan_ids:
        loan_map = dict(db.session.execute(
            db.select(Loan.id, Loan.loan_id).filter(Loan.id.in_(loan_ids))
        ).all())

    rule_map = {}
    if rule_ids:
        rule_map = dict(db.session.execute(
            db.select(ValidationRule.id, ValidationRule.rule_code)
            .filter(ValidationRule.id.in_(rule_ids))
        ).all())

    return jsonify({
        "exceptions": [
            _exception_summary(
                r,
                loan_business_id=loan_map.get(r.loan_id),
                rule_code=rule_map.get(r.rule_id),
            )
            for r in rows
        ],
        "pagination": {
            "page": page, "per_page": per_page,
            "total": total, "pages": (total + per_page - 1) // per_page,
        },
    })


@bp.get("/<uuid:exc_id>")
@role_required("operator", "reviewer")
def get_exception(exc_id):
    """Full detail: the exception, its loan's canonical record, AI recs, comments."""
    exc = db.session.get(ExceptionRecord, exc_id)
    if exc is None:
        return _err("NOT_FOUND", "no such exception", 404)

    summary = _exception_summary(exc)

    # Loan business id
    if exc.loan_id:
        loan = db.session.get(Loan, exc.loan_id)
        if loan:
            summary["loan_business_id"] = loan.loan_id
            summary["loan_status"] = loan.status

    # Canonical record for this loan
    if exc.loan_id:
        canon = db.session.get(LoanCanonical, exc.loan_id)
        if canon:
            summary["canonical_data"] = canon.data
            summary["field_provenance"] = {
                k: v["source_system"] if isinstance(v, dict) else v
                for k, v in (canon.field_provenance or {}).items()
            }
            summary["pinned_fields"] = list((canon.pinned_fields or {}).keys())

    # Validation result that produced this exception (if it was a rule failure)
    if exc.rule_id and exc.loan_id:
        vresult = db.session.execute(
            db.select(ValidationResult)
            .filter_by(rule_id=exc.rule_id, loan_id=exc.loan_id)
            .order_by(ValidationResult.evaluated_at.desc()).limit(1)
        ).scalars().first()
        if vresult:
            summary["validation_result"] = {
                "result": vresult.result,
                "details": vresult.details,
                "evaluated_at": vresult.evaluated_at.isoformat()
                                if vresult.evaluated_at else None,
            }

    # AI recommendations on this exception
    ai_recs = db.session.execute(
        db.select(AiRecommendation)
        .filter_by(exception_id=exc_id)
        .order_by(AiRecommendation.created_at.desc())
    ).scalars().all()
    summary["ai_recommendations"] = [
        {
            "id": str(r.id),
            "action_type": r.action_type,
            "provider": r.provider,
            "model_name": r.model_name,
            "problem": r.problem,
            "reasoning": r.reasoning,
            "suggested_field": r.suggested_field,
            "suggested_value": r.suggested_value,
            "suggested_severity": r.suggested_severity,
            "note_text": r.note_text,
            "confidence": r.confidence,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in ai_recs
    ]

    # Comments
    comments = db.session.execute(
        db.select(ExceptionComment)
        .filter_by(exception_id=exc_id)
        .order_by(ExceptionComment.created_at.asc())
    ).scalars().all()
    summary["comments"] = [
        {
            "id": str(c.id),
            "body": c.body,
            "ai_drafted": c.ai_drafted,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in comments
    ]

    # Reviewer decision history -- every action ever taken on this exception,
    # oldest first, with the reviewer's name for the UI's action log.
    decisions = db.session.execute(
        db.select(ReviewerDecision)
        .filter_by(exception_id=exc_id)
        .order_by(ReviewerDecision.decided_at.asc())
    ).scalars().all()
    reviewer_ids = {d.reviewer_id for d in decisions}
    reviewer_names = {}
    if reviewer_ids:
        from app.models import User
        reviewer_names = dict(db.session.execute(
            db.select(User.id, User.name).filter(User.id.in_(reviewer_ids))
        ).all())
    summary["decision_history"] = [
        {
            "id": str(d.id),
            "action": d.action,
            "request_correction": bool(d.request_correction),
            "changes": d.changes or [],
            "agreed_with_ai": d.agreed_with_ai,
            "comment": d.comment,
            "reviewer": reviewer_names.get(d.reviewer_id, str(d.reviewer_id)),
            "decided_at": d.decided_at.isoformat() if d.decided_at else None,
        }
        for d in decisions
    ]

    # Source records for this loan (so the reviewer can see what each source said)
    if exc.loan_id:
        recs = db.session.execute(
            db.select(LoanRecord)
            .filter_by(loan_id=exc.loan_id)
            .order_by(LoanRecord.source_system, LoanRecord.version)
        ).scalars().all()
        summary["source_records"] = [
            {
                "source_system": r.source_system,
                "version": r.version,
                "origin": r.origin,
                "data": r.data,
                "field_errors": r.field_errors or {},
            }
            for r in recs
        ]

    return jsonify(summary)


@bp.post("/<uuid:exc_id>/resolve")
@role_required("reviewer")
def resolve_exception(exc_id):
    """Resolve a single exception with a reviewer decision.

    Body:
        action: "accept" | "edit" | "reject" | "manual_resolution"
        request_correction: optional bool, reject only -- bounces the loan
                            back to "in_review" and audits correction_requested
        changes: [{"field": ..., "before": ..., "after": ..., "source_used": ...}]
                  required for edit/manual_resolution, ignored otherwise
        comment: optional string
        agreed_with_ai: optional bool (auto-derived from ai_recommendation_id if omitted)
        ai_recommendation_id: optional UUID

    The decision writes to loan_canonical.pinned_fields when changes are made,
    so the corrected values survive the next canonical blend. The exception's
    status moves to 'resolved' (accept/edit/manual_resolution) or 'rejected'.
    """
    exc = db.session.get(ExceptionRecord, exc_id)
    if exc is None:
        return _err("NOT_FOUND", "no such exception", 404)
    if exc.status in ("resolved", "rejected"):
        return _err("ALREADY_RESOLVED",
                    f"exception is already {exc.status}", 409)

    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("accept", "edit", "reject", "manual_resolution"):
        return _err("BAD_ACTION",
                    "action must be accept, edit, reject, or manual_resolution",
                    400)

    # A reject can send the loan back to the operator for correction. Only
    # meaningful with action="reject"; ignored otherwise.
    request_correction = bool(body.get("request_correction"))
    if request_correction and action != "reject":
        return _err("BAD_REQUEST_CORRECTION",
                    "request_correction is only valid with action='reject'", 400)

    changes = body.get("changes") or []
    if action in ("edit", "manual_resolution"):
        if not changes or not isinstance(changes, list):
            return _err("NO_CHANGES",
                        "edit/manual_resolution requires a non-empty changes list",
                        400)
        for ch in changes:
            if not isinstance(ch, dict) or "field" not in ch or "after" not in ch:
                return _err("BAD_CHANGE",
                            "each change needs at least {field, after}", 400)

    reviewer_id = uuid.UUID(get_jwt_identity())

    # Derive agreed_with_ai: if the reviewer cited an AI rec and didn't
    # explicitly disagree, they agreed.
    ai_rec_id = None
    if body.get("ai_recommendation_id"):
        try:
            ai_rec_id = uuid.UUID(body["ai_recommendation_id"])
        except ValueError:
            return _err("BAD_UUID", "ai_recommendation_id is not a valid UUID", 400)

    agreed = body.get("agreed_with_ai")
    if agreed is None and ai_rec_id is not None and action in ("accept", "edit"):
        agreed = True

    # Build the decision row
    decision = ReviewerDecision(
        id=uuid.uuid4(),
        exception_id=exc_id,
        loan_id=exc.loan_id,
        decision_scope="exception",
        ai_recommendation_id=ai_rec_id,
        reviewer_id=reviewer_id,
        action=action,
        request_correction=request_correction,
        changes=changes,
        agreed_with_ai=agreed,
        comment=body.get("comment"),
    )
    db.session.add(decision)

    # Apply the reviewer's corrections. ONLY edit / manual_resolution may touch
    # canonical data: a reject that happens to carry a changes array must not
    # mutate the record it is rejecting (the old `if changes and exc.loan_id`
    # gate let it through). accept/reject fall straight past this block.
    if action in ("edit", "manual_resolution") and changes and exc.loan_id:
        canon = db.session.get(LoanCanonical, exc.loan_id)
        if canon is None:
            return _err("NO_CANONICAL",
                        "loan has no canonical record to edit", 409)

        # Read the 'before' values server-side from canonical, NOT from the
        # client payload. The audit trail must attest to what the system
        # actually held, not to what the caller claimed it held -- otherwise a
        # reviewer (or a bug) could write a fictional before-value into the
        # hash chain.
        before_values = {ch["field"]: (canon.data or {}).get(ch["field"])
                         for ch in changes}
        after_values = {ch["field"]: ch["after"] for ch in changes}

        # Pin the corrected value AND reflect it in canonical.data/provenance
        # immediately, mirroring exactly what build_canonical does when it
        # carries a pinned field forward. Doing it inline keeps the detail
        # endpoint consistent without re-blending all 1,200 loans (which would
        # also emit its own run-level audit event).
        pinned = dict(canon.pinned_fields or {})
        data = dict(canon.data or {})
        provenance = dict(canon.field_provenance or {})
        for field, value in after_values.items():
            pinned[field] = value
            data[field] = value
            provenance[field] = {
                "source_system": "human_override",
                "trust_score": 100,
                "pinned": True,
            }
        canon.pinned_fields = pinned
        canon.data = data
        canon.field_provenance = provenance
        canon.computed_at = datetime.now(timezone.utc)

        # Append-only lineage: a human edit INSERTs a new revision rather than
        # mutating a source row, so "what did the reviewer change, and when" is
        # a one-line query. version is per-(loan, source) and part of the
        # revision_unique_per_source key, so a second edit becomes v2, v3, ...
        max_ver = db.session.execute(
            db.select(sa.func.max(LoanRecord.version)).filter(
                LoanRecord.loan_id == exc.loan_id,
                LoanRecord.source_system == "human_override")
        ).scalar()
        override_record = LoanRecord(
            id=uuid.uuid4(),
            loan_id=exc.loan_id,
            raw_record_id=None,
            source_system="human_override",
            version=(max_ver or 0) + 1,
            data=after_values,
            origin="human_edit",
            effective_at=datetime.now(timezone.utc),
        )
        db.session.add(override_record)
        db.session.flush()   # materialize the FK before revalidate references it

        # Validate the reviewer's own value the same way an import is validated.
        # This is what makes the edit flow honest: a manual correction that is
        # itself invalid opens a new exception instead of being trusted.
        revalidate_record(override_record, now=datetime.now(timezone.utc))

        log_event(
            event_type="field_edited",
            entity_type="loan_canonical",
            entity_id=exc.loan_id,
            loan_id=exc.loan_id,
            actor_id=reviewer_id,
            actor_type="human",
            before_value=before_values,
            after_value=after_values,
            reason=f"reviewer decision: {action} on exception {exc_id}",
        )

    # Update exception status
    old_status = exc.status
    if action == "reject":
        exc.status = "rejected"
    else:
        exc.status = "resolved"
    exc.resolved_at = datetime.now(timezone.utc)

    # A rejection can bounce the loan back to the operator's desk. The loan
    # status change is the operator-side signal; the correction_requested
    # audit event is the permanent record of who asked and why.
    if request_correction and exc.loan_id:
        loan = db.session.get(Loan, exc.loan_id)
        if loan is not None and loan.status != "in_review":
            loan.status = "in_review"
        log_event(
            event_type="correction_requested",
            entity_type="loan",
            entity_id=exc.loan_id,
            loan_id=exc.loan_id,
            actor_id=reviewer_id,
            actor_type="human",
            before_value={"loan_status": loan.status if loan else None,
                          "exception_status": old_status},
            after_value={"loan_status": "in_review",
                         "exception_status": exc.status},
            reason=body.get("comment") or f"correction requested on exception {exc_id}",
        )

    # Comment if provided
    if body.get("comment"):
        comment = ExceptionComment(
            id=uuid.uuid4(),
            exception_id=exc_id,
            author_id=reviewer_id,
            body=body["comment"],
            ai_drafted=False,
        )
        db.session.add(comment)
        log_event(
            event_type="reviewer_comment_added",
            entity_type="exception",
            entity_id=exc_id,
            loan_id=exc.loan_id,
            actor_id=reviewer_id,
            actor_type="human",
            after_value={"body": body["comment"]},
            reason="comment on resolve",
        )

    # Audit the decision itself
    log_event(
        event_type="loan_approved" if action in ("accept", "edit") else "loan_rejected",
        entity_type="exception",
        entity_id=exc_id,
        loan_id=exc.loan_id,
        actor_id=reviewer_id,
        actor_type="human",
        after_value={
            "action": action,
            "old_status": old_status,
            "new_status": exc.status,
            "changes_count": len(changes),
            "agreed_with_ai": agreed,
        },
        reason=f"reviewer resolved exception: {action}",
    )

    db.session.commit()
    return jsonify({
        "id": str(exc_id),
        "status": exc.status,
        "action": action,
        "decision_id": str(decision.id),
        "changes_applied": len(changes),
    })


@bp.post("/batch")
@role_required("reviewer")
def batch_resolve():
    """Resolve many exceptions in one request.

    Body:
        exception_ids: [uuid, ...]     -- explicit list
        OR
        filter: {rule_code, exception_type, severity}  -- select by criteria
        action: "accept" | "reject"  (no edit in batch -- edits need per-exception review)
        comment: optional string

    Each resolved exception gets its own ReviewerDecision with a shared
    batch_id, so a 40-row bulk action stays attributable row by row.
    """
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    if action not in ("accept", "reject"):
        return _err("BAD_ACTION",
                    "batch action must be accept or reject", 400)

    reviewer_id = uuid.UUID(get_jwt_identity())
    batch_id = uuid.uuid4()

    # Build the exception query
    q = db.select(ExceptionRecord).filter(ExceptionRecord.status == "open")

    exc_ids = body.get("exception_ids")
    filt = body.get("filter") or {}

    if exc_ids:
        try:
            id_set = {uuid.UUID(x) for x in exc_ids}
        except (ValueError, TypeError):
            return _err("BAD_UUID", "one or more exception_ids are not valid UUIDs", 400)
        q = q.filter(ExceptionRecord.id.in_(id_set))
    elif filt:
        if filt.get("rule_code"):
            q = q.join(ValidationRule,
                       ExceptionRecord.rule_id == ValidationRule.id).filter(
                           ValidationRule.rule_code == filt["rule_code"])
        if filt.get("exception_type"):
            q = q.filter(ExceptionRecord.exception_type == filt["exception_type"])
        if filt.get("severity"):
            q = q.filter(ExceptionRecord.severity == filt["severity"])
    else:
        return _err("NO_SELECTION",
                    "provide exception_ids or a filter to select exceptions", 400)

    exceptions = db.session.execute(q.order_by(ExceptionRecord.created_at)).scalars().all()
    if not exceptions:
        return _err("NO_MATCH", "no open exceptions matched the selection", 404)

    decisions = []
    now = datetime.now(timezone.utc)
    for exc in exceptions:
        exc.status = "resolved" if action == "accept" else "rejected"
        exc.resolved_at = now
        decisions.append(ReviewerDecision(
            id=uuid.uuid4(),
            exception_id=exc.id,
            loan_id=exc.loan_id,
            decision_scope="exception",
            reviewer_id=reviewer_id,
            action=action,
            changes=[],
            agreed_with_ai=None,
            comment=body.get("comment"),
            batch_id=batch_id,
        ))

    db.session.add_all(decisions)

    # Attribute the batch to each affected loan. The old single batch-level
    # event carried no loan_id, so a per-loan audit query (`WHERE loan_id = X`)
    # never surfaced batch resolutions. Emitting one event per loan -- all
    # sharing entity_id=batch_id -- keeps the action queryable BOTH ways: by
    # batch (entity_id) and by loan (loan_id). Per-loan counts sum back to
    # len(exceptions).
    per_loan: dict = {}
    for exc in exceptions:
        per_loan[exc.loan_id] = per_loan.get(exc.loan_id, 0) + 1

    for lid, count in per_loan.items():
        log_event(
            event_type="loan_approved" if action == "accept" else "loan_rejected",
            entity_type="batch",
            entity_id=batch_id,
            loan_id=lid,
            actor_id=reviewer_id,
            actor_type="human",
            after_value={
                "action": action,
                "exceptions_resolved": count,
                "batch_id": str(batch_id),
            },
            reason=f"batch resolve: {action} {count} exception(s) on this loan",
        )

    db.session.commit()
    return jsonify({
        "batch_id": str(batch_id),
        "action": action,
        "exceptions_resolved": len(exceptions),
    })


@bp.get("/clusters")
@role_required("operator", "reviewer")
def list_clusters():
    """Cluster-first view: root-cause groups with open member counts.

    The queue UI shows these cards first; expanding a card filters the
    exception list to cluster_id. member_count covers OPEN members only --
    resolved rows shrink the card without a re-clustering pass.
    """
    rows = db.session.execute(
        db.select(
            ExceptionCluster.id,
            ExceptionCluster.cluster_label,
            ExceptionCluster.root_cause_signal,
            ExceptionCluster.member_count,
            sa.func.count(ExceptionRecord.id).filter(
                ExceptionRecord.status == "open"),
            sa.func.min(ExceptionRecord.severity),
            sa.func.count(sa.distinct(ExceptionRecord.loan_id)),
        )
        .outerjoin(ExceptionRecord, ExceptionRecord.cluster_id == ExceptionCluster.id)
        .group_by(
            ExceptionCluster.id,
            ExceptionCluster.cluster_label,
            ExceptionCluster.root_cause_signal,
            ExceptionCluster.member_count,
        )
        .order_by(sa.func.count(ExceptionRecord.id).filter(
            ExceptionRecord.status == "open").desc())
    ).all()

    # Severity breakdown per cluster for the card's colour coding.
    sev_rows = db.session.execute(
        db.select(
            ExceptionRecord.cluster_id,
            ExceptionRecord.severity,
            sa.func.count(ExceptionRecord.id),
        )
        .filter(ExceptionRecord.status == "open",
                ExceptionRecord.cluster_id.isnot(None))
        .group_by(ExceptionRecord.cluster_id, ExceptionRecord.severity)
    ).all()
    sev_by_cluster: dict = {}
    for cluster_id, severity, n in sev_rows:
        sev_by_cluster.setdefault(cluster_id, {})[severity] = n

    return jsonify({"clusters": [
        {
            "id": str(cid),
            "cluster_label": label,
            "root_cause_signal": signal,
            "member_count": member_count,
            "open_count": open_n,
            "loans_affected": loans_n,
            "highest_severity": min(
                sev_by_cluster.get(cid, {}), key=SEV_ORDER.get, default=None),
            "severity_breakdown": sev_by_cluster.get(cid, {}),
        }
        for cid, label, signal, member_count, open_n, min_sev, loans_n in rows
    ]})


@bp.get("/stats")
@role_required("operator", "reviewer")
def exception_stats():
    """Summary counts for the dashboard: by status, severity, type."""
    rows = db.session.execute(
        db.select(
            ExceptionRecord.status,
            ExceptionRecord.severity,
            ExceptionRecord.exception_type,
            sa.func.count(ExceptionRecord.id),
        ).group_by(
            ExceptionRecord.status,
            ExceptionRecord.severity,
            ExceptionRecord.exception_type,
        )
    ).all()

    by_status = {}
    by_severity = {}
    by_type = {}
    total = 0
    for status, severity, exc_type, n in rows:
        by_status[status] = by_status.get(status, 0) + n
        by_severity[severity] = by_severity.get(severity, 0) + n
        by_type[exc_type] = by_type.get(exc_type, 0) + n
        total += n

    return jsonify({
        "total": total,
        "by_status": by_status,
        "by_severity": by_severity,
        "by_type": by_type,
    })
