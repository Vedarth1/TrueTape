# backend/app/api/consumer.py
"""Module H — Data Consumer read-only API.

Endpoints a data consumer (or dashboard) calls to browse loans, inspect
verified records, and export data. All read-only; no mutation.

Routes:
  GET /api/loans              – paginated loan browser with filters
  GET /api/loans/<id>        – loan detail: canonical, sources, exceptions, verification
  GET /api/loans/<id>/timeline – chronological loan history
  GET /api/summary           – aggregate dashboard stats
  GET /api/export            – verified records as CSV download
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import sqlalchemy as sa
from flask import Blueprint, Response, jsonify, request

from app.auth.decorators import role_required
from app.extensions import db
from app.models import (
    ValidationResult,
    AuditEvent,
    ExceptionRecord,
    Loan,
    LoanCanonical,
    LoanRecord,
    VerifiedRecord,
)

bp = Blueprint("consumer", __name__, url_prefix="/api")


def _err(code, message, status, **details):
    return jsonify({"error": {"code": code, "message": message,
                              "details": details}}), status


# ------------------------------------------------------------------
# GET /api/loans
# ------------------------------------------------------------------
@bp.get("/loans")
@role_required("operator", "reviewer", "consumer")
def list_loans():
    """Paginated loan browser with filters.

    Query params:
      status          – one of ingested|in_review|verified|rejected
      borrower_id     – exact match (case-sensitive)
      search          – free-text search across loan_id and borrower_id
      min_trust       – minimum trust score (float)
      max_trust       – maximum trust score (float)
      has_exceptions  – "open" for loans with open exceptions, "none" for clean
      source_system   – loans that have at least one record from this source
      page            – default 1
      per_page        – default 50, max 200
    """
    q = db.select(Loan)

    status = request.args.get("status")
    if status:
        q = q.filter(Loan.status == status)

    borrower_id = request.args.get("borrower_id")
    if borrower_id:
        q = q.filter(Loan.borrower_id == borrower_id)

    search = request.args.get("search")
    if search:
        q = q.filter(
            sa.or_(
                Loan.loan_id.ilike(f"%{search}%"),
                Loan.borrower_id.ilike(f"%{search}%"),
            )
        )

    min_trust = request.args.get("min_trust", type=float)
    max_trust = request.args.get("max_trust", type=float)
    if min_trust is not None or max_trust is not None:
        vq = (
            db.select(VerifiedRecord.loan_id)
            .group_by(VerifiedRecord.loan_id)
        )
        if min_trust is not None:
            vq = vq.having(sa.func.max(VerifiedRecord.trust_score) >= min_trust)
        if max_trust is not None:
            vq = vq.having(sa.func.max(VerifiedRecord.trust_score) <= max_trust)
        q = q.filter(Loan.id.in_(vq))

    has_exc = request.args.get("has_exceptions")
    if has_exc == "open":
        q = q.filter(
            Loan.id.in_(
                db.select(ExceptionRecord.loan_id)
                .filter(ExceptionRecord.status == "open")
                .filter(ExceptionRecord.loan_id.isnot(None))
            )
        )
    elif has_exc == "none":
        q = q.filter(
            ~Loan.id.in_(
                db.select(ExceptionRecord.loan_id)
                .filter(ExceptionRecord.status == "open")
                .filter(ExceptionRecord.loan_id.isnot(None))
            )
        )

    source_system = request.args.get("source_system")
    if source_system:
        q = q.filter(
            Loan.id.in_(
                db.select(LoanRecord.loan_id)
                .filter(LoanRecord.source_system == source_system)
            )
        )

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(200, max(1, request.args.get("per_page", 50, type=int)))

    # Subquery for total (before pagination)
    count_q = db.select(sa.func.count()).select_from(q.subquery())
    total = db.session.execute(count_q).scalar()

    rows = db.session.execute(
        q.order_by(Loan.updated_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).scalars().all()

    # Batch-load: open exception count per loan
    loan_ids = [r.id for r in rows]
    exc_counts: dict = {}
    if loan_ids:
        exc_counts = dict(
            db.session.execute(
                db.select(
                    ExceptionRecord.loan_id,
                    sa.func.count(ExceptionRecord.id),
                )
                .filter(
                    ExceptionRecord.loan_id.in_(loan_ids),
                    ExceptionRecord.status == "open",
                )
                .group_by(ExceptionRecord.loan_id)
            ).all()
        )

    # Batch-load: latest verified record per loan (for trust score)
    trust_map: dict = {}
    if loan_ids:
        vr_rows = db.session.execute(
            db.text("""
                SELECT DISTINCT ON (loan_id)
                    loan_id, trust_score, version, verified_at
                FROM verified_records
                WHERE loan_id = ANY(:loan_ids)
                ORDER BY loan_id, version DESC
            """), {"loan_ids": list(loan_ids)}
        ).mappings().all()
        trust_map = {
            r["loan_id"]: {
                "trust_score": float(r["trust_score"]),
                "version": r["version"],
                "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None,
            }
            for r in vr_rows
        }

    # Batch-load: source systems per loan
    source_map: dict = {}
    if loan_ids:
        src_rows = db.session.execute(
            db.select(
                LoanRecord.loan_id,
                sa.func.array_agg(sa.distinct(LoanRecord.source_system)),
            )
            .filter(LoanRecord.loan_id.in_(loan_ids))
            .group_by(LoanRecord.loan_id)
        ).all()
        source_map = {lid: list(srcs) for lid, srcs in src_rows}

    return jsonify({
        "loans": [
            {
                "id": str(loan.id),
                "loan_id": loan.loan_id,
                "borrower_id": loan.borrower_id,
                "status": loan.status,
                "open_exceptions": exc_counts.get(loan.id, 0),
                "source_systems": source_map.get(loan.id, []),
                "updated_at": loan.updated_at.isoformat(),
                **(trust_map.get(loan.id) or {}),
            }
            for loan in rows
        ],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": (total + per_page - 1) // per_page if total else 0,
        },
    })


# ------------------------------------------------------------------
# GET /api/loans/<id>
# ------------------------------------------------------------------
@bp.get("/loans/<uuid:loan_id>")
@role_required("operator", "reviewer", "consumer")
def get_loan(loan_id):
    """Loan detail: canonical data, source records, exception summary,
    and verification status.
    """
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        return _err("NOT_FOUND", "loan not found", 404)

    # Canonical data
    canonical = db.session.get(LoanCanonical, loan_id)
    canonical_data = canonical.data if canonical else {}
    field_provenance = canonical.field_provenance if canonical else {}
    pinned_fields = (canonical.pinned_fields or {}) if canonical else {}

    # Source records (latest version per source)
    # Latest version per source system (Postgres DISTINCT ON)
    raw_recs = db.session.execute(
        db.text("""
            SELECT DISTINCT ON (source_system)
                id, source_system, version, data, field_errors,
                origin, effective_at, ingested_at
            FROM loan_records
            WHERE loan_id = :loan_id
            ORDER BY source_system, version DESC
        """), {"loan_id": loan_id}
    ).mappings().all()

    source_records = [
        {
            "source_system": r["source_system"],
            "version": r["version"],
            "data": r["data"] if isinstance(r["data"], dict) else {},
            "field_errors": r["field_errors"] if isinstance(r["field_errors"], dict) else {},
            "origin": r["origin"],
            "effective_at": r["effective_at"].isoformat() if r["effective_at"] else None,
            "ingested_at": r["ingested_at"].isoformat(),
        }
        for r in raw_recs
    ]

    # Exception summary
    exc_rows = db.session.execute(
        db.select(
            ExceptionRecord.exception_type,
            ExceptionRecord.severity,
            ExceptionRecord.status,
            sa.func.count(ExceptionRecord.id),
        )
        .filter(ExceptionRecord.loan_id == loan_id)
        .group_by(
            ExceptionRecord.exception_type,
            ExceptionRecord.severity,
            ExceptionRecord.status,
        )
    ).all()
    exceptions = [
        {"type": t, "severity": s, "status": st, "count": n}
        for t, s, st, n in exc_rows
    ]
    open_exc_count = sum(n for _, _, st, n in exc_rows if st == "open")

    # Latest verified record
    vr = db.session.execute(
        db.select(VerifiedRecord)
        .filter_by(loan_id=loan_id)
        .order_by(VerifiedRecord.version.desc())
        .limit(1)
    ).scalars().first()

    verification = None
    if vr:
        verification = {
            "version": vr.version,
            "trust_score": float(vr.trust_score),
            "trust_score_breakdown": vr.trust_score_breakdown,
            "validation_summary": vr.validation_summary,
            "source_files": vr.source_files,
            "record_hash": vr.record_hash,
            "prev_record_hash": vr.prev_record_hash,
            "verified_at": vr.verified_at.isoformat() if vr.verified_at else None,
            "canonical_data": vr.canonical_data,
            "field_provenance": vr.field_provenance,
        }

    # Validation pass rate for this loan
    v_pass = db.session.execute(
        db.select(
            sa.func.count().filter(ValidationResult.result == "pass"),
            sa.func.count().filter(ValidationResult.result == "fail"),
            sa.func.count().filter(ValidationResult.result == "not_applicable"),
        )
        .filter(ValidationResult.loan_id == loan_id)
    ).one()

    return jsonify({
        "id": str(loan.id),
        "loan_id": loan.loan_id,
        "borrower_id": loan.borrower_id,
        "status": loan.status,
        "updated_at": loan.updated_at.isoformat(),
        "canonical_data": canonical_data,
        "field_provenance": field_provenance,
        "pinned_fields": pinned_fields,
        "source_records": source_records,
        "exceptions": exceptions,
        "open_exceptions": open_exc_count,
        "validation": {
            "pass": v_pass[0],
            "fail": v_pass[1],
            "not_applicable": v_pass[2],
        },
        "verification": verification,
    })


# ------------------------------------------------------------------
# GET /api/loans/<id>/timeline
# ------------------------------------------------------------------
@bp.get("/loans/<uuid:loan_id>/timeline")
@role_required("operator", "reviewer", "consumer")
def loan_timeline(loan_id):
    """Chronological timeline for a single loan: records, exceptions,
    audit events, verification.
    """
    loan = db.session.get(Loan, loan_id)
    if loan is None:
        return _err("NOT_FOUND", "loan not found", 404)

    events = []

    # Source records
    rec_rows = db.session.execute(
        db.select(LoanRecord)
        .filter_by(loan_id=loan_id)
        .order_by(LoanRecord.ingested_at.asc(), LoanRecord.version.asc())
    ).scalars().all()
    for r in rec_rows:
        events.append({
            "type": "record_imported",
            "timestamp": r.ingested_at.isoformat(),
            "source_system": r.source_system,
            "version": r.version,
            "origin": r.origin,
            "fields_count": len(r.data or {}),
        })

    # Exceptions
    exc_rows = db.session.execute(
        db.select(ExceptionRecord)
        .filter(ExceptionRecord.loan_id == loan_id)
        .order_by(ExceptionRecord.created_at.asc())
    ).scalars().all()
    for e in exc_rows:
        events.append({
            "type": "exception",
            "timestamp": e.created_at.isoformat(),
            "exception_type": e.exception_type,
            "severity": e.severity,
            "status": e.status,
            "field_name": e.field_name,
        })

    # Audit events (for this loan)
    audit_rows = db.session.execute(
        db.select(AuditEvent)
        .filter(AuditEvent.loan_id == loan_id)
        .order_by(AuditEvent.seq.asc())
    ).scalars().all()
    for a in audit_rows:
        events.append({
            "type": "audit",
            "timestamp": a.created_at.isoformat(),
            "event_type": a.event_type,
            "actor_type": a.actor_type,
            "actor_id": str(a.actor_id) if a.actor_id else None,
            "sequence": a.seq,
        })

    # Verification
    vr_rows = db.session.execute(
        db.select(VerifiedRecord)
        .filter_by(loan_id=loan_id)
        .order_by(VerifiedRecord.verified_at.asc())
    ).scalars().all()
    for v in vr_rows:
        events.append({
            "type": "verified",
            "timestamp": v.verified_at.isoformat(),
            "version": v.version,
            "trust_score": float(v.trust_score),
            "verified_by": str(v.verified_by),
        })

    # Sort chronologically
    events.sort(key=lambda e: e["timestamp"])

    return jsonify({
        "loan_id": loan.loan_id,
        "events": events,
    })


# ------------------------------------------------------------------
# GET /api/summary
# ------------------------------------------------------------------
@bp.get("/summary")
@role_required("operator", "reviewer", "consumer")
def dashboard_summary():
    """Aggregate stats for the consumer dashboard.

    Returns loan counts by status, exception summary, verification
    metrics, and source coverage — everything a dashboard needs in one call.
    """
    # Loans by status
    status_rows = db.session.execute(
        db.select(Loan.status, sa.func.count(Loan.id))
        .group_by(Loan.status)
    ).all()
    loans_by_status = {s: c for s, c in status_rows}
    total_loans = sum(loans_by_status.values())

    # Exception summary (reuse logic from exceptions/stats but consumer-accessible)
    exc_rows = db.session.execute(
        db.select(
            ExceptionRecord.status,
            ExceptionRecord.severity,
            ExceptionRecord.exception_type,
            sa.func.count(ExceptionRecord.id),
        )
        .group_by(
            ExceptionRecord.status,
            ExceptionRecord.severity,
            ExceptionRecord.exception_type,
        )
    ).all()
    exc_by_status = {}
    exc_by_severity = {}
    exc_by_type = {}
    total_exceptions = 0
    for status, severity, exc_type, n in exc_rows:
        exc_by_status[status] = exc_by_status.get(status, 0) + n
        exc_by_severity[severity] = exc_by_severity.get(severity, 0) + n
        exc_by_type[exc_type] = exc_by_type.get(exc_type, 0) + n
        total_exceptions += n
    # Loans with open exceptions
    loans_with_open = db.session.execute(
        db.select(sa.func.count(sa.distinct(ExceptionRecord.loan_id)))
        .filter(ExceptionRecord.status == "open")
        .filter(ExceptionRecord.loan_id.isnot(None))
    ).scalar() or 0

    # Verification metrics
    vr_count = db.session.execute(
        db.select(sa.func.count(VerifiedRecord.id))
    ).scalar() or 0
    verified_loans = db.session.execute(
        db.select(sa.func.count(sa.distinct(VerifiedRecord.loan_id)))
    ).scalar() or 0
    avg_trust = db.session.execute(
        db.select(sa.func.avg(VerifiedRecord.trust_score))
    ).scalar()
    avg_trust = float(avg_trust) if avg_trust is not None else None
    trust_bucket_expr = sa.case(
        (VerifiedRecord.trust_score >= 90, "90-100"),
        (VerifiedRecord.trust_score >= 75, "75-89"),
        (VerifiedRecord.trust_score >= 50, "50-74"),
        else_="0-49",
    ).label("bucket")
    trust_buckets = db.session.execute(
        db.select(
            trust_bucket_expr,
            sa.func.count(VerifiedRecord.id),
        )
        .group_by(trust_bucket_expr)
    ).all()

    # Source coverage
    src_rows = db.session.execute(
        db.select(
            LoanRecord.source_system,
            sa.func.count(sa.distinct(LoanRecord.loan_id)),
        )
        .group_by(LoanRecord.source_system)
    ).all()
    source_coverage = {s: c for s, c in src_rows}

    # Validation results aggregate
    val_rows = db.session.execute(
        db.select(ValidationResult.result, sa.func.count(ValidationResult.id))
        .group_by(ValidationResult.result)
    ).all()
    validation_summary = {r: c for r, c in val_rows}

    return jsonify({
        "loans": {
            "total": total_loans,
            "by_status": loans_by_status,
        },
        "exceptions": {
            "total": total_exceptions,
            "by_status": exc_by_status,
            "by_severity": exc_by_severity,
            "by_type": exc_by_type,
            "loans_affected": loans_with_open,
        },
        "verification": {
            "verified_records": vr_count,
            "verified_loans": verified_loans,
            "avg_trust_score": avg_trust,
            "trust_distribution": {bucket: count for bucket, count in trust_buckets},
        },
        "sources": source_coverage,
        "validation": validation_summary,
    })


# ------------------------------------------------------------------
# GET /api/export
# ------------------------------------------------------------------
@bp.get("/export")
@role_required("operator", "reviewer", "consumer")
def export_verified():
    """Download verified records as CSV.

    Query params:
      min_trust  – filter by minimum trust score
      format     – "csv" (default) or "json"
    """
    import json as _json

    fmt = request.args.get("format", "csv")
    min_trust = request.args.get("min_trust", type=float)

    # Get latest verified record per loan via raw SQL (DISTINCT ON)
    trust_clause = "AND vr.trust_score >= :min_trust" if min_trust is not None else ""
    sql = db.text(f"""
        SELECT DISTINCT ON (vr.loan_id)
            vr.id, vr.loan_id, vr.version, vr.canonical_data,
            vr.field_provenance, vr.trust_score, vr.trust_score_breakdown,
            vr.validation_summary, vr.source_files, vr.record_hash,
            vr.verified_at, l.loan_id AS biz_loan_id, l.borrower_id
        FROM verified_records vr
        JOIN loans l ON l.id = vr.loan_id
        WHERE true {trust_clause}
        ORDER BY vr.loan_id, vr.version DESC
    """)
    params = {}
    if min_trust is not None:
        params["min_trust"] = min_trust

    raw_rows = db.session.execute(sql, params).mappings().all()

    if not raw_rows:
        if fmt == "json":
            return jsonify({"records": [], "exported_at": datetime.now(timezone.utc).isoformat()})
        return Response("", mimetype="text/csv",
                         headers={"Content-Disposition": "attachment; filename=verified_records.csv"})

    # Build list of dicts from raw rows
    records = []
    for r in raw_rows:
        canonical = r["canonical_data"] if isinstance(r["canonical_data"], dict) else _json.loads(r["canonical_data"])
        ts_breakdown = r["trust_score_breakdown"] if isinstance(r["trust_score_breakdown"], dict) else _json.loads(r["trust_score_breakdown"])
        val_summary = r["validation_summary"] if isinstance(r["validation_summary"], dict) else _json.loads(r["validation_summary"])
        src_files = r["source_files"] if isinstance(r["source_files"], list) else _json.loads(r["source_files"])
        records.append({
            "loan_id": r["biz_loan_id"],
            "borrower_id": r["borrower_id"],
            "trust_score": float(r["trust_score"]),
            "trust_score_breakdown": ts_breakdown,
            "validation_summary": val_summary,
            "source_files": src_files,
            "record_hash": r["record_hash"],
            "verified_at": r["verified_at"].isoformat() if r["verified_at"] else None,
            "canonical_data": canonical,
        })

    if fmt == "json":
        out = []
        for rec in records:
            flat = {k: v for k, v in rec.items() if k != "canonical_data"}
            for k, v in rec["canonical_data"].items():
                flat.setdefault(k, v)
            out.append(flat)
        return jsonify({
            "records": out,
            "count": len(out),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        })

    # CSV export
    # Fixed columns first; canonical keys that share a name with a fixed
    # column (loan_id, borrower_id) are skipped so the header stays unique.
    fixed = ["loan_id", "borrower_id", "trust_score", "verified_at", "record_hash"]
    fixed_set = set(fixed)
    all_fields: list[str] = []
    seen: set[str] = set(fixed_set)
    for rec in records:
        for k in rec["canonical_data"]:
            if k not in seen:
                all_fields.append(k)
                seen.add(k)

    header = fixed + all_fields

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        row = {
            "loan_id": rec["loan_id"],
            "borrower_id": rec["borrower_id"],
            "trust_score": rec["trust_score"],
            "verified_at": rec["verified_at"] or "",
            "record_hash": rec["record_hash"],
        }
        row.update(rec["canonical_data"])
        writer.writerow(row)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=verified_records.csv"
        },
    )
