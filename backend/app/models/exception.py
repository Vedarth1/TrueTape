"""exception_clusters / exceptions / exception_comments.

Note the class name: ExceptionRecord, not Exception. `class Exception(db.Model)`
would shadow the builtin inside this module, and then `except Exception:`
anywhere below it would be catching a SQLAlchemy model -- a bug that presents
as "my error handler stopped working" and takes an hour to find.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import (EXCEPTION_STATUSES, EXCEPTION_TYPES, SEVERITIES,
                              one_of)


class ExceptionCluster(db.Model):
    """A group of exceptions sharing one root cause, so 40 rows resolve as one."""
    __tablename__ = "exception_clusters"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    cluster_label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # The signal that defined the group, e.g. {"rule_code": "INVALID_DATE_FORMAT",
    # "source_system": "ServicerX", "pattern": "DD/MM/YYYY"}. Stored so the
    # cluster is explainable rather than an opaque bucket.
    root_cause_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Denormalised counter. Invariant: ONLY the clustering pass writes this.
    # Never increment it from a request handler -- a counter with two writers
    # drifts, and a wrong count on the batch-resolve button is a visible bug.
    member_count: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Cluster {self.cluster_label} n={self.member_count}>"


class ExceptionRecord(db.Model):
    __tablename__ = "exceptions"
    __table_args__ = (
        # At-least-one, deliberately NOT exactly-one. The prose in the DDL says
        # "exactly one", and that prose is wrong: a row-scope validation failure
        # legitimately carries both -- the loan it belongs to AND the raw row it
        # came from, which is what makes lineage clickable. Do not tighten this
        # to an XOR to match the comment.
        sa.CheckConstraint("loan_id IS NOT NULL OR raw_record_id IS NOT NULL",
                           name="exception_has_subject"),
        sa.CheckConstraint(one_of("exception_type", EXCEPTION_TYPES),
                           name="exception_type_valid"),
        sa.CheckConstraint(one_of("severity", SEVERITIES), name="severity_valid"),
        # Same value list as severity, on purpose: the two are compared directly
        # to compute AI/human agreement, and drifting vocabularies would make
        # that comparison meaningless.
        sa.CheckConstraint(
            f"ai_severity IS NULL OR {one_of('ai_severity', SEVERITIES)}",
            name="ai_severity_valid"),
        sa.CheckConstraint(one_of("status", EXCEPTION_STATUSES),
                           name="status_valid"),
        sa.Index("ix_exceptions_queue", "status", "severity", "cluster_id"),
        sa.Index("ix_exceptions_raw_file_id", "raw_file_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)

    # NULLABLE, and this is load-bearing. The first defect the brief lists is a
    # missing loan_id; such a row can never become a `loans` row, so NOT NULL
    # here would make the most obvious defect in the dataset structurally
    # impossible to record.
    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("loans.id"))
    raw_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("raw_records.id"))
    raw_file_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("raw_files.id"))

    exception_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rule_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("validation_rules.id"))
    field_name: Mapped[Optional[str]] = mapped_column(sa.Text)

    severity: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # The AI's independent call, stored beside the rule's own severity rather
    # than overwriting it. Keeping both is what lets you report "the model
    # agreed with the rule on 84% of exceptions" -- a number you cannot compute
    # if the AI writes into the same column.
    ai_severity: Mapped[Optional[str]] = mapped_column(sa.Text)

    cluster_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("exception_clusters.id"))
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="open")
    # Gates verification: a loan with an open blocking exception cannot be
    # approved. Default TRUE so a new exception type is safe-by-default.
    is_blocking: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true())
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("users.id"))
    detail: Mapped[Optional[dict]] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        sa.DateTime(timezone=True))

    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False,
        server_default=sa.func.now(), onupdate=sa.func.now())

    rule: Mapped[Optional["ValidationRule"]] = relationship(lazy="select")  # noqa: F821
    cluster: Mapped[Optional["ExceptionCluster"]] = relationship(lazy="select")

    # Two reviewers opening the same exception from the queue is THE race in
    # this app, not a theoretical one. Same treatment as Loan.
    __mapper_args__ = {"version_id_col": version}

    def __repr__(self) -> str:
        return f"<Exception {self.exception_type} {self.severity} {self.status}>"


class ExceptionComment(db.Model):
    """Comments independent of decisions -- a reviewer can discuss without deciding."""
    __tablename__ = "exception_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    exception_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exceptions.id"), nullable=False)
    author_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False)
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # TRUE when the text was seeded by generate_reviewer_note. The author is
    # still the human who posted it -- the flag makes the AI's contribution
    # visible in the thread instead of laundering it as human prose.
    ai_drafted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false())
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Comment {self.exception_id} ai={self.ai_drafted}>"