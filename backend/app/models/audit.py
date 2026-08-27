"""audit_events -- append-only, hash-chained.

Two properties make the immutability claim real rather than asserted:
UPDATE and DELETE are revoked from the app role at the database level, and
each row's event_hash covers the previous row's hash.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import ACTOR_TYPES, EVENT_TYPES, one_of


class AuditEvent(db.Model):
    __tablename__ = "audit_events"
    __table_args__ = (
        sa.CheckConstraint(one_of("actor_type", ACTOR_TYPES),
                           name="actor_type_valid"),
        # Constrained even though the list has fifteen values, because a typo'd
        # event_type is invisible -- the event is written, the timeline filter
        # never matches it, and the gap only shows up in the demo.
        sa.CheckConstraint(one_of("event_type", EVENT_TYPES),
                           name="event_type_valid"),
        sa.Index("ix_audit_events_loan_seq", "loan_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)

    # Identity rather than the PK, because the PK is a UUID and UUIDs do not
    # order. seq gives the chain a deterministic walk order for /verify.
    # UNIQUE is not in the DDL but is worth having: it turns the verification
    # walk into an index scan and guarantees no two events claim one position.
    #
    # Caveat that matters: a sequence allocates before COMMIT, so seq alone
    # does not guarantee commit order. It is pg_advisory_xact_lock at the top
    # of log_event that makes seq order and chain order the same thing.
    seq: Mapped[int] = mapped_column(
        sa.BigInteger, sa.Identity(), nullable=False, unique=True)

    # A polymorphic pointer with no FK, deliberately. entity_id references rows
    # in a dozen tables and you cannot FK to "one of twelve"; twelve nullable
    # FK columns would make this table unreadable. The cost is that referential
    # integrity here is the application's job.
    entity_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(sa.Uuid)
    # Denormalised so GET /api/audit/:loanId is one index hit rather than a
    # union over entity_type-specific joins.
    loan_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("loans.id"))

    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # NULL when actor_type='system' -- an automated validation run has no user.
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("users.id"))
    actor_type: Mapped[str] = mapped_column(sa.Text, nullable=False)

    before_value: Mapped[Optional[dict]] = mapped_column(JSONB)
    after_value: Mapped[Optional[dict]] = mapped_column(JSONB)
    reason: Mapped[Optional[str]] = mapped_column(sa.Text)
    ai_metadata: Mapped[Optional[dict]] = mapped_column(JSONB)

    # event_hash is computed over the canonical JSON of this event INCLUDING
    # its id -- which is why every table in this schema uses a Python-side
    # uuid4 default instead of gen_random_uuid(). A server-side default means
    # no id until after the INSERT, and the hash could not include it.
    # prev_event_hash is NULL for exactly one row: the genesis event.
    event_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    prev_event_hash: Mapped[Optional[str]] = mapped_column(sa.Text)

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<Audit #{self.seq} {self.event_type} {self.actor_type}>"