"""ANILA Core Router — deployment entrypoint.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 9000

Reads from environment:
    CSP_BASE_URL      — myCSPPlatform base URL (e.g. http://csp:8000)
    MODEL             — main model name registered in CSP (default: gpt-4o-mini)

Runtime note:
    Router uses the caller's Bearer API key for CSP data-plane calls.
    It does not require a preconfigured RAG agent or a dedicated router API key.
"""
from anila_core.api.router_server import create_router_app

app = create_router_app()
