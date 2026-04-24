"""Dynamic loader for the CSP service-token middleware.

The AgenticRAG template is designed to run in two modes:

  1. **Inside the ANILA platform** — ``anila-core`` is available and provides
     ``anila_core.api.middleware.auth.CspServiceTokenMiddleware``. This is the
     canonical source of truth for CSP service-token behaviour across all
     agents in the platform.

  2. **Standalone** — the template is forked and used without ``anila-core``
     (e.g. running against a different orchestrator, or for local unit tests).
     In this mode the local ``csp_auth.CspServiceTokenMiddleware`` is used
     instead. The two implementations share the same public signature so the
     rest of the application does not need to care which one is active.

Loading order:
  1. ``anila_core.api.middleware.auth``            (canonical)
  2. ``src.anila_core.api.middleware.auth``         (monorepo dev layout)
  3. ``agentic_rag.api.middleware.csp_auth``        (in-package fallback)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def install_csp_middleware(app: "FastAPI", service_token: str | None) -> str:
    """Install the CSP service-token middleware on *app*.

    Args:
        app: FastAPI application to mount the middleware on.
        service_token: Expected value of the ``X-CSP-Service-Token`` header.
            When None/empty, middleware runs in pass-through dev mode.

    Returns:
        The import path of the middleware class that was actually installed —
        useful for logging / diagnostics.
    """
    dev_mode = not service_token

    for module_path in (
        "anila_core.api.middleware.auth",
        "src.anila_core.api.middleware.auth",
    ):
        try:
            module = __import__(module_path, fromlist=["CspServiceTokenMiddleware"])
        except ImportError:
            continue
        middleware_cls = getattr(module, "CspServiceTokenMiddleware", None)
        if middleware_cls is None:
            # A different version of anila_core is on PYTHONPATH that predates
            # the CSP middleware (it may only have the older ApiKeyMiddleware).
            # Skip it and try the next candidate rather than crashing at boot.
            logger.warning(
                "anila_core module %s has no CspServiceTokenMiddleware (path=%s). "
                "This usually means an older anila-core is shadowing the one "
                "shipped with ANILA. Falling through to the next candidate.",
                module_path,
                getattr(module, "__file__", "?"),
            )
            continue
        app.add_middleware(
            middleware_cls,
            service_token=service_token,
            dev_mode=dev_mode,
        )
        logger.info(
            "CSP middleware installed from %s (dev_mode=%s)",
            module_path,
            dev_mode,
        )
        return module_path

    from .csp_auth import CspServiceTokenMiddleware

    app.add_middleware(
        CspServiceTokenMiddleware,
        service_token=service_token,
        dev_mode=dev_mode,
    )
    fallback_path = "agentic_rag.api.middleware.csp_auth"
    logger.info(
        "CSP middleware installed from local fallback %s (dev_mode=%s)",
        fallback_path,
        dev_mode,
    )
    return fallback_path
