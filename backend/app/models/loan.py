"""loans / loan_records / loan_canonical -- the canonical spine.

Read the naming trap before you write any query against these tables:

    loans.loan_id       TEXT   the business key, e.g. "LN-0001042"
    loan_records.loan_id UUID  a foreign key to loans.id

Same column name, two different types, two different meanings. The convention
that keeps this out of your bug list: the HTTP layer speaks the TEXT business
key (that is what a reviewer reads off a screen), and everything below the
service boundary speaks UUIDs. Resolve once, at the top of the request, and
never pass an unannotated `loan_id` between functions.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import LOAN_STATUSES, RECORD_ORIGINS, one_of


class Loan(db.Model):
    __tablename__ = "loans"
    __table_args__ = (
        sa.CheckConstraint(one_of("status", LOAN_STATUSES), name="status_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    borrower_id: Mapped[Optional[str]] = mapped_column(sa.Text)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="ingested")

    # The concurrency token. SQLAlchemy owns this column -- do NOT assign to it.
    # With version_id_col set, every ORM UPDATE is emitted as
    #   UPDATE loans SET ..., version = 3 WHERE id = ... AND version = 2
    # and a zero-row result raises StaleDataError instead of silently winning.
    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="1")

    # onupdate= is what the DDL's AUDIT FIX comment is about: server_default
    # fires on INSERT only, so without onupdate this column would say the loan
    # was last touched at import time forever.
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False,
        server_default=sa.func.now(), onupdate=sa.func.now())

    # Cardinality is the whole question with collections. A loan has at most
    # three imported records plus a handful of human edits, so loading them all
    # is fine -- unlike RawFile.records, which would be 5,000 rows and is
    # therefore deliberately not defined.
    records: Mapped[list["LoanRecord"]] = relationship(
        back_populates="loan", lazy="select")
    canonical: Mapped[Optional["LoanCanonical"]] = relationship(
        back_populates="loan", uselist=False, lazy="select")

    __mapper_args__ = {"version_id_col": version}

    def __repr__(self) -> str:
        return f"<Loan {self.loan_id} {self.status} v{self.version}>"


class LoanRecord(db.Model):
    """One source's view of one loan, at one revision. Append-only.

    A human edit does not mutate a row here -- it INSERTs version N+1 with
    origin='human_edit'. That is what makes "what did ServicerX originally
    say" a one-line query instead of an archaeology exercise, and it is why
    the UNIQUE below includes version.
    """
    __tablename__ = "loan_records"
    __table_args__ = (
        sa.CheckConstraint(one_of("origin", RECORD_ORIGINS), name="origin_valid"),
        sa.UniqueConstraint("loan_id", "source_system", "version",
                            name="revision_unique_per_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("loans.id"), nullable=False)
    raw_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("raw_records.id"))
    source_system: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # NOT a concurrency token, despite sharing a name with loans.version. This
    # is a revision number and part of the unique key above; version_id_col is
    # deliberately not set on this mapper. Conflating the two would make every
    # append look like a lost update.
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Per-field coercion failures, e.g. {"origination_date": "unparseable date
    # '13/45/2024'"}. Kept per field rather than as one row-level flag because
    # one bad cell must not discard the other twenty good ones.
    field_errors: Mapped[Optional[dict]] = mapped_column(JSONB)

    effective_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    origin: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="import")

    loan: Mapped["Loan"] = relationship(back_populates="records", lazy="select")

    def __repr__(self) -> str:
        return f"<LoanRecord {self.source_system} v{self.version} {self.origin}>"


class LoanCanonical(db.Model):
    """The per-field blend across sources. Exactly one live row per loan.

    Shared primary key with loans -- loan_id is both PK and FK, so the
    one-to-one is enforced by the schema and not by convention. There is no
    version column: this table holds the current answer, and its history lives
    in loan_records revisions and verified_records versions.
    """
    __tablename__ = "loan_canonical"

    loan_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("loans.id"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Why every field won, in the field's own words. This is the difference
    # between a demo that claims lineage and one that shows it.
    field_provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Fields a human has decided. The survivorship engine SKIPS these instead
    # of re-deriving them, so tomorrow's import cannot quietly revert today's
    # reviewer. Empty dict rather than NULL so the engine never branches on None.
    pinned_fields: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))

    computed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    loan: Mapped["Loan"] = relationship(back_populates="canonical", lazy="select")

    def __repr__(self) -> str:
        return f"<LoanCanonical {self.loan_id} {len(self.pinned_fields or {})} pinned>"