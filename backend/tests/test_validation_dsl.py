"""Unit tests for the row-scope DSL evaluator.

No fixtures, no app context, no Postgres: dsl.py imports nothing from the
project, and that purity is the point -- these run in milliseconds against the
REAL data/seed/validation_rules.json, so a broken semantic is caught before a
run writes ~38k validation_results rows.

What is being pinned here is not "the code works" but two specific decisions
that fail SILENTLY when they regress:

  1. Applicability is decided by the source file's column_mapping, not by
     whether a value happens to be present. A field the file never declared is
     not_applicable; a declared column with a blank cell is a FAIL. Collapse
     those two and either the manifest produces 842 false CRITICALs, or
     REQUIRED_CORE_FIELDS can never fire.

  2. Kleene three-valued logic: `and` is dominated by False, `or` by True.
     Propagating ABSENT unconditionally would downgrade legitimate passes to
     not_applicable and quietly shrink the trust-score denominator.

A regression in either produces zero exceptions and looks exactly like a clean
dataset, which is why test_no_rule_is_universally_not_applicable exists.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.validation.dsl import ABSENT, EvalContext, evaluate

_SEED = (Path(__file__).resolve().parents[2]
         / "data" / "seed" / "validation_rules.json")
ROW_RULES = [r for r in json.loads(_SEED.read_text()) if r["scope"] == "row"]

# Fixed clock so STALENESS_THRESHOLD is deterministic.
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)

# The canonical fields each source file declares a column for. Mirrors what
# normalizer._build_column_mapping derives from the real CSV headers: the loan
# tape carries everything except document_status, the manifest carries three.
TAPE_COLS = frozenset({
    "loan_id", "borrower_id", "borrower_name", "original_principal",
    "current_balance", "interest_rate", "origination_date", "maturity_date",
    "loan_term_months", "payment_status", "days_past_due", "property_state",
    "loan_purpose", "property_type", "credit_score", "ltv_ratio", "dti_ratio",
    "last_updated_at", "source_system", "servicer_name"})
MANIFEST_COLS = frozenset({"loan_id", "document_status", "last_updated_at"})

CLEAN = {
    "loan_id": "LN-0000001", "borrower_id": "BR-0001",
    "borrower_name": "Ada Lovelace",
    "original_principal": 250000.0, "current_balance": 180000.0,
    "interest_rate": 6.25,
    "origination_date": "2022-03-15", "maturity_date": "2052-03-15",
    "loan_term_months": 360,
    "payment_status": "current", "days_past_due": 0,
    "property_state": "CA", "loan_purpose": "purchase",
    "property_type": "single_family",
    "credit_score": 740, "ltv_ratio": 0.72, "dti_ratio": 0.31,
    "last_updated_at": (NOW - timedelta(days=10)).isoformat(),
    "source_system": "OriginationCore", "servicer_name": "ServicerX",
}


def verdicts(data, in_scope, field_errors=None):
    """rule_code -> 'pass' | 'fail' | 'not_applicable' for all 15 row rules."""
    out = {}
    for rule in ROW_RULES:
        ctx = EvalContext(data, field_errors, in_scope, NOW,
                          source_system=data.get("source_system"))
        out[rule["rule_code"]], _ = evaluate(rule["condition"], ctx)
    return out


def fails(data, in_scope, field_errors=None):
    return {k for k, v in verdicts(data, in_scope, field_errors).items()
            if v == "fail"}


def without(field, **overrides):
    """A CLEAN row with `field` absent from data -- i.e. a blank cell."""
    row = dict(CLEAN, **overrides)
    row.pop(field, None)
    return row


# --- baseline --------------------------------------------------------------

def test_clean_tape_row_has_no_failures():
    v = verdicts(CLEAN, TAPE_COLS)
    assert not {k for k, r in v.items() if r == "fail"}
    # document_status is the one canonical field the loan tape has no column
    # for, so exactly one rule is out of scope.
    assert [k for k, r in v.items() if r == "not_applicable"] == \
        ["REQUIRED_DOCUMENT_STATUS"]


# --- decision 1: scope, not presence ---------------------------------------

def test_blank_cell_in_a_declared_column_is_a_failure():
    """THE test for the column_mapping design.

    original_principal's column exists in the loan tape but the cell is empty.
    Only REQUIRED_CORE_FIELDS may fail: the comparisons that reference the same
    field see an ABSENT operand and are correctly not_applicable, so one blank
    cell yields one exception rather than three.
    """
    row = without("original_principal")
    assert fails(row, TAPE_COLS) == {"REQUIRED_CORE_FIELDS"}
    v = verdicts(row, TAPE_COLS)
    assert v["NON_NEGATIVE_PRINCIPAL"] == "not_applicable"
    assert v["CURRENT_BALANCE_LE_ORIGINAL_PRINCIPAL"] == "not_applicable"


def test_manifest_row_produces_no_false_exceptions():
    """13 of the 15 row rules are not_applicable against a manifest row.

    Plus the 3 dataset rules, that is the "16 of the 18 seed rules" the
    loan-record model docstring calls the correct answer. A boolean verdict here
    would instead mean 842 spurious CRITICALs.
    """
    manifest = {"loan_id": "LN-0000001", "document_status": "complete",
                "last_updated_at": (NOW - timedelta(days=5)).isoformat()}
    v = verdicts(manifest, MANIFEST_COLS)
    assert not {k for k, r in v.items() if r == "fail"}
    assert sum(1 for r in v.values() if r == "not_applicable") == 13
    assert v["REQUIRED_CORE_FIELDS"] == "not_applicable"
    assert v["REQUIRED_DOCUMENT_STATUS"] == "pass"


def test_coercion_failure_is_caught_by_invalid_date_format():
    """A failed coercion lives in field_errors and is ABSENT from data.

    If field_error() read the value instead of inspecting scope, this row would
    be indistinguishable from a blank cell and the rule could never fire.
    REQUIRED_CORE_FIELDS must still pass: the value is present, merely
    malformed, and one bad cell should not raise two exceptions.
    """
    row = without("origination_date")
    errs = {"origination_date": {"raw": "13/45/2024", "expected": "date"}}
    assert fails(row, TAPE_COLS, errs) == {"INVALID_DATE_FORMAT"}
    v = verdicts(row, TAPE_COLS, errs)
    assert v["REQUIRED_CORE_FIELDS"] == "pass"
    assert v["MATURITY_AFTER_ORIGINATION"] == "not_applicable"


# --- decision 2: Kleene logic ---------------------------------------------

def test_or_short_circuits_true_over_absent():
    """paid_off with no days_past_due column value is a genuine PASS.

    PAYMENT_STATUS_DPD_CONSISTENT is or(status != 'current', dpd == 0). The
    left branch is True, so the rule is vacuously satisfied and the missing dpd
    is irrelevant. Unconditional ABSENT propagation would call this
    not_applicable and silently drop it from the trust denominator.
    """
    row = without("days_past_due", payment_status="paid_off",
                  current_balance=0.0)
    v = verdicts(row, TAPE_COLS)
    assert not {k for k, r in v.items() if r == "fail"}
    assert v["PAYMENT_STATUS_DPD_CONSISTENT"] == "pass"
    assert v["CLOSED_LOAN_ZERO_BALANCE"] == "pass"


def test_absent_has_no_truth_value():
    """The sentinel guard itself: `if value:` on ABSENT must be a loud error."""
    with pytest.raises(TypeError):
        bool(ABSENT)


# --- ordinary rule firing --------------------------------------------------

def test_negative_principal():
    # CURRENT_BALANCE_LE_ORIGINAL_PRINCIPAL necessarily fails too: no
    # non-negative balance can be <= a negative principal.
    assert fails(dict(CLEAN, original_principal=-5000.0), TAPE_COLS) == {
        "NON_NEGATIVE_PRINCIPAL", "CURRENT_BALANCE_LE_ORIGINAL_PRINCIPAL"}


def test_maturity_before_origination():
    """Also pins that ISO date strings order lexicographically -- the reason
    this rule needs no date parsing at all."""
    assert fails(dict(CLEAN, maturity_date="2020-01-01"), TAPE_COLS) == {
        "MATURITY_AFTER_ORIGINATION"}


def test_staleness_beyond_180_days():
    """Exercises days_between across the tz-aware `now` / naive-ISO field mix."""
    stale = dict(CLEAN,
                 last_updated_at=(NOW - timedelta(days=200)).isoformat())
    assert fails(stale, TAPE_COLS) == {"STALENESS_THRESHOLD"}
    fresh = dict(CLEAN,
                 last_updated_at=(NOW - timedelta(days=179)).isoformat())
    assert not fails(fresh, TAPE_COLS)


def test_unmappable_payment_status_survives_to_fail_here():
    """'activ' is not in PAYMENT_STATUS_CANON, so normalization keeps it as
    lowercased raw text rather than coercing it away -- and VALID_PAYMENT_STATUS
    is what turns that into an exception."""
    assert fails(dict(CLEAN, payment_status="activ"), TAPE_COLS) == {
        "VALID_PAYMENT_STATUS"}


def test_interest_rate_out_of_range():
    assert fails(dict(CLEAN, interest_rate=41.0), TAPE_COLS) == {
        "INTEREST_RATE_IN_RANGE"}


def test_invalid_state_code():
    assert fails(dict(CLEAN, property_state="ZZ"), TAPE_COLS) == {
        "VALID_BORROWER_STATE"}


# --- the silent-failure detector -------------------------------------------

def test_no_rule_is_universally_not_applicable():
    """A rule that is not_applicable on every source can never fire, and its
    absence from the exception queue is indistinguishable from clean data. Each
    row rule must reach a real verdict on at least one of the two shapes."""
    manifest = {"loan_id": "LN-0000001", "document_status": "complete",
                "last_updated_at": (NOW - timedelta(days=5)).isoformat()}
    tape_v = verdicts(CLEAN, TAPE_COLS)
    man_v = verdicts(manifest, MANIFEST_COLS)
    dead = [code for code in tape_v
            if tape_v[code] == "not_applicable"
            and man_v[code] == "not_applicable"]
    assert dead == [], f"rules that can never fire: {dead}"


def test_every_seed_row_rule_is_exercised():
    """Guards against a rule being added to the seed JSON with no coverage."""
    assert len(ROW_RULES) == 15
    assert len(verdicts(CLEAN, TAPE_COLS)) == 15
