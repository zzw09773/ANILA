"""Example: wrap an anila-agent as a CSP-dispatchable OpenAI-compatible service.

anila-agent is a CLI / library. To let the CSP Router dispatch to it, an agent
ships a thin OpenAI-compatible HTTP wrapper. Copy this file into your agent repo
and adjust ``MODEL_NAME`` / ``ANILA_COLLECTION_ID``.

Endpoints:
    GET  /health               health probe (CSP registers agents with this)
    GET  /v1/models            manifest (model_type=agent)
    POST /v1/chat/completions   main entry — CSP Router forwards conversations here

Auth model (the corrected one — earlier downstream copies got this wrong):
    The CSP Router dispatches with:
        X-CSP-Service-Token: csk-...        ← THIS agent's own credential; we verify it
        X-ANILA-User-Id / -Email / -Groups  ← end-user identity (trust only AFTER the
                                              service token verifies)
    It does NOT forward the user's JWT. Verifying ``Authorization: Bearer <jwt>``
    is wrong — mirror ``proxy_service._build_downstream_headers`` on the CSP side.

Inbound auth: ``X-CSP-Service-Token`` must equal env ``CSP_SERVICE_TOKEN`` (the
csk- the agent owner self-issues via ``POST /api/agents/{id}/credentials/
issue-static``). Constant-time comparison. **Unset → reject by default**
(fail-closed); set ``ANILA_ALLOW_NO_SERVICE_TOKEN=1`` to disable verification
for LOCAL DEV only.

RAG: retrieval goes through CSP's HTTP search API via ``CspHttpRetriever`` (no
direct DB) — wired with the ``build_agent(retriever=...)`` escape hatch. S-Q1:
**one key** — the agent reuses its own ``CSP_SERVICE_TOKEN`` (csk-) as the
search credential; CSP authorises it scoped to the agent's bound collection.
``CSP_SEARCH_TOKEN`` is now an optional override (transition / distinct search
identity); when unset it falls back to ``CSP_SERVICE_TOKEN``.

Run: ``uvicorn examples.service_wrapper:app --host 0.0.0.0 --port 8200``
"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from anila_agent.core.agent import build_agent
from anila_agent.core.runner import AnilaRunner
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.utils.config import load_config

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("ANILA_AGENT_NAME", "my-agent")
COLLECTION_ID = int(os.environ.get("ANILA_COLLECTION_ID", "0") or "0")
CSP_BASE_URL = os.environ.get("CSP_BASE_URL", "https://172.16.120.35")
SSL_VERIFY = os.environ.get("ANILA_SSL_VERIFY", "1") not in ("0", "false", "False", "no")
# Inbound: the Router's X-CSP-Service-Token must equal this (the agent's csk-).
# Empty → reject by default (fail-closed); prod MUST set it.
CSP_SERVICE_TOKEN = os.environ.get("CSP_SERVICE_TOKEN", "")
# Explicit LOCAL-DEV opt-out: allow inbound calls when CSP_SERVICE_TOKEN is unset.
# Without this, an unconfigured agent rejects every dispatch (fail-closed) — a
# forgotten key is loudly broken instead of silently wide-open.
ALLOW_NO_SERVICE_TOKEN = os.environ.get("ANILA_ALLOW_NO_SERVICE_TOKEN", "") == "1"
# S-Q1 one-key model: search reuses the agent's own csk-. CSP_SEARCH_TOKEN is an
# optional override; when unset it falls back to CSP_SERVICE_TOKEN.
CSP_SEARCH_TOKEN = os.environ.get("CSP_SEARCH_TOKEN", "") or CSP_SERVICE_TOKEN

_CONFIG: Any = None


def verify_service_token(
    provided: str | None, expected: str, *, allow_unset: bool = False
) -> bool:
    """Constant-time check of the inbound X-CSP-Service-Token.

    Returns True when the request is allowed:
      * ``expected`` empty  → allowed ONLY when ``allow_unset`` is True
                              (explicit local-dev opt-out); otherwise rejected
                              (fail-closed — a forgotten csk- is not wide-open);
      * otherwise           → allowed iff ``provided`` matches via
                              ``hmac.compare_digest`` (timing-attack safe).

    Pure + side-effect free so it can be unit-tested without FastAPI.
    """
    if not expected:
        return allow_unset
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _CONFIG
    if not CSP_SERVICE_TOKEN:
        if ALLOW_NO_SERVICE_TOKEN:
            logger.warning(
                "CSP_SERVICE_TOKEN unset + ANILA_ALLOW_NO_SERVICE_TOKEN=1 — inbound "
                "service-token verification DISABLED (local dev only)."
            )
        else:
            logger.error(
                "CSP_SERVICE_TOKEN unset — every dispatch will be REJECTED (401, "
                "fail-closed). Set the agent's csk-, or ANILA_ALLOW_NO_SERVICE_TOKEN=1 "
                "for local dev only."
            )
    if not SSL_VERIFY:
        import litellm  # noqa: PLC0415 — optional dep, only when self-signed

        litellm.ssl_verify = False
    _CONFIG = load_config()
    yield


app = FastAPI(title=MODEL_NAME, lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: list[ChatMessage]
    stream: bool = False


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "model_type": "agent"}],
    }


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    x_anila_user_id: str | None = Header(default=None, alias="X-ANILA-User-Id"),
) -> dict[str, Any]:
    if not verify_service_token(
        x_csp_service_token, CSP_SERVICE_TOKEN, allow_unset=ALLOW_NO_SERVICE_TOKEN
    ):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-CSP-Service-Token. The CSP Router dispatch "
                "sends the agent's csk- automatically; for a direct local test "
                "send the header (or set ANILA_ALLOW_NO_SERVICE_TOKEN=1 to disable)."
            ),
        )

    user_prompt = next(
        (m.content for m in reversed(req.messages) if m.role == "user"), ""
    )
    if not user_prompt:
        raise HTTPException(status_code=400, detail="no user message in `messages`")

    # Retrieval via CSP HTTP (no DB). The build_agent(retriever=) escape hatch
    # injects it directly. S-Q1 one-key: api_key is the agent's own csk-
    # (CSP_SEARCH_TOKEN falls back to CSP_SERVICE_TOKEN above); CSP authorises it
    # scoped to this agent's bound collection.
    retriever = CspHttpRetriever(
        csp_base_url=CSP_BASE_URL,
        collection_id=COLLECTION_ID,
        api_key=CSP_SEARCH_TOKEN,
        verify_ssl=SSL_VERIFY,
    )

    session_id = f"req-{uuid.uuid4().hex[:12]}"
    assembled = build_agent(_CONFIG, session_id=session_id, retriever=retriever)
    runner = AnilaRunner(assembled, session_id=session_id)
    await runner.start()
    summary = await runner.send(user_prompt)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    # RunSummary exposes `final_output` (already sanitized of any
                    # leaked tool-call tokens by the runner).
                    "content": summary.final_output or "",
                },
                "finish_reason": "stop",
            }
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8200")))
