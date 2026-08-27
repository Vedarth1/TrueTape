"""source_trust_config -- survivorship policy stored as data, not as a constant.

Read-only in the shipped build. It lives in a table anyway because "ServicerX
is more trustworthy than the loan tape for current_balance, and here is the
written rationale" is a policy statement a judge can query, whereas a dict in
a .py file is a magic number.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db


class SourceTrustConfig(db.Model):
    __tablename__ = "source_trust_config"
    __table_args__ = (
        sa.CheckConstraint("trust_score BETWEEN 0 AND 100",
                           name="trust_score_range"),
        sa.UniqueConstraint("source_system", "field_name",
                            name="source_field_unique"),
        # Postgres treats NULLs as distinct in a unique index, so the
        # constraint above does NOT stop two rows with the same source_system
        # and field_name IS NULL -- meaning the base score for a source could
        # be seeded twice and survivorship would pick whichever the planner
        # returned first. A partial unique index closes the hole on every
        # Postgres version. (PG15+ could use NULLS NOT DISTINCT instead; this
        # works regardless, and it is one line.)
        sa.Index("uq_trust_base_score", "source_system", unique=True,
                 postgresql_where=sa.text("field_name IS NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    source_system: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # NULL means "base score for every field of this source".
    field_name: Mapped[Optional[str]] = mapped_column(sa.Text)
    trust_score: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # NOT NULL by design. A trust weight without a written reason is a magic
    # number, and the seed loader will refuse to insert one.
    rationale: Mapped[str] = mapped_column(sa.Text, nullable=False)
    updated_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        sa.ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())

    def __repr__(self) -> str:
        scope = self.field_name or "*"
        return f"<Trust {self.source_system}/{scope}={self.trust_score}>"