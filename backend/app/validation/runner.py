# backend/app/validation/runner.py
"""Stage 3 -- row-scope validation.

Evaluates every active scope='row' rule against every loan_record, writing one
validation_results row per (record, rule) and one exceptions row per fail.
Follows the ingestion convention: does NOT commit -- the caller commits once.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import sqlalchemy as sa

from app.extensions import db
from app.models import (ExceptionRecord, LoanRecord, RawFile, RawRecord,
                        ValidationResult, ValidationRule)
from app.services.audit import log_event
from app.validation.dsl import EvalContext, _to_naive_utc, evaluate, render_message

# HIGH/CRITICAL gate loan approval; LOW/MEDIUM are surfaced but non-blocking.
# The column defaults to TRUE (safe-by-default for a new exception type), so
# this is a deliberate downgrade: a LOW "unrecognized payment_status" should not
# be able to freeze a loan that is otherwise clean.
BLOCKING_SEVERITIES = ("HIGH", "CRITICAL")


def _build_scope_map():
    """raw_record_id -> frozenset(canonical fields the file declared a column for).

    Read off raw_files.column_mapping ({header: canonical|None}), already
    populated by normalizer._build_column_mapping, rather than hard-coded per
    source_system -- so a new file shape needs no code change here. Two
    queries, not one per row.
    """
    file_fields = {
        fid: frozenset(v for v in (mapping or {}).values() if v)
        for fid, mapping in db.session.execute(
            db.select(RawFile.id, RawFile.column_mapping)).all()
    }
    return {
        rr_id: file_fields.get(rf_id, frozenset())
        for rr_id, rf_id in db.session.execute(
            db.select(RawRecord.id, RawRecord.raw_file_id)).all()
    }


def _existing_count(rule_ids):
    """Results already written by a per-record pass.

    Scoped to loan_record_id IS NOT NULL on purpose: B3 will run these same
    rules against loan_canonical with loan_record_id NULL, and a B1 re-run must
    not see -- or later delete -- those.
    """
    if not rule_ids:
        return 0
    return db.session.execute(
        db.select(sa.func.count(ValidationResult.id)).filter(
            ValidationResult.rule_id.in_(rule_ids),
            ValidationResult.loan_record_id.isnot(None))
    ).scalar()


def _clear_previous(rule_ids):
    """Clear a previous row-scope run on a forced re-run.

    Reviewer decisions are history, not obstacles: only OPEN exceptions are
    deleted, and the (rule, raw_record) pairs a reviewer already resolved or
    rejected are returned so the creation pass can skip them -- re-running
    must not reopen a decided defect as a duplicate open exception.
    """
    decided = {
        (rule_id, raw_record_id)
        for rule_id, raw_record_id in db.session.execute(
            db.select(ExceptionRecord.rule_id, ExceptionRecord.raw_record_id)
            .filter(ExceptionRecord.exception_type == "validation_failure",
                    ExceptionRecord.rule_id.in_(rule_ids),
                    ExceptionRecord.raw_record_id.isnot(None),
                    ExceptionRecord.status != "open"))
    }
    db.session.execute(sa.delete(ExceptionRecord).filter(
        ExceptionRecord.exception_type == "validation_failure",
        ExceptionRecord.rule_id.in_(rule_ids),
        ExceptionRecord.raw_record_id.isnot(None),
        ExceptionRecord.status == "open"))
    db.session.execute(sa.delete(ValidationResult).filter(
        ValidationResult.rule_id.in_(rule_ids),
        ValidationResult.loan_record_id.isnot(None)))
    return decided


def run_row_validation(force=False, now=None):
    """Run all active row-scope rules over every loan_record. Does NOT commit."""
    rules = db.session.execute(
        db.select(ValidationRule)
        .filter_by(scope="row", is_active=True)
        .order_by(ValidationRule.rule_code)
    ).scalars().all()
    if not rules:
        raise RuntimeError(
            "no active scope='row' rules -- seed data/seed/validation_rules.json first")

    rule_ids = [r.id for r in rules]
    existing = _existing_count(rule_ids)
    if existing and not force:
        return {"skipped": "already_validated", "existing_results": existing}

    now = now or datetime.now(timezone.utc)
    scope_map = _build_scope_map()
    decided_pairs = _clear_previous(rule_ids) if existing else set()

    # Every version, including the 5 duplicate version-2 rows: those are real
    # rows in the file, and B2's DUPLICATE_LOAN_ID depends on seeing them.
    records = db.session.execute(
        db.select(LoanRecord).order_by(LoanRecord.source_system,
                                       LoanRecord.version)
    ).scalars().all()

    result_rows, exceptions = [], []
    skipped_decided = 0
    tally = {"pass": 0, "fail": 0, "not_applicable": 0}

    for rec in records:
        if rec.raw_record_id:
            in_scope = scope_map.get(rec.raw_record_id, frozenset())
        else:
            # A human_edit record has no raw row; its declared fields are
            # simply whatever it carries.
            in_scope = frozenset(rec.data or {}) | frozenset(rec.field_errors or {})

        for rule in rules:
            ctx = EvalContext(rec.data, rec.field_errors, in_scope, now,
                              source_system=rec.source_system)
            result, details = evaluate(rule.condition, ctx)
            tally[result] += 1

            result_rows.append({
                "id": uuid.uuid4(),
                "loan_record_id": rec.id,
                "loan_id": rec.loan_id,
                "rule_id": rule.id,
                "rule_version": rule.version,
                "result": result,
                "details": details,
            })

            if result != "fail":
                continue

            # A reviewer already decided this exact defect; keep the result
            # row (it is the objective fact) but do not reopen the exception.
            if (rule.id, rec.raw_record_id) in decided_pairs:
                skipped_decided += 1
                continue

            refd = details["referenced_fields"]
            exceptions.append(ExceptionRecord(
                id=uuid.uuid4(),
                loan_id=rec.loan_id,
                raw_record_id=rec.raw_record_id,
                exception_type="validation_failure",
                rule_id=rule.id,
                # Only when the rule blamed exactly one field; a multi-field
                # rule has no single honest answer here.
                field_name=refd[0] if len(refd) == 1 else None,
                severity=rule.severity,
                is_blocking=rule.severity in BLOCKING_SEVERITIES,
                status="open",
                detail={
                    "rule_code": rule.rule_code,
                    "rule_version": rule.version,
                    "source_system": rec.source_system,
                    "record_version": rec.version,
                    "message": render_message(rule.message_template,
                                              details["values"]),
                    **details,
                },
            ))

    if result_rows:
        # Core executemany: ~38k ORM objects would be needlessly slow and hold
        # 38k identities in the session for no benefit.
        db.session.execute(sa.insert(ValidationResult), result_rows)
    db.session.add_all(exceptions)   # ORM, so version_id_col behaves

    # One run-level event, not one per exception: each audit append is a
    # sequential hash under pg_advisory_xact_lock, and the exception row already
    # carries rule_id + loan_id + created_at. Per-exception events are two
    # extra lines if the traceability demo wants them.
    log_event(
        event_type="validation_executed",
        entity_type="validation_run",
        actor_type="system",
        after_value={
            "scope": "row",
            "rules": len(rules),
            "records": len(records),
            "results": len(result_rows),
            "passed": tally["pass"],
            "failed": tally["fail"],
            "not_applicable": tally["not_applicable"],
            "exceptions_created": len(exceptions),
        },
        reason="row-scope validation run",
    )

    return {"rules": len(rules), "records": len(records),
            "results_written": len(result_rows), **tally,
            "exceptions_created": len(exceptions),
            "decisions_preserved": len(decided_pairs),
            "decided_skipped": skipped_decided}


def revalidate_record(record, now=None):
    """Re-run active row-scope rules against ONE loan_record. Does NOT commit.

    Used after a human edit inserts a `human_override` record: the reviewer's
    chosen value is validated exactly the way an imported value would be, so a
    bad manual edit (a negative balance, a maturity before origination) is
    caught and surfaced as a fresh exception rather than silently trusted. This
    is the integrity half of "re-run validation on edit" -- the half that has
    teeth, because the original source rows are immutable and their own
    results/exceptions cannot change.

    Deliberately scoped to a single record: it writes validation_results for
    THIS record only and never deletes anything, so it cannot trip the
    reviewer-touched idempotency guards in _clear_previous and cannot duplicate
    the untouched exceptions already sitting on the original source rows.
    """
    rules = db.session.execute(
        db.select(ValidationRule)
        .filter_by(scope="row", is_active=True)
        .order_by(ValidationRule.rule_code)
    ).scalars().all()
    now = now or datetime.now(timezone.utc)

    # A human_override record has no raw row, so its declared fields are simply
    # whatever it carries -- the same branch run_row_validation takes for a
    # human_edit record. A field the override does not carry stays ABSENT and
    # its rules resolve to not_applicable, so a sparse edit cannot spuriously
    # fail unrelated cross-field rules.
    if record.raw_record_id:
        in_scope = _build_scope_map().get(record.raw_record_id, frozenset())
    else:
        in_scope = (frozenset(record.data or {})
                    | frozenset(record.field_errors or {}))

    result_rows, exceptions = [], []
    tally = {"pass": 0, "fail": 0, "not_applicable": 0}

    for rule in rules:
        ctx = EvalContext(record.data, record.field_errors, in_scope, now,
                          source_system=record.source_system)
        result, details = evaluate(rule.condition, ctx)
        tally[result] += 1

        result_rows.append({
            "id": uuid.uuid4(),
            "loan_record_id": record.id,
            "loan_id": record.loan_id,
            "rule_id": rule.id,
            "rule_version": rule.version,
            "result": result,
            "details": details,
        })

        if result != "fail":
            continue

        refd = details["referenced_fields"]
        exceptions.append(ExceptionRecord(
            id=uuid.uuid4(),
            loan_id=record.loan_id,
            raw_record_id=None,
            exception_type="validation_failure",
            rule_id=rule.id,
            field_name=refd[0] if len(refd) == 1 else None,
            severity=rule.severity,
            is_blocking=rule.severity in BLOCKING_SEVERITIES,
            status="open",
            detail={
                "rule_code": rule.rule_code,
                "rule_version": rule.version,
                "source_system": record.source_system,
                "record_version": record.version,
                "message": render_message(rule.message_template,
                                          details["values"]),
                "origin": "human_edit_revalidation",
                **details,
            },
        ))

    if result_rows:
        db.session.execute(sa.insert(ValidationResult), result_rows)
    db.session.add_all(exceptions)

    return {"record_id": str(record.id), **tally,
            "exceptions_created": len(exceptions)}


# ---------------------------------------------------------------------------
# Stage 3b -- dataset-scope validation (B2)
#
# Row rules answer "is this record well-formed?"  Dataset rules answer "does
# this SET of records contain a structural defect?" -- a duplicate loan_id, a
# cloned borrower under a fresh id, a burst of originations that smells like
# round-tripping.  The verdict is still per-record (one validation_results row
# per offending loan_record), but the DETECTION is an aggregate over the whole
# batch.
#
# Three check types, matching data/seed/validation_rules.json:
#   duplicate_key         -- same key(s) within a partition (per-file or global)
#   fingerprint_duplicate -- same soft fingerprint, different loan_id (a clone)
#   repeat_pattern        -- > N originations by one borrower within W days
#
# Same conventions as run_row_validation: does NOT commit, one audit event,
# idempotent (skip if results exist unless force=True).
# ---------------------------------------------------------------------------


def _clear_previous_dataset(rule_ids):
    """Clear a previous dataset-scope run on a forced re-run.

    Scoped to the given dataset rule_ids so a force-rerun of B2 does not
    delete B1's row-scope exceptions -- the row and dataset passes are
    independent and must be clearable independently. Reviewer decisions are
    preserved: only OPEN exceptions are deleted, and their (rule, loan) pairs
    are returned so the creation pass skips already-decided defects.
    """
    decided = {
        (rule_id, loan_id)
        for rule_id, loan_id in db.session.execute(
            db.select(ExceptionRecord.rule_id, ExceptionRecord.loan_id)
            .filter(ExceptionRecord.exception_type == "validation_failure",
                    ExceptionRecord.rule_id.in_(rule_ids),
                    ExceptionRecord.status != "open"))
    }
    db.session.execute(sa.delete(ExceptionRecord).filter(
        ExceptionRecord.exception_type == "validation_failure",
        ExceptionRecord.rule_id.in_(rule_ids),
        ExceptionRecord.status == "open"))
    db.session.execute(sa.delete(ValidationResult).filter(
        ValidationResult.rule_id.in_(rule_ids),
        ValidationResult.loan_record_id.isnot(None)))
    return decided


def _load_dataset_records():
    """All loan_records with raw_file_id and canonical fields for dataset checks.

    Two queries, not a per-row join: the dataset checks iterate the full list
    in Python, and the raw_file_map is a flat {raw_record_id: raw_file_id}.
    """
    raw_file_map = dict(db.session.execute(
        db.select(RawRecord.id, RawRecord.raw_file_id)).all())
    records = db.session.execute(
        db.select(LoanRecord).order_by(LoanRecord.source_system,
                                       LoanRecord.version)
    ).scalars().all()
    out = []
    for rec in records:
        out.append({
            "id": rec.id,
            "loan_id_fk": rec.loan_id,
            "raw_record_id": rec.raw_record_id,
            "raw_file_id": raw_file_map.get(rec.raw_record_id),
            "source_system": rec.source_system,
            "version": rec.version,
            "data": rec.data or {},
        })
    return out


def _check_duplicate_key(records, condition):
    """Same key(s) within a partition.  Returns (failing_ids, na_ids).

    partition_by='raw_file_id' catches a loan boarded twice in the same file;
    'global' catches it across files.  Records with a None key go to
    not_applicable -- a missing loan_id is REQUIRED_CORE_FIELDS' problem, not
    a duplicate signal.
    """
    keys = condition["keys"]
    partition_by = condition.get("partition_by", "global")
    na = set()
    groups = defaultdict(list)
    for rec in records:
        vals = tuple(rec["data"].get(k) for k in keys)
        if any(v is None for v in vals):
            na.add(rec["id"])
            continue
        partition = (rec["raw_file_id"]
                     if partition_by == "raw_file_id" else "global")
        groups[(partition, vals)].append(rec)
    failing = set()
    for group in groups.values():
        if len(group) > 1:
            failing.update(r["id"] for r in group)
    return failing, na


def _check_fingerprint_duplicate(records, condition):
    """Same soft fingerprint, different loan_id.  Returns (failing_ids, na_ids).

    Distinct from duplicate_key: a group with the same fingerprint AND the
    same loan_id is a plain duplicate (duplicate_key catches it).  Only a
    group where the loan_ids differ is a clone -- the borrower is real, but
    someone boarded a second loan under a fresh id.
    """
    keys = condition["keys"]
    na = set()
    groups = defaultdict(list)
    for rec in records:
        vals = tuple(rec["data"].get(k) for k in keys)
        if any(v is None for v in vals):
            na.add(rec["id"])
            continue
        groups[vals].append(rec)
    failing = set()
    for group in groups.values():
        if len(group) <= 1:
            continue
        loan_ids = {r["data"].get("loan_id") for r in group}
        if len(loan_ids) > 1:
            failing.update(r["id"] for r in group)
    return failing, na


def _check_repeat_pattern(records, condition):
    """> N originations by one borrower within W days.  Returns (failing_ids, na_ids).

    Sliding window per borrower: sort by origination_date, then for each
    starting record count how many fall within window_days.  If the count
    exceeds count_gt, every record in that window is flagged.
    """
    key_field = condition["key"]
    count_gt = condition["count_gt"]
    window_days = condition["window_days"]
    na = set()
    groups = defaultdict(list)
    for rec in records:
        kv = rec["data"].get(key_field)
        if kv is None:
            na.add(rec["id"])
            continue
        dt = _to_naive_utc(rec["data"].get("origination_date"))
        if dt is None:
            na.add(rec["id"])
            continue
        groups[kv].append((rec, dt))
    failing = set()
    for group in groups.values():
        group.sort(key=lambda x: x[1])
        for i, (rec, start) in enumerate(group):
            in_window = [r for r, d in group[i:]
                         if (d - start).days <= window_days]
            if len(in_window) > count_gt:
                failing.update(r["id"] for r in in_window)
    return failing, na


_DATASET_CHECKS = {
    "duplicate_key": _check_duplicate_key,
    "fingerprint_duplicate": _check_fingerprint_duplicate,
    "repeat_pattern": _check_repeat_pattern,
}


def run_dataset_validation(force=False):
    """Run all active dataset-scope rules over every loan_record.  Does NOT commit.

    Mirror of run_row_validation for aggregate checks: one validation_results
    row per (record, rule), one exception per fail, one audit event for the
    run.  Idempotent -- a second call returns {'skipped': ...} unless force.
    """
    rules = db.session.execute(
        db.select(ValidationRule)
        .filter_by(scope="dataset", is_active=True)
        .order_by(ValidationRule.rule_code)
    ).scalars().all()
    if not rules:
        raise RuntimeError(
            "no active scope='dataset' rules -- "
            "seed data/seed/validation_rules.json first")

    rule_ids = [r.id for r in rules]
    existing = _existing_count(rule_ids)
    if existing and not force:
        return {"skipped": "already_validated", "existing_results": existing}
    decided_pairs = _clear_previous_dataset(rule_ids) if existing else set()

    records = _load_dataset_records()

    result_rows, exceptions = [], []
    skipped_decided = 0
    tally = {"pass": 0, "fail": 0, "not_applicable": 0}

    for rule in rules:
        check_type = rule.condition.get("check")
        check_fn = _DATASET_CHECKS.get(check_type)
        if check_fn is None:
            raise ValueError(f"unknown dataset check: {check_type!r}")
        failing_ids, na_ids = check_fn(records, rule.condition)

        for rec in records:
            if rec["id"] in failing_ids:
                result = "fail"
            elif rec["id"] in na_ids:
                result = "not_applicable"
            else:
                result = "pass"
            tally[result] += 1

            result_rows.append({
                "id": uuid.uuid4(),
                "loan_record_id": rec["id"],
                "loan_id": rec["loan_id_fk"],
                "rule_id": rule.id,
                "rule_version": rule.version,
                "result": result,
                "details": {
                    "check": check_type,
                    "partition_by": rule.condition.get("partition_by", "global"),
                    "source_system": rec["source_system"],
                    "record_version": rec["version"],
                },
            })

            if result != "fail":
                continue

            if (rule.id, rec["loan_id_fk"]) in decided_pairs:
                skipped_decided += 1
                continue

            exceptions.append(ExceptionRecord(
                id=uuid.uuid4(),
                loan_id=rec["loan_id_fk"],
                raw_record_id=rec["raw_record_id"],
                exception_type="validation_failure",
                rule_id=rule.id,
                field_name=None,
                severity=rule.severity,
                is_blocking=rule.severity in BLOCKING_SEVERITIES,
                status="open",
                detail={
                    "rule_code": rule.rule_code,
                    "rule_version": rule.version,
                    "source_system": rec["source_system"],
                    "record_version": rec["version"],
                    "message": render_message(
                        rule.message_template, rec["data"]),
                    "check": check_type,
                    "partition_by": rule.condition.get("partition_by", "global"),
                },
            ))

    if result_rows:
        db.session.execute(sa.insert(ValidationResult), result_rows)
    db.session.add_all(exceptions)

    log_event(
        event_type="validation_executed",
        entity_type="validation_run",
        actor_type="system",
        after_value={
            "scope": "dataset",
            "rules": len(rules),
            "records": len(records),
            "results": len(result_rows),
            "passed": tally["pass"],
            "failed": tally["fail"],
            "not_applicable": tally["not_applicable"],
            "exceptions_created": len(exceptions),
        },
        reason="dataset-scope validation run",
    )

    return {"rules": len(rules), "records": len(records),
            "results_written": len(result_rows), **tally,
            "exceptions_created": len(exceptions),
            "decisions_preserved": len(decided_pairs),
            "decided_skipped": skipped_decided}


# ---------------------------------------------------------------------------
# Stage 3c -- cross-source conflict detection (B3)
#
# SOURCE_CONFLICT is deliberately NOT a rule row. A rule in validation_rules
# has scope='cross_source' and a condition tree, but the cross-source executor
# does not evaluate a condition against one record -- it COMPARES two records
# from different sources for the same loan and asks whether their values are
# materially different. That is a different kind of check, and shoehorning it
# into the DSL would require either a multi-record context node (which breaks
# the purity/security argument for the evaluator) or a per-field rule row per
# conflict field (which clutters the rules table with infrastructure).
#
# So the executor lives here alongside the row and dataset runners, produces
# ExceptionRecord rows with exception_type='source_conflict' and rule_id=NULL,
# and does NOT produce validation_results (which require a rule_id FK).
#
# The fields it compares are the ones where both OriginationCore and
# ServicerFeed have a view: current_balance and payment_status. The material
# threshold matches the generator's MATERIAL_ABS / MATERIAL_PCT so a 0.01
# rounding difference never becomes a 500-row exception queue.
#
# Same conventions: does NOT commit, one audit event, idempotent.
# ---------------------------------------------------------------------------

# Materiality thresholds -- match data/generate_dataset.py.
# A balance difference below both is immaterial (timing/rounding), not a
# data-integrity conflict.
_CONFLICT_ABS = 500.0
_CONFLICT_PCT = 0.01

# The fields to compare and how to compare them. Categorical fields (payment
# status) conflict on any difference; numeric fields use the materiality
# thresholds. This is data, not a hardcoded list, in the sense that adding a
# field here is a code change -- but the fields are the structural ones where
# cross-source disagreement is both possible and meaningful.
_CONFLICT_FIELDS = {
    "current_balance": "numeric",
    "payment_status": "categorical",
}


def _clear_previous_cross_source():
    """Clear previous source_conflict exceptions on a forced re-run.

    Scoped to exception_type='source_conflict' so B1/B2 validation_failure
    exceptions are untouched. Reviewer decisions are preserved: only OPEN
    conflicts are deleted, and their (loan, field) pairs are returned so the
    detection pass skips already-decided disagreements.
    """
    decided = {
        (loan_id, field)
        for loan_id, field in db.session.execute(
            db.select(ExceptionRecord.loan_id, ExceptionRecord.field_name)
            .filter(ExceptionRecord.exception_type == "source_conflict",
                    ExceptionRecord.status != "open"))
    }
    db.session.execute(sa.delete(ExceptionRecord).filter(
        ExceptionRecord.exception_type == "source_conflict",
        ExceptionRecord.status == "open"))
    return decided


def _latest_record(records, source_system):
    """Pick the highest-version record for a source from a loan's record list.

    Scans for the max version rather than relying on query order, because
    _load_dataset_records sorts version ASC for B2's aggregate checks.
    """
    best = None
    for rec in records:
        if rec["source_system"] == source_system:
            if best is None or rec["version"] > best["version"]:
                best = rec
    return best


def run_cross_source_validation(force=False):
    """Detect material disagreements between OriginationCore and ServicerFeed
    for the same loan.  Does NOT commit.

    For every loan that has a record in both sources, compare current_balance
    and payment_status. A material difference creates one ExceptionRecord per
    conflicting field, pointing at the ServicerFeed record (the 'newer' source
    that disagrees with the origination truth).

    Idempotent: a second call returns {'skipped': ...} unless force.
    """
    existing = db.session.execute(
        db.select(sa.func.count(ExceptionRecord.id)).filter(
            ExceptionRecord.exception_type == "source_conflict")
    ).scalar()
    if existing and not force:
        return {"skipped": "already_detected", "existing_exceptions": existing}
    decided_pairs = _clear_previous_cross_source() if existing else set()

    records = _load_dataset_records()

    # Group by loan_id FK (UUID) so we can compare sources for the same loan.
    by_loan = defaultdict(list)
    for rec in records:
        by_loan[rec["loan_id_fk"]].append(rec)

    exceptions = []
    loans_compared = 0
    conflicts_found = 0

    for loan_id_fk, recs in by_loan.items():
        orig = _latest_record(recs, "OriginationCore")
        serv = _latest_record(recs, "ServicerFeed")
        if orig is None or serv is None:
            continue
        loans_compared += 1

        orig_data = orig["data"]
        serv_data = serv["data"]

        for field, mode in _CONFLICT_FIELDS.items():
            orig_val = orig_data.get(field)
            serv_val = serv_data.get(field)

            # If either source doesn't carry the field, there is nothing to
            # compare -- not a conflict, just a coverage gap.
            if orig_val is None or serv_val is None:
                continue

            is_conflict = False
            if mode == "categorical":
                is_conflict = orig_val != serv_val
            elif mode == "numeric":
                try:
                    orig_f = float(orig_val)
                    serv_f = float(serv_val)
                except (TypeError, ValueError):
                    continue
                diff = abs(orig_f - serv_f)
                if diff <= _CONFLICT_ABS:
                    continue
                # Percentage threshold: relative to the larger magnitude so a
                # 500 difference on a 50k balance is immaterial (0.1%) while
                # the same 500 on a 5k balance is material (10%).
                base = max(abs(orig_f), abs(serv_f), 1.0)
                if diff / base < _CONFLICT_PCT:
                    continue
                is_conflict = True

            if not is_conflict:
                continue

            if (loan_id_fk, field) in decided_pairs:
                continue

            conflicts_found += 1
            exceptions.append(ExceptionRecord(
                id=uuid.uuid4(),
                loan_id=loan_id_fk,
                raw_record_id=serv["raw_record_id"],
                raw_file_id=serv["raw_file_id"],
                exception_type="source_conflict",
                rule_id=None,
                field_name=field,
                severity="MEDIUM",
                is_blocking=False,
                status="open",
                detail={
                    "field": field,
                    "origination_value": orig_val,
                    "servicer_value": serv_val,
                    "source_system": "ServicerFeed",
                    "record_version": serv["version"],
                    "message": (f"{field} conflict: OriginationCore says "
                                f"{orig_val}, ServicerFeed says {serv_val}"),
                },
            ))

    db.session.add_all(exceptions)

    log_event(
        event_type="validation_executed",
        entity_type="cross_source_run",
        actor_type="system",
        after_value={
            "scope": "cross_source",
            "loans_compared": loans_compared,
            "conflicts_found": conflicts_found,
            "exceptions_created": len(exceptions),
        },
        reason="cross-source conflict detection",
    )

    return {"loans_compared": loans_compared,
            "conflicts_found": conflicts_found,
            "exceptions_created": len(exceptions),
            "decisions_preserved": len(decided_pairs)}
