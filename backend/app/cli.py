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
from app.models import User

# The single source of truth for "append-only" tables. harden-db revokes on
# these, and any later audit of the grants should read from this same list.
APPEND_ONLY_TABLES = ("audit_events", "ai_recommendations",
                      "raw_records", "verified_records")
APP_ROLE = "truetape_app"
SEED_DIR = os.environ.get("SEED_DIR", "/data/seed")


def register_cli(app):
    app.cli.add_command(reset_db)
    app.cli.add_command(harden_db)
    app.cli.add_command(seed)


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


def _seed_users(path: Path) -> int:
    payload = json.loads(path.read_text())
    for u in payload:
        email = u["email"].strip().lower()
        pw_hash = bcrypt.hashpw(u["password"].encode(), bcrypt.gensalt()).decode()
        existing = db.session.execute(
            db.select(User).filter_by(email=email)).scalar_one_or_none()
        if existing:
            existing.name, existing.role, existing.password_hash = \
                u["name"], u["role"], pw_hash
        else:
            db.session.add(User(name=u["name"], email=email,
                                role=u["role"], password_hash=pw_hash))
    return len(payload)


@click.command("seed")
@with_appcontext
def seed():
    """Idempotent seed. Runs as the app role — users/trust/rules are freely
    writable; only the four append-only tables are revoked. (Trust config and
    the 18 rules load here next.)"""
    n_users = _seed_users(Path(SEED_DIR) / "users.json")
    db.session.commit()
    click.echo(f"seed: {n_users} users upserted from {SEED_DIR}/users.json")


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