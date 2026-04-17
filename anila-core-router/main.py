"""ANILA Core Router — deployment entrypoint.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 9000

Reads from environment:
    CSP_BASE_URL      — myCSPPlatform base URL (e.g. http://csp:8000)
    CSP_API_KEY       — CSP API Key for data-plane calls (sk-...)
    CSP_SERVICE_TOKEN — service-to-service credential (optional dev_mode if unset)
    LLM_MODEL         — model name registered in CSP (default: gpt-4o-mini)
"""
from anila_core.api.router_server import create_router_app

app = create_router_app()
