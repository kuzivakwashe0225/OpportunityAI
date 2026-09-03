"""SQLAlchemy models for Phase 2: accounts, profiles, documents, notifications.

See SOLUTION_DEFINITION.md §14. Distinct from models.py's PersonalProfile
(Pydantic, used by the existing single-tenant discovery/matching pipeline) -
Profile.fields here is the account-scoped, persisted equivalent, stored as a
JSON blob rather than one column per field: it needs to round-trip the same
shape PersonalProfile already validates, and nothing here queries by
individual profile fields yet (matching still happens in Python against a
loaded PersonalProfile, not via SQL) - proper columns are a straightforward
later migration if that changes, not a decision to relitigate now.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

PROFILE_TYPES = ("scholarship", "job", "grant")


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    profiles: Mapped[list["Profile"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        UniqueConstraint("account_id", "profile_type", name="uq_account_profile_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    profile_type: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    picture_url: Mapped[str | None] = mapped_column(String(500), default=None)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    hidden_fields: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    account: Mapped["Account"] = relationship(back_populates="profiles")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(20), default="skipped")
    uploaded_at: Mapped[datetime] = mapped_column(default=_now)

    profile: Mapped["Profile"] = relationship(back_populates="documents")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    account: Mapped["Account"] = relationship(back_populates="notifications")
