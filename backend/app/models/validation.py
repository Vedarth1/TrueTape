"""validation_rules / validation_results.

Rules are versioned, never edited in place. Changing a rule inserts version
N+1 and flips version N to is_active=false, because a validation_result
written last Tuesday has to stay interpretable -- if you mutate the rule the
result was produced by, your audit trail now cites a rule that never ran.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.extensions import db
from app.models.enums import (RESULT_STATES, RULE_SCOPES, RULE_SOURCES,
                              SEVERITIES, one_of)


class ValidationRule(db.Model):
    __tablename__ = "validation_rules"
    __table_args__ = (
        sa.CheckConstraint(one_of("scope", RULE_SCOPES), name="scope_valid"),
        sa.CheckConstraint(one_of("severity", SEVERITIES), name="severity_valid"),
        sa.CheckConstraint(one_of("source", RULE_SOURCES), name="source_valid"),
        sa.UniqueConstraint("rule_code", "version", name="code_version_unique"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    rule_code: Mapped[str] = mapped_column(sa.Text, nullable=False)
    version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default="1")
    scope: Mapped[str] = mapped_column(sa.Text, nullable=False)
    severity: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # The DSL expression tree for scope='row', or the check spec for
    # scope='dataset'. JSONB rather than a string of Python: nothing in this
    # column is ever eval'd, which is the entire security argument for the
    # AI-authored-rule feature.
    condition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    message_template: Mapped[str] = mapped_column(sa.Text, nullable=False)

    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # The reviewer's English sentence, for AI-generated rules. Kept so the UI
    # can show "you asked for X, here is the tree we compiled" side by side.
    natural_language_source: Mapped[Optional[str]] = mapped_column(sa.Text)
    explanation: Mapped[Optional[str]] = mapped_column(sa.Text)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.true())

    def __repr__(self) -> str:
        return f"<Rule {self.rule_code} v{self.version} {self.severity}>"


class ValidationResult(db.Model):
    __tablename__ = "validation_results"
    __table_args__ = (
        sa.CheckConstraint(one_of("result", RESULT_STATES), name="result_valid"),
        # The queue and the trust score both filter by loan then by outcome.
        sa.Index("ix_validation_results_loan_result", "loan_id", "result"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)

    # Nullable: a dataset-scope rule (DUPLICATE_LOAN_ID) is a judgement about a
    # set of rows, not about one record, so there is no single record to point at.
    loan_record_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("loan_records.id"))
    # NOT nullable, for the same reason: this is the column that makes dataset
    # results reachable from a loan's detail page.
    loan_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("loans.id"), nullable=False)

    rule_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("validation_rules.id"), nullable=False)
    # Denormalised on purpose. rule_id already identifies a single (code,
    # version) row, so this is redundant -- but it means the audit query reads
    # "REQUIRED_CORE_FIELDS v2 failed" without a join, and an audit trail you
    # have to join to interpret is an audit trail nobody reads.
    rule_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)

    # Three states, not a boolean. 'not_applicable' is the correct answer for
    # 16 of the 18 seed rules against a document_manifest row, and only
    # pass/fail count toward the trust score denominator. A boolean here turns
    # every blank cell in an 842-row manifest into a false exception.
    result: Mapped[str] = mapped_column(sa.Text, nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    # The operand values that decided it -- what the message_template renders
    # from, and what the AI evidence bundle quotes.
    details: Mapped[Optional[dict]] = mapped_column(JSONB)

    rule: Mapped["ValidationRule"] = relationship(lazy="select")

    def __repr__(self) -> str:
        return f"<Result {self.result} rule={self.rule_id}>"