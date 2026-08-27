"""verified_records -- the output artefact, hash-chained per loan.

Immutable: UPDATE and DELETE revoked for the app role. A correction after
verification produces version N+1, it does not edit version N.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class VerifiedRecord(db.Model):
    __tablename__ = "verified_records"
    __table_args__ = (
        sa.CheckConstraint("trust_score BETWEEN 0 AND 100",
                           name="trust_score_range"),
        sa.UniqueConstraint("loan_id", "version", name="loan_version_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    loan_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("loans.id"), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    canonical_data: Mapped[dict] = mapped_column(JSONB, nullable=False)
    field_provenance: Mapped[dict] = mapped_column(JSONB, nullable=False)
    validation_summary: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # field_provenance names source *systems*; the brief asks for a source
    # FILE reference. A verified record is typically built from two or three
    # files, hence an array, each entry carrying that file's SHA-256 so the
    # lineage is provable from this row alone without joining back through
    # loan_records to raw_files.
    source_files: Mapped[list] = mapped_column(JSONB, nullable=False)

    decision_ids: Mapped[Optional[list]] = mapped_column(JSONB)
    ai_recommendation_ids: Mapped[Optional[list]] = mapped_column(JSONB)

    trust_score: Mapped[float] = mapped_column(
        sa.Numeric(5, 2, asdecimal=False), nullable=False)
    trust_score_breakdown: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Per-loan chain. The same fork race as audit_events, so the append path
    # takes pg_advisory_xact_lock keyed by loan id -- a global lock here would
    # needlessly serialise verification across unrelated loans.
    record_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prev_record_hash: Mapped[Optional[str]] = mapped_column(sa.Text)

    verified_by: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Verified {self.loan_id} v{self.version} trust={self.trust_score}>"