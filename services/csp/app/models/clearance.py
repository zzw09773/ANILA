# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance, compartment, and need-to-know data model.

Platform roles are intentionally absent from these tables.  A user receives
data authority only through one active :class:`ClearanceGrant`; collection
membership and need-to-know are attached to that same grant so callers cannot
compose partial authority from multiple grants.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base

_CLASSIFICATION_VALUES_SQL = (
    "'無機密', '營業秘密', '機密', '極機密', '絕對機密'"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SecurityCompartment(Base):
    """Owner/admin-managed compartment catalog (for example PROJECT_X)."""

    __tablename__ = "security_compartments"
    __table_args__ = (
        CheckConstraint(
            "length(trim(code)) > 0 AND code = upper(code) "
            "AND code NOT LIKE '% %'",
            name="ck_security_compartments_code_canonical",
        ),
        CheckConstraint(
            "length(trim(name)) > 0",
            name="ck_security_compartments_name_nonempty",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ClearanceGrant(Base):
    """One bounded clearance authority issued to one user."""

    __tablename__ = "clearance_grants"
    __table_args__ = (
        CheckConstraint(
            f"max_classification_level IN ({_CLASSIFICATION_VALUES_SQL})",
            name="ck_clearance_grants_classification_level",
        ),
        CheckConstraint(
            "expires_at > valid_from",
            name="ck_clearance_grants_time_order",
        ),
        CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_clearance_grants_basis_ticket_nonempty",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_clearance_grants_revocation_pair",
        ),
        Index(
            "ix_clearance_grants_subject_window",
            "subject_user_id",
            "revoked_at",
            "valid_from",
            "expires_at",
        ),
        Index("ix_clearance_grants_expires_at", "expires_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    subject_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    max_classification_level = Column(String(20), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    basis_ticket = Column(String(255), nullable=False)
    issued_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    subject = relationship("User", foreign_keys=[subject_user_id])
    issuer = relationship("User", foreign_keys=[issued_by_user_id])
    revoker = relationship("User", foreign_keys=[revoked_by_user_id])
    compartment_memberships = relationship(
        "ClearanceGrantCompartment",
        back_populates="clearance_grant",
        cascade="all, delete-orphan",
    )
    collection_access_grants = relationship(
        "CollectionAccessGrant",
        back_populates="clearance_grant",
        cascade="all, delete-orphan",
    )


class ClearanceGrantCompartment(Base):
    """A compartment covered by exactly one clearance grant."""

    __tablename__ = "clearance_grant_compartments"
    __table_args__ = (
        UniqueConstraint(
            "clearance_grant_id",
            "compartment_id",
            name="uq_clearance_grant_compartment",
        ),
        Index(
            "ix_clearance_grant_compartments_compartment",
            "compartment_id",
        ),
    )

    clearance_grant_id = Column(
        Integer,
        ForeignKey("clearance_grants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    compartment_id = Column(
        Integer,
        ForeignKey("security_compartments.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    clearance_grant = relationship(
        "ClearanceGrant", back_populates="compartment_memberships"
    )
    compartment = relationship("SecurityCompartment")


class CollectionAccessGrant(Base):
    """Collection membership/NTK bound to one clearance grant.

    A collection owner may satisfy ``membership_granted`` implicitly, but
    never the ``need_to_know`` flag, classification ceiling, or compartments.
    """

    __tablename__ = "collection_access_grants"
    __table_args__ = (
        UniqueConstraint(
            "clearance_grant_id",
            "collection_id",
            name="uq_collection_access_grant",
        ),
        CheckConstraint(
            "membership_granted OR need_to_know",
            name="ck_collection_access_grants_nonempty_authority",
        ),
        CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_collection_access_grants_basis_ticket_nonempty",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by_user_id IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_collection_access_grants_revocation_pair",
        ),
        Index(
            "ix_collection_access_grants_collection",
            "collection_id",
            "revoked_at",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    clearance_grant_id = Column(
        Integer,
        ForeignKey("clearance_grants.id", ondelete="CASCADE"),
        nullable=False,
    )
    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        nullable=False,
    )
    membership_granted = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    need_to_know = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    basis_ticket = Column(String(255), nullable=False)
    issued_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    clearance_grant = relationship(
        "ClearanceGrant", back_populates="collection_access_grants"
    )
    collection = relationship("IngestionCollection")
    issuer = relationship("User", foreign_keys=[issued_by_user_id])
    revoker = relationship("User", foreign_keys=[revoked_by_user_id])


class CollectionRequiredCompartment(Base):
    """A compartment required for every document in a collection."""

    __tablename__ = "collection_required_compartments"
    __table_args__ = (
        CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_collection_required_compartments_basis_nonempty",
        ),
        Index(
            "ix_collection_required_compartments_compartment",
            "compartment_id",
        ),
    )

    collection_id = Column(
        Integer,
        ForeignKey("ingestion_collections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    compartment_id = Column(
        Integer,
        ForeignKey("security_compartments.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    basis_ticket = Column(String(255), nullable=False)
    assigned_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class DocumentRequiredCompartment(Base):
    """Document-only requirements; evaluator unions these with collection rows."""

    __tablename__ = "document_required_compartments"
    __table_args__ = (
        CheckConstraint(
            "length(trim(basis_ticket)) > 0",
            name="ck_document_required_compartments_basis_nonempty",
        ),
        Index(
            "ix_document_required_compartments_compartment",
            "compartment_id",
        ),
    )

    document_id = Column(
        Integer,
        ForeignKey("ingestion_documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    compartment_id = Column(
        Integer,
        ForeignKey("security_compartments.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    basis_ticket = Column(String(255), nullable=False)
    assigned_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


__all__ = [
    "ClearanceGrant",
    "ClearanceGrantCompartment",
    "CollectionAccessGrant",
    "CollectionRequiredCompartment",
    "DocumentRequiredCompartment",
    "SecurityCompartment",
]
