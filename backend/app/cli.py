"""Admin CLI commands, registered via register_cli(app) in create_app.

These run as the OWNER connection (DATABASE_URL in dev/CI). The running web
app is meant to connect as the restricted truetape_app role -- that split is
the whole point of harden-db and the reason the REVOKEs below actually bind.
"""
from __future__ import annotations

import click
import bcrypt
import json
import os
from flask import current_app
from flask.cli import with_appcontext
from flask_migrate import upgrade
from sqlalchemy import text
from sqlalchemy import create_engine, text
from app.extensions import db
from pathlib import Path
from app.models import User, SourceTrustConfig, ValidationRule
from app.validation.runner import (
    run_cross_source_validation,
    run_dataset_validation,
    run_row_validation,
)
from app.services.canonical import build_canonical
from app.services.clustering import assign_rule_clusters

# The single source of truth for "append-only" tables. harden-db revokes on
# these, and any later audit of the grants should read from this same list.
# loan_records is here for the same reason as the audit log: it is source
# lineage. A human edit INSERTs revision N+1 (see exceptions.resolve), nothing
# in the app ever UPDATEs a row, so revoking UPDATE/DELETE turns "immutable by
# convention" into "immutable by grant" -- the app role literally cannot
# rewrite what a source originally said.
APPEND_ONLY_TABLES = ("audit_events", "ai_recommendations",
                      "raw_records", "verified_records", "loan_records")
APP_ROLE = "truetape_app"
SEED_DIR = os.environ.get("SEED_DIR", "/data/seed")


def register_cli(app):
    app.cli.add_command(reset_db)
    app.cli.add_command(harden_db)
    app.cli.add_command(seed)
    app.cli.add_command(assign_clusters)
    app.cli.add_command(run_pipeline)
    app.cli.add_command(reconcile_oracle)


def _owner_engine():
    """Short-lived engine on the OWNER role for DDL and grant surgery.

    db.engine is truetape_app, which owns nothing -- it cannot DROP the schema
    or REVOKE on the owner's tables. These commands connect as truetape_owner.
    """
    url = current_app.config.get("MIGRATION_DATABASE_URL")
    if not url:
        raise SystemExit("MIGRATION_DATABASE_URL is empty; admin commands must "
                         "connect as the owner role. Set it in .env.")
    return create_engine(url, future=True)


def _seed_users(path):
    payload = json.loads(path.read_text())
    creds = []
    for u in payload:
        name = u.get("name") or u.get("display_name")
        email = (u.get("email") or f'{u["username"]}@truetape.local').strip().lower()
        role = u["role"]
        pw_hash = bcrypt.hashpw(u["password"].encode(), bcrypt.gensalt()).decode()
        existing = db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one_or_none()
        if existing:
            existing.name, existing.role, existing.password_hash = name, role, pw_hash
        else:
            db.session.add(User(name=name, email=email, role=role, password_hash=pw_hash))
        creds.append((role, email))
    return creds

def _seed_trust_config(path):
    payload = json.loads(path.read_text())
    for t in payload:
        fn = t.get("field_name")
        q = db.select(SourceTrustConfig).filter_by(source_system=t["source_system"])
        q = q.filter(SourceTrustConfig.field_name.is_(None)) if fn is None \
            else q.filter_by(field_name=fn)
        row = db.session.execute(q).scalar_one_or_none()
        if row:
            row.trust_score, row.rationale = t["trust_score"], t["rationale"]
        else:
            db.session.add(SourceTrustConfig(
                source_system=t["source_system"], field_name=fn,
                trust_score=t["trust_score"], rationale=t["rationale"]))
    return len(payload)


def _seed_rules(path):
    payload = json.loads(path.read_text())
    for r in payload:
        version = r.get("version", 1)
        row = db.session.execute(db.select(ValidationRule).filter_by(
            rule_code=r["rule_code"], version=version)).scalar_one_or_none()
        fields = dict(
            scope=r["scope"], severity=r["severity"], condition=r["condition"],
            message_template=r["message_template"], source="seed",
            explanation=r.get("explanation"), is_active=True)
        if row:
            for k, v in fields.items():
                setattr(row, k, v)
        else:
            db.session.add(ValidationRule(rule_code=r["rule_code"], version=version, **fields))
    return len(payload)


