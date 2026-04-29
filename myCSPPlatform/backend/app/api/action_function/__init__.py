"""Routers for the ANILA Functions v1 API.

Each sub-module exports its own router; ``app.api.router`` aggregates
them into the top-level ``api_router``. Splitting keeps each
endpoint group's deps + RBAC reasoning local rather than one giant
file.
"""

from app.api.action_function.crud import router as crud_router
from app.api.action_function.valves import router as valves_router
from app.api.action_function.marketplace import router as marketplace_router
from app.api.action_function.run import router as run_router
from app.api.action_function.runs import router as runs_router
from app.api.action_function.enabled_actions import router as enabled_actions_router

__all__ = [
    "crud_router",
    "valves_router",
    "marketplace_router",
    "run_router",
    "runs_router",
    "enabled_actions_router",
]
