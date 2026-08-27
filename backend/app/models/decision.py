"""reviewer_decisions -- the ONLY write path to canonical data.

Nothing else in the system may mutate loan_canonical. The AI writes
recommendations; a human writes decisions; decisions write canonical values.
That single-writer rule is what makes the whole "AI-assisted, human-decided"
claim checkable rather than rhetorical.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import DECISION_ACTIONS, DECISION_SCOPES, one_of


class ReviewerDecision(db.Model):
    __tablename__ = "reviewer_decisions"
    __table_args__ = (
        sa.CheckConstraint(one_of("decision_scope", DECISION_SCOPES),
                           name="decision_scope_valid"),
        sa.CheckConstraint(one_of("action", DECISION_ACTIONS),
                           name="action_valid"),
        # Scope-dependent, not a simple OR: an exception-scope decision without
        # an exception_id, or a loan-scope decision without a loan_id, is a row
        # nobody can interpret six weeks later.
        sa.CheckConstraint(
            "(decision_scope = 'exception' AND exception_id IS NOT NULL) OR "
            "(decision_scope = 'loan'      AND loan_id      IS NOT NULL)",
            name="decision_has_subject"),
        sa.Index("ix_reviewer_decisions_exception_id", "exception_id"),
        sa.Index("ix_reviewer_decisions_batch_id", "batch_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    exception_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("exceptions.id"))
    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("loans.id"))
    decision_scope: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="exception")

    # Nullable: a reviewer may resolve an exception the AI never touched, and
    # NULL here is the honest record of "no AI involvement".
    ai_recommendation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("ai_recommendations.id"))
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    request_correction: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false())

    # A LIST, not a single field/value pair. Resolving one exception routinely
    # corrects two fields at once -- a date and the status that depends on it --
    # and a scalar pair could record only the first. The second edit would have
    # been unrepresentable and therefore missing from the audit trail.
    # [{"field": ..., "before": ..., "after": ..., "source_used": ...}]
    changes: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"))

    # Cheap to capture, and it is the single most useful number in the write-up:
    # the human-agreement rate on AI suggestions.
    agreed_with_ai: Mapped[Optional[bool]] = mapped_column(sa.Boolean)
    comment: Mapped[Optional[str]] = mapped_column(sa.Text)
    # Set when this decision was one of N in a cluster batch resolve, so a
    # 40-row bulk action stays attributable row by row.
    batch_id: Mapped[Optional[uuid.UUID]] = mapped_column(sa.Uuid)
    decided_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Decision {self.action} by={self.reviewer_id}>"