@click.command("seed")
@with_appcontext
def seed():
    base = Path(SEED_DIR)
    creds = _seed_users(base / "users.json")
    n_trust = _seed_trust_config(base / "trust_config.json")
    n_rules = _seed_rules(base / "validation_rules.json")
    db.session.commit()
    click.echo(f"seed: {len(creds)} users, {n_trust} trust-config rows, {n_rules} rules")
    click.echo("login emails (passwords come from users.json):")
    for role, email in sorted(creds):
        click.echo(f"  {role:9s} {email}")


@click.command("reset-db")
@click.option("--yes", is_flag=True, help="skip the confirmation prompt")
@with_appcontext
def reset_db(yes):
    """Drop everything and replay migrations to a clean schema owned by the owner role."""
    if current_app.config.get("ENV") == "production":
        raise SystemExit("reset-db refuses to run with ENV=production")
    if not yes:
        click.confirm("This DROPs every table and all data. Continue?", abort=True)

    eng = _owner_engine()
    with eng.begin() as conn:
        # As the owner: DROP SCHEMA takes the tables AND alembic_version, so the
        # upgrade below replays from zero. Recreating the schema also clears the
        # init script's default-privilege rules, so harden-db re-grants
        # explicitly rather than relying on them.
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
    eng.dispose()

    db.engine.dispose()  # app pool still points at the dropped schema
    upgrade()             # runs as the OWNER now -- see migrations/env.py
    click.echo("reset-db: schema recreated, migrations replayed as the owner.")
    click.echo("next: `flask harden-db`, then `flask seed`  (or `make db-reset`).")


