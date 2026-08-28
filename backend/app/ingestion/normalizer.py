# backend/app/ingestion/normalizer.py
"""Stage 2 — normalization.

Maps each cleanly-parsed raw_record into a typed, canonical loan_records row
(find-or-create its loan by natural loan_id), or quarantines rows with no
identity (missing loan_id) as import_error exceptions.

Emits NO audit events: Stage 2 is deliberately silent (arch §), and
`record_imported` is logged once per file by the Stage-1 worker in service.py.
Follows the service convention: does NOT commit — the caller commits once.
"""
import uuid

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import RawFile, RawRecord, Loan, LoanRecord, ExceptionRecord
from app.ingestion.aliases import CANONICAL_FIELD_TYPES, resolve_header, coerce_value


def _build_column_mapping(rows):
    """Union of raw headers across the file -> ({header: canonical|None}, [unmapped])."""
    ordered, seen = [], set()
    for r in rows:
        for h in (r.raw_payload or {}):
            if h not in seen:
                seen.add(h)
                ordered.append(h)
    mapping = {h: resolve_header(h) for h in ordered}
    unmapped = [h for h, tgt in mapping.items() if tgt is None]
    return mapping, unmapped


def _normalize_row(raw_payload):
    """raw dict -> (data, field_errors, effective_at)."""
    data, field_errors, effective_at = {}, {}, None
    for header, value in (raw_payload or {}).items():
        field = resolve_header(header)
        if field is None:
            continue                      # unmapped: surfaced at file level, not dropped silently
        kind, out = coerce_value(field, value)
        if kind == "absent":
            continue                      # blank/missing: field stays absent
        if kind == "error":
            field_errors[field] = {"raw": out, "expected": CANONICAL_FIELD_TYPES[field]}
        elif kind == "ts":
            data[field] = out.isoformat()  # JSON-safe copy in data
            if field == "last_updated_at":
                effective_at = out         # real datetime -> loan_records.effective_at
        else:                              # "value"
            data[field] = out
    return data, field_errors, effective_at


def _get_or_create_loan(loan_id_text, borrower_id):
    """Find-or-create by natural loan_id; race-safe against a concurrent file."""
    loan = db.session.execute(
        db.select(Loan).filter_by(loan_id=loan_id_text)
    ).scalar_one_or_none()
    if loan is not None:
        return loan
    loan = Loan(id=uuid.uuid4(), loan_id=loan_id_text, borrower_id=borrower_id)
    db.session.add(loan)
    try:
        with db.session.begin_nested():   # SAVEPOINT absorbs a UNIQUE(loan_id) race
            db.session.flush()
    except IntegrityError:
        loan = db.session.execute(
            db.select(Loan).filter_by(loan_id=loan_id_text)
        ).scalar_one()
    return loan


def _next_version(counters, loan_pk, source_system):
    """Per-(loan, source) version sequence, seeded from the DB max then bumped
    in memory — so duplicate loan_ids *within one file* (the DUPLICATE_LOAN_ID
    defect) get distinct versions without a per-row flush and without tripping
    UNIQUE(loan_id, source_system, version)."""
    key = (loan_pk, source_system)
    if key not in counters:
        base = db.session.execute(
            db.select(func.max(LoanRecord.version))
            .filter_by(loan_id=loan_pk, source_system=source_system)
        ).scalar()
        counters[key] = base or 0
    counters[key] += 1
    return counters[key]


def normalize_file(raw_file_id):
    """Normalize every cleanly-parsed row of a file. Does NOT commit; returns counts."""
    rf = db.session.get(RawFile, raw_file_id)
    if rf is None:
        raise ValueError(f"raw_file {raw_file_id} not found")

    # Idempotency: a file is normalized exactly once. The Stage-1 worker already
    # calls this after parse, so a manual/second call must be a no-op — otherwise
    # it appends duplicate version-2 records for every loan. (A force re-upload
    # makes a NEW raw_file with a new id, which normalizes normally.)
    existing_lr = db.session.execute(
        db.select(func.count(LoanRecord.id))
        .join(RawRecord, LoanRecord.raw_record_id == RawRecord.id)
        .filter(RawRecord.raw_file_id == raw_file_id)
    ).scalar()

    existing_ex = db.session.execute(
        db.select(func.count(ExceptionRecord.id)).filter(
            ExceptionRecord.raw_file_id == raw_file_id,
            ExceptionRecord.exception_type == "import_error",
        )
    ).scalar()

    if existing_lr or existing_ex:
        return {"loan_records_created": 0, "quarantined_count": 0,
                "unmapped_columns": rf.unmapped_columns or [], "skipped": "already_normalized"}

    rows = db.session.execute(
        db.select(RawRecord)
        .filter(RawRecord.raw_file_id == raw_file_id)
        .filter(RawRecord.parse_error.is_(None))   # structural failures already surfaced as failed_rows
        .order_by(RawRecord.row_number)
    ).scalars().all()

    mapping, unmapped = _build_column_mapping(rows)
    rf.column_mapping = mapping
    rf.unmapped_columns = unmapped

    counters, created, quarantined = {}, 0, 0
    for rr in rows:
        data, field_errors, effective_at = _normalize_row(rr.raw_payload)
        loan_id_text = data.get("loan_id")

        if not loan_id_text:                          # no identity -> quarantine, never a loan
            db.session.add(ExceptionRecord(
                id=uuid.uuid4(),
                loan_id=None,
                raw_record_id=rr.id,
                raw_file_id=rf.id,
                exception_type="import_error",
                field_name="loan_id",
                severity="HIGH",
                is_blocking=True,
                status="open",
                detail={"reason": "missing loan_id", "raw_payload": rr.raw_payload},
            ))
            quarantined += 1
            continue

        loan = _get_or_create_loan(loan_id_text, data.get("borrower_id"))
        db.session.add(LoanRecord(
            id=uuid.uuid4(),
            loan_id=loan.id,
            raw_record_id=rr.id,
            source_system=rf.source_system,
            version=_next_version(counters, loan.id, rf.source_system),
            data=data,
            field_errors=field_errors or None,
            effective_at=effective_at,
            origin="import",
        ))
        created += 1

    return {"loan_records_created": created,
            "quarantined_count": quarantined,
            "unmapped_columns": unmapped}