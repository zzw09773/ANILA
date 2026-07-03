// Inbound-guard + outbound-RAG snippets for a directly-issued csk-.
//
// Surfaced wherever a csk- is shown one-shot (detail-modal issue-static /
// rotate, and the register wizard step-2) so a dev with a NON-template
// agent (one that does not fork AgenticRAG and cannot import anila_core)
// gets, alongside the token:
//   1. a zero-dependency FastAPI/Starlette middleware that ENFORCES the
//      inbound X-CSP-Service-Token (fail-CLOSED — refuses to boot when the
//      token env is unset), and
//   2. (only when a collection is bound) how the SAME csk- queries that
//      collection's RAG outbound.
//
// The csk- only ever appears in the `.env` line; runnable code reads it
// from os.environ so the secret never gets hardcoded into a source file
// the dev might commit. Env names match the platform convention
// (CSP_BASE_URL / CSP_SERVICE_TOKEN / ANILA_COLLECTION_ID — see the
// newAgentEnvSnippet in the agent onboarding view). Mirrors bootstrapSnippets.js.

const PLACEHOLDER_CSK = 'csk-PASTE-FROM-ADMIN-UI'

/**
 * @typedef {object} GuardContext
 * @property {string} csk            - csk-... service token (one-shot).
 * @property {number} [collectionId] - Agent's bound collection id; the
 *                                      outbound RAG snippet is emitted only
 *                                      when this is a number.
 */

/**
 * Inbound guard — .env line (csk- pre-filled) + a fail-closed Starlette
 * middleware that verifies X-CSP-Service-Token on every non-public request.
 * @param {GuardContext} ctx
 */
export function pythonInboundGuard(ctx) {
  const cskLiteral = ctx.csk || PLACEHOLDER_CSK
  // When a collection is bound, the outbound RAG snippet also reads
  // CSP_BASE_URL — surface it here so the .env block stays self-contained
  // even in the detail-modal path (which has no other .env generator). Uses
  // the same loopback-safe placeholder convention as newAgentEnvSnippet.
  const ragEnvLine =
    typeof ctx.collectionId === 'number'
      ? '\n# 出向 RAG 還需這行（從 agent 機器可達的 CSP host，別用 localhost）\nCSP_BASE_URL=https://<csp-host-reachable-from-agent>'
      : ''
  return `# 1) .env（程式從環境變數讀，別把 csk- 寫進原始碼）
CSP_SERVICE_TOKEN=${cskLiteral}${ragEnvLine}

# 2) main.py / app.py — 貼這段（只需 Starlette/FastAPI，你的 agent 本來就有）
import hmac
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_CSP_TOKEN = os.environ.get("CSP_SERVICE_TOKEN", "")
if not _CSP_TOKEN:                                    # fail-CLOSED：沒設就拒絕啟動
    raise RuntimeError(
        "CSP_SERVICE_TOKEN unset — 把 ANILA 後台核發的 csk- 貼進 .env 再啟動"
    )

# 與平台一致的公開路徑（探活/文件），這些不驗 token
_PUBLIC_PATHS = {"/health", "/docs", "/openapi.json", "/redoc"}


class AnilaInboundGuard(BaseHTTPMiddleware):
    """拒絕任何不是來自 ANILA Router 的請求。"""

    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        presented = request.headers.get("X-CSP-Service-Token", "")
        if not presented:
            return JSONResponse(
                {"detail": "missing X-CSP-Service-Token"}, status_code=401
            )
        if not hmac.compare_digest(presented, _CSP_TOKEN):   # 常數時間比對
            return JSONResponse(
                {"detail": "invalid service token"}, status_code=403
            )
        return await call_next(request)


app.add_middleware(AnilaInboundGuard)   # 接上去，一行`
}

/**
 * Outbound RAG — the SAME csk- as Authorization: Bearer to search the
 * agent's bound collection. Returns '' when no collection is bound (the
 * agent then has nothing to search). Route/body verified against
 * SearchRequest (POST /api/ingestion/collections/{id}/search).
 * @param {GuardContext} ctx
 */
export function pythonOutboundRag(ctx) {
  if (typeof ctx.collectionId !== 'number') return ''
  return `import os

import httpx

CSP_BASE_URL = os.environ["CSP_BASE_URL"].rstrip("/")   # 從 agent 機器可達的 CSP host（別用 localhost）
COLLECTION_ID = ${ctx.collectionId}   # 此 agent 綁定的 collection


def search_rag(query: str, top_k: int = 5) -> dict:
    """用同一把 csk- 查這個 agent 綁定的 RAG collection。"""
    resp = httpx.post(
        f"{CSP_BASE_URL}/api/ingestion/collections/{COLLECTION_ID}/search",
        json={"query": query, "top_k": top_k},
        headers={"Authorization": f"Bearer {os.environ['CSP_SERVICE_TOKEN']}"},
        timeout=30.0,
    )
    resp.raise_for_status()    # csk- 僅能搜自己綁定的 collection；搜別的會 403
    return resp.json()`
}

/**
 * Build both snippets for the panel.
 * @param {GuardContext} ctx
 */
export function buildGuardSnippets(ctx) {
  return {
    inboundGuard: pythonInboundGuard(ctx),
    outboundRag: pythonOutboundRag(ctx),
  }
}
