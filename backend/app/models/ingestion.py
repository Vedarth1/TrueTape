"""raw_files / raw_records -- the immutable landing zone.

Nothing in here is ever rewritten. raw_records.raw_payload is the byte-faithful
original row, and it is the thing that makes field-level lineage provable
rather than merely asserted: every canonical value traces back to the exact
JSON it was read from. Both tables have UPDATE and DELETE revoked from the
truetape_app role -- see `flask harden-db` in A3.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import FILE_KINDS, FILE_STATUSES, one_of


class RawFile(db.Model):
    __tablename__ = "raw_files"
    __table_args__ = (
        sa.CheckConstraint(one_of("file_kind", FILE_KINDS), name="file_kind_valid"),
        sa.CheckConstraint(one_of("status", FILE_STATUSES), name="status_valid"),
        sa.Index("ix_raw_files_file_hash", "file_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    file_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_system: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # Not unique. The same file may legitimately be re-uploaded after a
    # correction upstream; the 409 is an application decision the caller can
    # override with ?force=true, so it must not be a schema decision. Indexed
    # because that duplicate check runs on every upload.
    file_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(sa.Text, nullable=False)

    uploaded_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    row_count: Mapped[Optional[int]] = mapped_column(sa.Integer)
    parsed_count: Mapped[Optional[int]] = mapped_column(sa.Integer)
    failed_count: Mapped[Optional[int]] = mapped_column(sa.Integer)

    # Written by the normalizer: header-as-read -> canonical field name. This
    # is what the UI's mapping-review panel renders, and it is the audit trail
    # for "why did this column become interest_rate".
    column_mapping: Mapped[Optional[dict]] = mapped_column(JSONB)
    unmapped_columns: Mapped[Optional[list]] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(sa.Text, nullable=False)

    @property
    def progress_pct(self) -> int:
        """Derived, never stored -- a stored copy can disagree with the counts."""
        if not self.row_count:
            return 0
        done = (self.parsed_count or 0) + (self.failed_count or 0)
        return min(100, round(done * 100 / self.row_count))

    def __repr__(self) -> str:
        return f"<RawFile {self.filename} {self.status}>"


class RawRecord(db.Model):
    __tablename__ = "raw_records"
    __table_args__ = (
        # Makes a re-run of the importer over a half-finished file fail loudly
        # instead of silently doubling the tape. The unique index also serves
        # as the raw_file_id index, so there is no second index to maintain.
        sa.UniqueConstraint("raw_file_id", "row_number",
                            name="row_unique_per_file"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    raw_file_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("raw_files.id"), nullable=False)
    row_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # Byte-faithful. Every value is the string as read -- no coercion, no
    # trimming, no null-ing of blanks. The moment you clean here you have
    # destroyed the evidence that the data was dirty.
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    parse_error: Mapped[Optional[str]] = mapped_column(sa.Text)

    file: Mapped["RawFile"] = relationship(lazy="select")

    def __repr__(self) -> str:
        return f"<RawRecord {self.raw_file_id}#{self.row_number}>"