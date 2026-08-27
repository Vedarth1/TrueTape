"""users -- the actor table. Every audit event and reviewer decision points here."""
from __future__ import annotations

import uuid
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.extensions import db
from app.models.enums import ROLES, one_of


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        sa.CheckConstraint(one_of("role", ROLES), name="role_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    email: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    role: Mapped[str] = mapped_column(sa.Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[Optional[sa.DateTime]] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now())

    def __repr__(self) -> str:
        return f"<User {self.email} {self.role}>"