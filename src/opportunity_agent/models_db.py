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

# "tender" is separate from "grant" deliberately: both are organisation-shaped,
# but a tender is a contract bid found on procurement boards (see egp.py) and a
# grant is a funding call found by search, so they need different discovery.
# What each type asks the owner for lives in profile_schema.py, not here.
PROFILE_TYPES = ("scholarship", "job", "grant", "tender")


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
    opportunities: Mapped[list["StoredOpportunity"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    discovery_runs: Mapped[list["ProfileDiscoveryRun"]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # Which paper this *is* (profile_schema.DocumentKind.key), not just its
    # filename. Without it the agent can see that four files were uploaded but
    # not whether any of them is the tax clearance a tender is asking for.
    # "other" for anything the owner didn't classify.
    doc_type: Mapped[str] = mapped_column(String(50), default="other")
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(20), default="skipped")
    uploaded_at: Mapped[datetime] = mapped_column(default=_now)

    profile: Mapped["Profile"] = relationship(back_populates="documents")


class StoredOpportunity(Base):
    """One opportunity, scoped to one Profile.

    Replaces the single global OpportunityStore for the multi-profile pipeline
    (SOLUTION_DEFINITION.md §16). `payload` holds the Opportunity pydantic
    model's dump - same reasoning as Profile.fields: nothing queries individual
    opportunity fields via SQL, matching happens in Python.

    Two orthogonal axes rather than one overloaded status:
      stage        - where the agent/human workflow has got to
      match_status - what the eligibility engine decided (eligible /
                     needs_review / ineligible), kept denormalised so the
                     "browse what it rejected" view is a cheap query
    """

    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("profile_id", "canonical_url", name="uq_profile_opportunity_url"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    match_status: Mapped[str] = mapped_column(String(20), nullable=False)
    match_score: Mapped[int] = mapped_column(default=0)
    match_reasons: Mapped[dict] = mapped_column(JSON, default=dict)
    # discovered -> needs_documents -> drafted -> approved / dismissed / submitted
    # "needs_documents" is its own stage rather than a flag: the owner qualifies
    # and the draft exists, but a file is missing, and that is a different call
    # to action from "read this and approve it".
    stage: Mapped[str] = mapped_column(String(20), default="discovered")
    # The compliance advisor's output for this opportunity (compliance.py):
    # what is held, what is missing, what to do, what blocks submission.
    compliance: Mapped[dict | None] = mapped_column(JSON, default=None)
    escalated: Mapped[bool] = mapped_column(default=False)
    package: Mapped[dict | None] = mapped_column(JSON, default=None)
    usefulness: Mapped[str | None] = mapped_column(String(20), default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    profile: Mapped["Profile"] = relationship(back_populates="opportunities")


class ProfileDiscoveryRun(Base):
    """Audit record of one agent discovery cycle for one profile."""

    __tablename__ = "profile_discovery_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(default=_now)
    completed_at: Mapped[datetime] = mapped_column(default=_now)
    queries: Mapped[list] = mapped_column(JSON, default=list)
    found: Mapped[int] = mapped_column(default=0)
    added: Mapped[int] = mapped_column(default=0)
    drafted: Mapped[int] = mapped_column(default=0)
    failures: Mapped[list] = mapped_column(JSON, default=list)

    profile: Mapped["Profile"] = relationship(back_populates="discovery_runs")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("profiles.id"), default=None)
    opportunity_id: Mapped[str | None] = mapped_column(ForeignKey("opportunities.id"), default=None)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    account: Mapped["Account"] = relationship(back_populates="notifications")
