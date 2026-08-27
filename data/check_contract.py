#!/usr/bin/env python3
"""Fails if the seed rules and the oracle disagree.

Field-name drift shows up as a crash in the engine. Value-set drift is silent —
a rule quietly scores zero recall, or fires on every row. This catches both by
comparing the two files that define the contract. Run it after touching either.
"""
import csv, json, sys
from pathlib import Path

SEED = Path(__file__).resolve().parent / "seed"

rules = {r["rule_code"] for r in json.loads((SEED / "validation_rules.json").read_text())["rules"]}
with (SEED / "expected_exception_sample.csv").open(newline="") as fh:
    oracle = {r["rule_code"] for r in csv.DictReader(fh) if r.get("rule_code")}

problems = ([f"oracle code with no rule : {c}" for c in sorted(oracle - rules)]
            + [f"rule with no oracle cover: {c}" for c in sorted(rules - oracle)])
for p in problems:
    print(p)
print(f"{len(rules)} rules, {len(oracle)} covered — {'FAIL' if problems else 'OK'}")
sys.exit(1 if problems else 0)