"""Router mode example — launch ANILA Core Router as a standalone service.

The Router fetches available agents from CSP at startup, then routes
incoming queries to the most appropriate agent via the main LLM.

Run (with CSP already running):
    CSP_BASE_URL=http://localhost:8000 \
    CSP_API_KEY=sk-... \
    MODEL=google/gemma4 \
    API_DEV_MODE=true \
    uvicorn main:app --host 0.0.0.0 --port 9000

Then send a query:
    curl -X POST http://localhost:9000/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{"model":"anila-router","messages":[{"role":"user","content":"查 Q3 財報重點"}]}'
"""

from anila_core.api.router_server import create_router_app

app = create_router_app()
