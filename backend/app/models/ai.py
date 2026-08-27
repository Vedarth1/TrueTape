"""ai_recommendations -- immutable, and with no write path to canonical data.

One wide table for all five action types rather than five narrow tables. The
sparse columns are the cost; the benefit is that every recommendation shares
one provenance envelope (model, prompt_version, evidence_bundle, latency,
confidence) and "show me everything the AI said about this exception" is one
indexed query instead of a five-way UNION. Sparse NULLs are cheap in Postgres;
a five-way UNION in a demo is not.

UPDATE and DELETE are revoked on this table for the app role. The model's
output is evidence, and evidence you can edit is not evidence.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import AI_ACTION_TYPES, SEVERITIES, one_of


class AiRecommendation(db.Model):
    __tablename__ = "ai_recommendations"
    __table_args__ = (
        sa.CheckConstraint(one_of("action_type", AI_ACTION_TYPES),
                           name="action_type_valid"),
        sa.CheckConstraint(
            f"suggested_severity IS NULL OR "
            f"{one_of('suggested_severity', SEVERITIES)}",
            name="suggested_severity_valid"),
        # Not in the DDL; added because a confidence of 47 instead of 0.47 is
        # exactly the kind of unit slip that silently poisons every threshold
        # downstream, and NUMERIC(4,3) would happily store it.
        sa.CheckConstraint("confidence IS NULL OR confidence BETWEEN 0 AND 1",
                           name="confidence_range"),
        sa.Index("ix_ai_recommendations_exception_id", "exception_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    action_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    exception_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("exceptions.id"))
    cluster_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("exception_clusters.id"))

    # Provenance envelope: shared by all five action types, all NOT NULL.
    # 'provider' records what actually served the request, which matters
    # because the provider degrades to the deterministic stub on failure and
    # the record must say so rather than implying a model call happened.
    model_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prompt_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Exactly what the model saw. Without this, "the AI hallucinated" and "we
    # fed it the wrong evidence" are indistinguishable after the fact.
    evidence_bundle: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # explain_failure
    problem: Mapped[Optional[str]] = mapped_column(sa.Text)
    evidence: Mapped[Optional[dict]] = mapped_column(JSONB)
    # CAREFUL: this is the reviewer-facing explanation, parsed out of
    # json.loads(message["content"])["reasoning"]. It is NOT message["reasoning"],
    # which is the provider's chain-of-thought and belongs in raw_model_response.
    # Same key, two meanings -- this collision already cost us once.
    reasoning: Mapped[Optional[str]] = mapped_column(sa.Text)

    # suggest_correction
    suggested_field: Mapped[Optional[str]] = mapped_column(sa.Text)
    suggested_value: Mapped[Optional[str]] = mapped_column(sa.Text)
    suggested_source: Mapped[Optional[str]] = mapped_column(sa.Text)
    # classify_severity / reviewer_note / batch_summary
    suggested_severity: Mapped[Optional[str]] = mapped_column(sa.Text)
    note_text: Mapped[Optional[str]] = mapped_column(sa.Text)
    summary_text: Mapped[Optional[str]] = mapped_column(sa.Text)

    # asdecimal=False so Python gets a float. NUMERIC gives exact storage and
    # lets the CHECK above work, but Flask's JSON provider does not serialise
    # decimal.Decimal -- jsonify would raise TypeError. Money would justify a
    # custom provider; a confidence score does not.
    confidence: Mapped[Optional[float]] = mapped_column(
        sa.Numeric(4, 3, asdecimal=False))
    # Decomposed so the UI shows WHY confidence is 0.62, not just that it is.
    confidence_breakdown: Mapped[Optional[dict]] = mapped_column(JSONB)

    raw_model_response: Mapped[Optional[dict]] = mapped_column(JSONB)
    latency_ms: Mapped[Optional[int]] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<AiRec {self.action_type} conf={self.confidence}>"