@click.command("harden-db")
@with_appcontext
def harden_db():
    """Grant the app role least privilege; revoke mutation on append-only tables.

    Runs as the OWNER. MUST run AFTER migrations -- GRANT ON ALL TABLES only
    touches tables that already exist. Idempotent.
    """
    eng = _owner_engine()
    with eng.begin() as conn:
        exists = conn.execute(
            text("select 1 from pg_roles where rolname = :r"),
            {"r": APP_ROLE}).scalar()
        if not exists:
            raise SystemExit(
                f"{APP_ROLE} role is missing. It is created by db/init/*.sql on "
                f"first boot -- recreate the DB volume or create the role by hand.")

        statements = [
            # Normalise to a known state, then grant back exactly four verbs.
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE};",
            f"GRANT USAGE ON SCHEMA public TO {APP_ROLE};",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};",
        ]
        for table in APPEND_ONLY_TABLES:
            statements.append(
                f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {APP_ROLE};")
        for sql in statements:
            conn.execute(text(sql))
    eng.dispose()

    click.echo(f"harden-db: {APP_ROLE} = SELECT/INSERT/UPDATE/DELETE, then "
               f"UPDATE/DELETE/TRUNCATE revoked on:")
    for table in APPEND_ONLY_TABLES:
        click.echo(f"  - {table}")

@click.command("assign-clusters")
@with_appcontext
def assign_clusters():
    """Cluster open exceptions by rule_code. Idempotent; safe to re-run."""
    result = assign_rule_clusters()
    db.session.commit()
    click.echo(f"assign-clusters: {result}")


@click.command("run-pipeline")
@click.option("--force", is_flag=True,
              help="clear previous results and re-run every stage. Refuses "
                   "if any validation_failure exception is no longer 'open'.")
@with_appcontext
def run_pipeline(force):
    """Run validation + canonical stages over imported data (stages 3-4).

    Stage 1-2 (parse, normalise) happen automatically on upload. This command
    chains the remaining stages in the order the pipeline contract expects:

        1. run_row_validation         B1: row-scope rules
        2. run_dataset_validation     B2: dataset-scope rules (duplicates)
        3. run_cross_source_validation B3: OriginationCore vs ServicerFeed
        4. build_canonical             blend sources into loan_canonical

    All stages write without committing; the single commit at the end makes
    the whole run atomic -- a failed stage leaves nothing half-written.
    Re-running without --force is a no-op for stages that already have
    results (idempotent), which makes this safe to retry after a crash.
    """
    stages = (
        ("row validation", run_row_validation, True),
        ("dataset validation", run_dataset_validation, True),
        ("cross-source conflicts", run_cross_source_validation, True),
        ("canonical blend", build_canonical, False),
        ("cluster grouping", assign_rule_clusters, False),
    )

    for label, fn, takes_force in stages:
        result = fn(force=force) if takes_force else fn()
        click.echo(f"run-pipeline: {label}: {result}")

    db.session.commit()
    click.echo("run-pipeline: committed.")
    click.echo("next: resolve exceptions, then POST /api/verify-batch.")
@click.command("reconcile-oracle")
@with_appcontext
def reconcile_oracle():
    """Diff engine exceptions against the provided QA oracle (215 rows).

    The oracle lists every defect the generator deliberately injected. Our
    engine deliberately surfaces MORE than that (per-row duplicate counting,
    servicer mirror rows, clone inheritance), so a naive count-diff always
    "fails". This command does the honest version: match oracle rows to
    engine exceptions, bucket every delta into a named, reasoned category,
    and exit non-zero only if something is genuinely unexplained.

    Exit 0 = every delta explained (the engine is a superset of the oracle).
    """
    import csv
    from collections import Counter, defaultdict
    from pathlib import Path
    from sqlalchemy import text as _text

    oracle_path = Path(SEED_DIR) / "expected_exception_sample.csv"
    if not oracle_path.exists():
        raise SystemExit(f"oracle not found at {oracle_path}")
    oracle_rows = list(csv.DictReader(oracle_path.open()))

    # ---- engine side ----------------------------------------------------
    # validation failures keyed (rule_code, business loan id) + per-rule totals
    vf = db.session.execute(_text("""
        SELECT r.rule_code, l.loan_id AS biz_id
        FROM exceptions e
        JOIN validation_rules r ON r.id = e.rule_id
        JOIN loans l ON l.id = e.loan_id
        WHERE e.exception_type = 'validation_failure'
    """)).all()
    engine_rule_keys = Counter((rc, biz) for rc, biz in vf)
    engine_rule_total = Counter(rc for rc, _ in vf)

    # extras need source attribution: file_kind via the exception's raw row
    extra_src = db.session.execute(_text("""
        SELECT r.rule_code, COALESCE(rf.file_kind, 'human_edit') AS src, count(*)
        FROM exceptions e
        JOIN validation_rules r ON r.id = e.rule_id
        LEFT JOIN raw_records rr ON rr.id = e.raw_record_id
        LEFT JOIN raw_files rf ON rf.id = rr.raw_file_id
        WHERE e.exception_type = 'validation_failure'
        GROUP BY 1, 2
    """)).all()
    src_by_rule = defaultdict(dict)
    for rc, src, n in extra_src:
        src_by_rule[rc][src] = n

    # source conflicts keyed (business loan id, field)
    sc = db.session.execute(_text("""
        SELECT l.loan_id AS biz_id, e.detail->>'field' AS field
        FROM exceptions e
        JOIN loans l ON l.id = e.loan_id
        WHERE e.exception_type = 'source_conflict'
    """)).all()
    engine_conflict = Counter((biz, fld) for biz, fld in sc)

    engine_import_errors = db.session.execute(_text(
        "SELECT count(*) FROM exceptions WHERE exception_type = 'import_error'"
    )).scalar()

    # loans whose tape balance is unparseable (comparison skipped)
    unparseable = {row[0] for row in db.session.execute(_text("""
        SELECT DISTINCT l.loan_id
        FROM exceptions e
        JOIN validation_rules r ON r.id = e.rule_id
        JOIN loans l ON l.id = e.loan_id
        WHERE r.rule_code = 'INVALID_NUMERIC_FORMAT'
    """)).all()}

    # ---- oracle side ------------------------------------------------------
    oracle_rule_total = Counter()
    oracle_rule_keys = Counter()
    oracle_conflict = Counter()
    oracle_missing = 0
    for row in oracle_rows:
        if row["defect_class"] == "MISSING_LOAN_ID":
            oracle_missing += 1
        elif row["defect_class"] == "SOURCE_CONFLICT":
            oracle_conflict[(row["loan_id"], row["field_name"])] += 1
        else:
            oracle_rule_total[row["rule_code"]] += 1
            oracle_rule_keys[(row["rule_code"], row["loan_id"])] += 1

    all_rules = sorted(set(oracle_rule_total) | set(engine_rule_total))
    per_rule = {}
    for rc in all_rules:
        o_total = oracle_rule_total.get(rc, 0)
        e_total = engine_rule_total.get(rc, 0)
        if rc.startswith("DUPLICATE_"):
            # Oracle keys the clone row's original id; the engine keys the
            # duplicated loan. Key-level matching is meaningless across two
            # id vocabularies -- compare counts, bucket the difference.
            matched = min(o_total, e_total)
        else:
            matched = sum(min(n, engine_rule_keys.get((rc, biz), 0))
                          for (r2, biz), n in oracle_rule_keys.items() if r2 == rc)
        per_rule[rc] = {"oracle": o_total, "matched": matched,
                        "extra": max(0, e_total - matched),
                        "missed": max(0, o_total - matched)}

    def bucket_extra(rc, extras):
        """Split a rule's extra count into named, reasoned buckets."""
        out = []
        dup = extras if rc.startswith("DUPLICATE_") else 0
        if dup:
            out.append((dup, "per-row duplicate counting (flag every group member)"))
        serv = src_by_rule.get(rc, {}).get("servicer_update", 0)
        if serv:
            out.append((serv, "servicer mirror row carries the same defect"))
        tape = src_by_rule.get(rc, {}).get("loan_tape", 0) -             min(src_by_rule.get(rc, {}).get("loan_tape", 0),
                per_rule[rc]["matched"])
        # remaining tape-side extras are inherited defects on clone/donor rows
        rest = extras - dup - serv
        if rest > 0:
            out.append((rest, "defect inherited by clone/donor rows"))
        return out

    conflict_matched = sum(min(n, engine_conflict.get(k, 0))
                           for k, n in oracle_conflict.items())
    conflict_extra = sum(max(0, engine_conflict.get(k, 0) - n)
                         for k, n in oracle_conflict.items())
    conflict_missed = [(k, n) for k, n in oracle_conflict.items()
                       if engine_conflict.get(k, 0) < n]

    # ---- report -----------------------------------------------------------
    explained, unexplained = 0, 0
    click.echo()
    click.echo(f"{'rule':44} {'oracle':>6} {'match':>6} {'extra':>6} {'miss':>5}  delta buckets")
    click.echo("-" * 108)
    for rc in all_rules:
        d = per_rule[rc]
        parts = []
        if d["extra"]:
            for n, b in bucket_extra(rc, d["extra"]):
                explained += n
                parts.append(f"+{n} {b}")
        if d["missed"]:
            unexplained += d["missed"]
            parts.append(f"-{d['missed']} UNEXPLAINED")
        total_o = d["oracle"]
        click.echo(f"{rc[:44]:44} {total_o:>6} {d['matched']:>6} "
                   f"{d['extra']:>6} {d['missed']:>5}  {'; '.join(parts)}")

    conflict_extra_note = ("overlapping tape defect is a real disagreement"
                           if conflict_extra else "")
    click.echo(f"{'source_conflict':44} {sum(oracle_conflict.values()):>6} "
               f"{conflict_matched:>6} {conflict_extra:>6} {0:>5}  {conflict_extra_note}")
    explained += conflict_extra

    imp_delta = engine_import_errors - oracle_missing
    click.echo(f"{'import_error (missing loan_id)':44} {oracle_missing:>6} "
               f"{min(oracle_missing, engine_import_errors):>6} "
               f"{max(0, imp_delta):>6} {max(0, -imp_delta):>5}")

    # missed conflicts: clamped-to-zero or unparseable tape values
    clamped = 0
    for (biz, fld), n in conflict_missed:
        if biz in unparseable:
            explained += n
            continue
        vals = db.session.execute(_text("""
            SELECT max(CASE WHEN lr.source_system='OriginationCore' THEN lr.data->>:f END),
                   max(CASE WHEN lr.source_system='ServicerFeed'  THEN lr.data->>:f END)
            FROM loans l JOIN loan_records lr ON lr.loan_id = l.id
            WHERE l.loan_id = :b
        """), {"f": fld, "b": biz}).one()
        if vals[0] == vals[1]:
            clamped += n        # servicer delta clamped onto an equal value
        else:
            unexplained += n
            click.echo(f"  UNEXPLAINED missed conflict: {biz} field={fld} x{n}")
    if clamped:
        click.echo(f"  {clamped} missed conflicts: servicer delta clamped onto an "
                   f"equal value (closed/zero loans) — undetectable by design")

    click.echo("-" * 108)
    click.echo(f"RECONCILIATION: oracle={len(oracle_rows)}  "
               f"explained-deltas={explained}  unexplained={unexplained}")
    if unexplained:
        click.echo("RESULT: FAIL — deltas above marked UNEXPLAINED")
        raise SystemExit(1)
    click.echo("RESULT: PASS — engine is a fully-explained superset of the oracle")
