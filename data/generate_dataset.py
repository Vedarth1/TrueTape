#!/usr/bin/env python3
"""
TrueTape synthetic dataset generator.

Emits a deliberately messy multi-source loan tape PLUS a machine-readable
oracle of every defect injected, so the validation engine can be scored
against ground truth instead of eyeballed.

    python data/generate_dataset.py --rows 1200 --seed 42 --out data/seed
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ── canonical schema ─────────────────────────────────────────────────────
CANONICAL_FIELDS = [
    "loan_id", "borrower_id", "borrower_name", "original_principal",
    "current_balance", "interest_rate", "origination_date", "maturity_date",
    "loan_term_months", "payment_status", "days_past_due", "property_state",
    "loan_purpose", "property_type", "credit_score", "ltv_ratio",
    "dti_ratio", "document_status", "last_updated_at", "source_system",
    "servicer_name",
]
assert len(CANONICAL_FIELDS) == 21, "the docs claim 21 canonical fields"

SERVICER_FIELDS = [
    "loan_id", "current_balance", "payment_status", "days_past_due",
    "last_updated_at", "source_system", "servicer_name",
]
MANIFEST_FIELDS = ["loan_id", "document_type", "document_status", "received_date"]

PAYMENT_STATUS_CANON = {
    "current": "current", "30-59 days": "dpd_30_59", "60-89 days": "dpd_60_89",
    "90+ days": "dpd_90_plus", "default": "default",
    "paid off": "paid_off", "closed": "closed",
}

# payment_status -> the days_past_due range that is CONSISTENT with it
STATUS_DPD = {
    "Current":     (0, 0),
    "30-59 Days":  (30, 59),
    "60-89 Days":  (60, 89),
    "90+ Days":    (90, 180),
    "Default":     (181, 400),
    "Paid Off":    (0, 0),
    "Closed":      (0, 0),
}
ZERO_BALANCE_STATUSES = {"Paid Off", "Closed"}
DELINQUENT_STATUSES = {"30-59 Days", "60-89 Days", "90+ Days", "Default"}

VALID_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
]
SERVICERS = ["ServicerX", "MeridianServicing", "AtlasLoanCare"]
FIRST = ["Priya", "Arjun", "Meera", "Rahul", "Ananya", "Vikram", "Sneha",
         "Karan", "Divya", "Rohan", "Ishita", "Aditya", "Nisha", "Manav",
         "Tara", "Dev", "Kavya", "Siddharth", "Riya", "Aman"]
LAST = ["Sharma", "Iyer", "Nair", "Reddy", "Kapoor", "Mehta", "Bose",
        "Chauhan", "Pillai", "Joshi", "Verma", "Rao", "Malhotra", "Sethi",
        "Gupta", "Banerjee", "Desai", "Kulkarni", "Menon", "Trivedi"]

STALE_DAYS = 180          # rule 11 threshold
MATERIAL_ABS = 500.0      # cross-source conflict thresholds
MATERIAL_PCT = 0.01

# The oracle speaks two vocabularies: a defect_class is how a human names what
# went wrong; a rule_code is what validation_results.rule_code will hold. The QA
# harness joins on rule_code, so the mapping lives here exactly once.
RULE_CODE = {
    "MISSING_REQUIRED_FIELD":         "REQUIRED_CORE_FIELDS",
    "DUPLICATE_LOAN_ID":              "DUPLICATE_LOAN_ID",
    "DUPLICATE_BORROWER_FINGERPRINT": "DUPLICATE_BORROWER_FINGERPRINT",
    "INVALID_DATE_FORMAT":            "INVALID_DATE_FORMAT",
    "INVALID_NUMERIC_FORMAT":         "INVALID_NUMERIC_FORMAT",
    "MATURITY_BEFORE_ORIGINATION":    "MATURITY_AFTER_ORIGINATION",
    "NEGATIVE_PRINCIPAL":             "NON_NEGATIVE_PRINCIPAL",
    "NEGATIVE_BALANCE":               "NON_NEGATIVE_BALANCE",
    "BALANCE_EXCEEDS_PRINCIPAL":      "CURRENT_BALANCE_LE_ORIGINAL_PRINCIPAL",
    "RATE_OUT_OF_RANGE":              "INTEREST_RATE_IN_RANGE",
    "STATUS_CURRENT_WITH_DPD":        "PAYMENT_STATUS_DPD_CONSISTENT",
    "STATUS_DELINQUENT_WITHOUT_DPD":  "DELINQUENT_STATUS_DPD_CONSISTENT",
    "MISSING_DOCUMENT_STATUS":        "REQUIRED_DOCUMENT_STATUS",
    "INVALID_PAYMENT_STATUS":         "VALID_PAYMENT_STATUS",
    "STALE_LAST_UPDATED":             "STALENESS_THRESHOLD",
    "INVALID_STATE_CODE":             "VALID_BORROWER_STATE",
    "REPEATED_BORROWER_PATTERN":      "REPEATED_BORROWER_PATTERN",
    "CLOSED_LOAN_POSITIVE_BALANCE":   "CLOSED_LOAN_ZERO_BALANCE",
    # Not rules — scored against other tables, so deliberately unmapped.
    "MISSING_LOAN_ID":                None,   # quarantine path
    "SOURCE_CONFLICT":                None,   # cross-source executor
}
assert len({v for v in RULE_CODE.values() if v}) == 18, "seed rule count drifted"


class Oracle:
    """Ground truth for every defect this generator injects on purpose."""

    COLUMNS = ["file_name", "row_ref", "loan_id", "defect_class", "rule_code",
               "field_name", "injected_value", "note"]

    def __init__(self):
        self.rows = []

    def record(self, **kw):
        defect = kw.pop("rule_code")          # call sites still pass the defect class
        if defect not in RULE_CODE:
            raise KeyError(f"unmapped defect class {defect!r} — add it to RULE_CODE")
        kw["defect_class"] = defect
        kw["rule_code"] = RULE_CODE[defect] or ""
        self.rows.append({c: kw.get(c, "") for c in self.COLUMNS})

    def counts(self):
        out = {}
        for r in self.rows:
            key = (r["defect_class"], r["rule_code"] or "—")
            out[key] = out.get(key, 0) + 1
        return dict(sorted(out.items()))


# ── clean data ───────────────────────────────────────────────────────────
def build_clean_loans(rows: int, rng: random.Random, as_of: date) -> list[dict]:
    """Every row here is VALID. Any exception the engine raises against an
    untouched row is a false positive and therefore a bug."""
    loans = []
    for i in range(1, rows + 1):
        term = rng.choice([120, 180, 240, 300, 360])
        origination = as_of - timedelta(days=rng.randint(90, 6 * 365))
        maturity = origination + timedelta(days=int(term * 30.44))

        principal = round(rng.uniform(50_000, 900_000), 2)
        status = rng.choices(
            list(STATUS_DPD),
            weights=[70, 8, 5, 4, 3, 6, 4],
        )[0]
        dpd_lo, dpd_hi = STATUS_DPD[status]
        balance = 0.0 if status in ZERO_BALANCE_STATUSES else round(
            principal * rng.uniform(0.55, 0.99), 2)

        updated = as_of - timedelta(days=rng.randint(0, 55))

        loans.append({
            "_row_ref": f"loan_tape#{i:05d}",
            "loan_id": f"LN-{i:06d}",
            "borrower_id": f"BR-{rng.randint(1, rows * 2):06d}",
            "borrower_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
            "original_principal": principal,
            "current_balance": balance,
            "interest_rate": round(rng.uniform(3.0, 14.5), 3),
            "origination_date": origination.isoformat(),
            "maturity_date": maturity.isoformat(),
            "loan_term_months": term,
            "payment_status": status,
            "days_past_due": rng.randint(dpd_lo, dpd_hi),
            "property_state": rng.choice(VALID_STATES),
            "loan_purpose": rng.choice(
                ["Purchase", "Refinance", "CashOutRefi", "Construction"]),
            "property_type": rng.choice(
                ["SingleFamily", "Condo", "MultiFamily", "Townhouse"]),
            "credit_score": rng.randint(580, 820),
            "ltv_ratio": round(rng.uniform(0.40, 0.95), 4),
            "dti_ratio": round(rng.uniform(0.15, 0.50), 4),
            "document_status": rng.choices(
                ["Complete", "Pending", "Missing"], weights=[80, 15, 5])[0],
            "last_updated_at": f"{updated.isoformat()}T09:00:00Z",
            "source_system": "OriginationCore",
            "servicer_name": rng.choice(SERVICERS),
        })
    return loans


# ── defect injection ─────────────────────────────────────────────────────
def inject_defects(loans, rng, oracle, as_of, rows) -> None:
    """Mutates `loans` in place. Each row receives AT MOST ONE defect."""

    def n(base: int) -> int:                      # scale counts with --rows
        return max(1, round(base * rows / 1200))

    pool = list(range(len(loans)))
    rng.shuffle(pool)

    def take(count: int) -> list[int]:
        return [pool.pop() for _ in range(min(count, len(pool)))]

    def hit(idx, rule_code, field, value, note=""):
        loan = loans[idx]
        oracle.record(file_name="loan_tape.csv", row_ref=loan["_row_ref"],
                      loan_id=loan["loan_id"], rule_code=rule_code,
                      field_name=field, injected_value=value, note=note)

    # 1. missing loan_id  -> QUARANTINE PATH, not a validation failure.
    #    These rows have no loan to attach a result to (architecture §2).
    for i in take(n(4)):
        hit(i, "MISSING_LOAN_ID", "loan_id", "", "quarantined, never validated")
        loans[i]["loan_id"] = ""

    # 2. missing required field (principal)
    for i in take(n(6)):
        hit(i, "MISSING_REQUIRED_FIELD", "original_principal", "")
        loans[i]["original_principal"] = ""

    # 3. duplicate loan_id within one file
    donors = [j for j in pool if loans[j]["loan_id"]]
    for i in take(n(5)):
        donor = loans[rng.choice(donors)]["loan_id"]
        hit(i, "DUPLICATE_LOAN_ID", "loan_id", donor, f"clone of {donor}")
        loans[i]["loan_id"] = donor

    # 4. malformed dates - three distinct shapes, because real tapes have all three
    for k, i in enumerate(take(n(12))):
        raw = {0: "31/07/2024", 1: "07-31-2024", 2: "Jul 31 2024"}[k % 3]
        hit(i, "INVALID_DATE_FORMAT", "origination_date", raw)
        loans[i]["origination_date"] = raw

    # 4b. malformed numerics
    for k, i in enumerate(take(n(9))):
        raw = {0: "1,25,000", 1: "$118500", 2: "N/A"}[k % 3]
        hit(i, "INVALID_NUMERIC_FORMAT", "current_balance", raw)
        loans[i]["current_balance"] = raw

    # 5. maturity before origination
    for i in take(n(5)):
        orig = date.fromisoformat(loans[i]["origination_date"])
        bad = (orig - timedelta(days=rng.randint(30, 900))).isoformat()
        hit(i, "MATURITY_BEFORE_ORIGINATION", "maturity_date", bad)
        loans[i]["maturity_date"] = bad

    # 6a / 6b. negative amounts
    for i in take(n(3)):
        v = -abs(float(loans[i]["original_principal"] or 100000))
        hit(i, "NEGATIVE_PRINCIPAL", "original_principal", v)
        loans[i]["original_principal"] = round(v, 2)
    for i in take(n(4)):
        v = -round(rng.uniform(500, 40_000), 2)
        hit(i, "NEGATIVE_BALANCE", "current_balance", v)
        loans[i]["current_balance"] = v

    # 7. balance exceeds original principal
    for i in take(n(7)):
        v = round(float(loans[i]["original_principal"]) * rng.uniform(1.05, 1.4), 2)
        hit(i, "BALANCE_EXCEEDS_PRINCIPAL", "current_balance", v)
        loans[i]["current_balance"] = v
        if loans[i]["payment_status"] in ZERO_BALANCE_STATUSES:
            loans[i]["payment_status"] = "Current"      # keep it a single defect
            loans[i]["days_past_due"] = 0

    # 8. interest rate out of range
    for k, i in enumerate(take(n(6))):
        v = round(rng.uniform(30.0, 95.0), 3) if k % 2 else round(rng.uniform(-4.0, -0.1), 3)
        hit(i, "RATE_OUT_OF_RANGE", "interest_rate", v)
        loans[i]["interest_rate"] = v

    # 9 / 9b. status contradicts days_past_due, split so the message can
    #         name the actual inconsistency instead of "these disagree"
    for i in take(n(6)):
        v = rng.randint(45, 220)
        hit(i, "STATUS_CURRENT_WITH_DPD", "days_past_due", v,
            "status Current but dpd > 0")
        loans[i]["payment_status"] = "Current"
        loans[i]["days_past_due"] = v
        if loans[i]["current_balance"] in ("", 0, 0.0):
            loans[i]["current_balance"] = round(
                float(loans[i]["original_principal"] or 200000) * 0.8, 2)
    for i in take(n(5)):
        hit(i, "STATUS_DELINQUENT_WITHOUT_DPD", "days_past_due", 0,
            "status delinquent but dpd = 0")
        loans[i]["payment_status"] = rng.choice(sorted(DELINQUENT_STATUSES))
        loans[i]["days_past_due"] = 0
        if loans[i]["current_balance"] in ("", 0, 0.0):
            loans[i]["current_balance"] = round(
                float(loans[i]["original_principal"] or 200000) * 0.8, 2)

    # 10. missing document status
    for i in take(n(8)):
        hit(i, "MISSING_DOCUMENT_STATUS", "document_status", "")
        loans[i]["document_status"] = ""

    # 11. stale last_updated_at
    for i in take(n(10)):
        stale = as_of - timedelta(days=rng.randint(STALE_DAYS + 20, 900))
        v = f"{stale.isoformat()}T09:00:00Z"
        hit(i, "STALE_LAST_UPDATED", "last_updated_at", v)
        loans[i]["last_updated_at"] = v

    # 12. invalid state code
    for k, i in enumerate(take(n(5))):
        v = {0: "XX", 1: "ZZ", 2: "California"}[k % 3]
        hit(i, "INVALID_STATE_CODE", "property_state", v)
        loans[i]["property_state"] = v

    # --- INVALID_PAYMENT_STATUS -------------------------------------------
    # VALID_PAYMENT_STATUS was the one rule with no oracle coverage. Every value
    # here is unmappable even under a case-insensitive normalizer, so this tests
    # the rule rather than the normalizer.
    BAD_STATUSES = ["activ", "Unknown", "PAIDOFF", "N/A", "PastDue"]
    for k, i in enumerate(take(n(5))):
        v = BAD_STATUSES[k % len(BAD_STATUSES)]
        hit(i, "INVALID_PAYMENT_STATUS", "payment_status", v,
            "status outside the canonical vocabulary")
        loans[i]["payment_status"] = v

    # 13. closed / paid-off loan still holding a balance
    for i in take(n(5)):
        v = round(rng.uniform(1_000, 90_000), 2)
        loans[i]["payment_status"] = rng.choice(["Paid Off", "Closed"])
        loans[i]["days_past_due"] = 0
        hit(i, "CLOSED_LOAN_POSITIVE_BALANCE", "current_balance", v)
        loans[i]["current_balance"] = v

    # 14. borrower fingerprint duplicated across rows (APPENDED rows)
    seq = len(loans)
    donors = [loans[j] for j in pool[:200] if loans[j]["loan_id"]]
    for src in rng.sample(donors, k=min(n(6), len(donors))):
        seq += 1
        clone = dict(src)
        clone["_row_ref"] = f"loan_tape#{seq:05d}"
        clone["loan_id"] = f"LN-9{seq:05d}"
        clone["borrower_id"] = f"BR-9{seq:05d}"        # same person, new id
        loans.append(clone)
        oracle.record(file_name="loan_tape.csv", row_ref=clone["_row_ref"],
                      loan_id=clone["loan_id"],
                      rule_code="DUPLICATE_BORROWER_FINGERPRINT",
                      field_name="borrower_name",
                      injected_value=clone["borrower_name"],
                      note=f"same fingerprint as {src['loan_id']}")

    # 15. one borrower, six loans inside sixty days (APPENDED rows)
    ring_id = "BR-777777"
    ring_name = "Kabir Raghunathan"
    base = as_of - timedelta(days=200)
    for k in range(6):
        seq += 1
        term = 240
        origination = base + timedelta(days=k * 11)
        principal = round(rng.uniform(180_000, 420_000), 2)
        row = {
            "_row_ref": f"loan_tape#{seq:05d}",
            "loan_id": f"LN-8{seq:05d}",
            "borrower_id": ring_id,
            "borrower_name": ring_name,
            "original_principal": principal,
            "current_balance": round(principal * 0.94, 2),
            "interest_rate": round(rng.uniform(8.0, 12.0), 3),
            "origination_date": origination.isoformat(),
            "maturity_date": (origination + timedelta(days=int(term * 30.44))).isoformat(),
            "loan_term_months": term,
            "payment_status": "Current",
            "days_past_due": 0,
            "property_state": "MH",
            "loan_purpose": "Purchase",
            "property_type": "SingleFamily",
            "credit_score": rng.randint(640, 700),
            "ltv_ratio": 0.9,
            "dti_ratio": 0.44,
            "document_status": "Pending",
            "last_updated_at": f"{as_of.isoformat()}T09:00:00Z",
            "source_system": "OriginationCore",
            "servicer_name": SERVICERS[0],
        }
        loans.append(row)
        oracle.record(file_name="loan_tape.csv", row_ref=row["_row_ref"],
                      loan_id=row["loan_id"],
                      rule_code="REPEATED_BORROWER_PATTERN",
                      field_name="borrower_id", injected_value=ring_id,
                      note="6 originations in 60 days")
    # MH is not in VALID_STATES - fix it so this stays a single-defect ring
    for row in loans[-6:]:
        row["property_state"] = "MA"


# ── the other two source files ───────────────────────────────────────────
def build_servicer_update(loans, rng, oracle, as_of, rows):
    """~40% coverage. Deliberate balance/status disagreements with
    origination, many sharing ONE timestamp so clustering has real signal."""
    seen, eligible = set(), []
    for loan in loans:
        lid = loan["loan_id"]
        if not lid or lid in seen:
            continue
        seen.add(lid)
        eligible.append(loan)

    covered = rng.sample(eligible, k=int(len(eligible) * 0.40))
    n_conf = max(1, round(80 * rows / 1200))
    conflicted = set(
        l["loan_id"] for l in rng.sample(covered, k=min(n_conf, len(covered))))

    # the hero cluster: 37 records that all arrived in one bad sync
    sync_gap = rng.sample(sorted(conflicted), k=min(37, len(conflicted)))
    sync_ts = f"{(as_of - timedelta(days=7)).isoformat()}T02:14:00Z"

    out = []
    for k, loan in enumerate(covered, start=1):
        lid = loan["loan_id"]
        ref = f"servicer_update#{k:05d}"
        try:
            base_balance = float(loan["current_balance"])
        except (TypeError, ValueError):
            base_balance = round(rng.uniform(40_000, 500_000), 2)

        balance, status = base_balance, loan["payment_status"]
        updated = (as_of - timedelta(days=rng.randint(0, 20))).isoformat() + "T06:30:00Z"

        if lid in conflicted:
            if lid in sync_gap:
                updated = sync_ts
            mode = rng.choices(["balance", "status", "both"], weights=[60, 25, 15])[0]
            if mode in ("balance", "both"):
                delta = max(MATERIAL_ABS * 2, base_balance * rng.uniform(0.04, 0.22))
                balance = round(max(0.0, base_balance - delta), 2)
                oracle.record(file_name="servicer_update.csv", row_ref=ref,
                              loan_id=lid, rule_code="SOURCE_CONFLICT",
                              field_name="current_balance", injected_value=balance,
                              note=f"origination says {base_balance}")
            if mode in ("status", "both"):
                status = rng.choice(sorted(DELINQUENT_STATUSES - {loan["payment_status"]}))
                oracle.record(file_name="servicer_update.csv", row_ref=ref,
                              loan_id=lid, rule_code="SOURCE_CONFLICT",
                              field_name="payment_status", injected_value=status,
                              note=f"origination says {loan['payment_status']}")

        lo, hi = STATUS_DPD.get(status, (0, 0))
        out.append({
            "loan_id": lid,
            "current_balance": balance,
            "payment_status": status,
            "days_past_due": rng.randint(lo, hi),
            "last_updated_at": updated,
            "source_system": "ServicerFeed",
            "servicer_name": loan["servicer_name"],
        })
    return out


def build_document_manifest(loans, rng, oracle, as_of, rows):
    """Only THREE fields beyond the key. Eighteen of the twenty-one canonical
    fields are absent, which is exactly why rules must return
    `not_applicable` rather than `fail` (architecture §7)."""
    ids = [l["loan_id"] for l in loans if l["loan_id"]]
    covered = rng.sample(sorted(set(ids)), k=int(len(set(ids)) * 0.70))
    out = []
    blanks = set(rng.sample(covered, k=max(1, round(6 * rows / 1200))))
    for k, lid in enumerate(covered, start=1):
        ref = f"document_manifest#{k:05d}"
        status = "" if lid in blanks else rng.choices(
            ["Complete", "Pending", "Missing"], weights=[75, 18, 7])[0]
        if lid in blanks:
            oracle.record(file_name="document_manifest.csv", row_ref=ref,
                          loan_id=lid, rule_code="MISSING_DOCUMENT_STATUS",
                          field_name="document_status", injected_value="")
        out.append({
            "loan_id": lid,
            "document_type": rng.choice(
                ["NoteAgreement", "TitleDeed", "IncomeProof", "AppraisalReport"]),
            "document_status": status,
            "received_date": (as_of - timedelta(days=rng.randint(5, 400))).isoformat(),
        })
    return out


# ── writers ──────────────────────────────────────────────────────────────
def write_csv(path: Path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser(description="TrueTape synthetic dataset generator")
    p.add_argument("--rows", type=int, default=1200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--as-of", default="2026-08-26",
                   help="frozen reference date; keeps stale-date defects reproducible")
    p.add_argument("--out", default="data/seed")
    args = p.parse_args()

    rng = random.Random(args.seed)
    as_of = date.fromisoformat(args.as_of)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    oracle = Oracle()

    loans = build_clean_loans(args.rows, rng, as_of)
    inject_defects(loans, rng, oracle, as_of, args.rows)
    servicer = build_servicer_update(loans, rng, oracle, as_of, args.rows)
    manifest = build_document_manifest(loans, rng, oracle, as_of, args.rows)

    write_csv(out / "loan_tape.csv", CANONICAL_FIELDS, loans)
    write_csv(out / "servicer_update.csv", SERVICER_FIELDS, servicer)
    write_csv(out / "document_manifest.csv", MANIFEST_FIELDS, manifest)
    write_csv(out / "expected_exception_sample.csv", Oracle.COLUMNS, oracle.rows)

    (out / "users.json").write_text(json.dumps([
        {"username": "arun.operator", "password": "operator123",
         "role": "operator", "display_name": "Arun Deshpande"},
        {"username": "priya.reviewer", "password": "reviewer123",
         "role": "reviewer", "display_name": "Priya Sharma"},
        {"username": "neil.consumer", "password": "consumer123",
         "role": "consumer", "display_name": "Neil Fernandes"},
    ], indent=2) + "\n", encoding="utf-8")

    counts = oracle.counts()

    # counts is keyed by (defect_class, rule_code) tuples, which JSON cannot use
    # as keys. Flatten into two maps — both are worth having in the summary.
    by_defect, by_rule = {}, {}
    for (defect, code), count in counts.items():
        by_defect[defect] = by_defect.get(defect, 0) + count
        by_rule[code] = by_rule.get(code, 0) + count

    summary = {
        "seed": args.seed,
        "as_of": args.as_of,
        "rows_requested": args.rows,
        "loan_tape_rows": len(loans),
        "servicer_update_rows": len(servicer),
        "document_manifest_rows": len(manifest),
        "expected_findings": sum(counts.values()),
        "by_defect_class": dict(sorted(by_defect.items())),
        "by_rule_code": dict(sorted(by_rule.items())),
    }
    (out / "generation_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"\n  wrote 6 files to {out}/")
    print(f"  loan_tape {len(loans)}  servicer {len(servicer)}  manifest {len(manifest)}")
    print(f"  {sum(counts.values())} expected findings:\n")
    for (defect, code), count in oracle.counts().items():
        print(f"  {count:4d}  {defect:32s} -> {code or '—'}")
    print()


if __name__ == "__main__":
    main()