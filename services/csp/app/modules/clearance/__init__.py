# -*- coding: utf-8 -*-
"""Gate 2 G2a clearance management and data-access policy surface."""

from app.modules.clearance.router import router
from app.modules.clearance.service import (
    ClearancePolicyDataError,
    DataAccessContext,
    DataAccessDecision,
    add_grant_compartment,
    assign_collection_required_compartment,
    assign_document_required_compartment,
    create_security_compartment,
    evaluate_data_access,
    grant_collection_access,
    issue_clearance_grant,
    resolve_and_evaluate_data_access,
    resolve_data_access_context,
    revoke_clearance_grant,
    revoke_collection_access,
)

__all__ = [
    "ClearancePolicyDataError",
    "DataAccessContext",
    "DataAccessDecision",
    "add_grant_compartment",
    "assign_collection_required_compartment",
    "assign_document_required_compartment",
    "create_security_compartment",
    "evaluate_data_access",
    "grant_collection_access",
    "issue_clearance_grant",
    "resolve_and_evaluate_data_access",
    "resolve_data_access_context",
    "revoke_clearance_grant",
    "revoke_collection_access",
    "router",
]
