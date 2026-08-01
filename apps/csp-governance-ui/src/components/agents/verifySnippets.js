// P2.1 — 派工 JWT 驗簽片段（取代舊的靜態服務憑證比對）。
// 契約：Authorization: Bearer <JWT>；iss=anila-csp、aud=anila-agent；
// RS256 + kid；JWKS = {CSP_BASE_URL}/.well-known/jwks.json；CA 用獨立 PEM 檔。

/**
 * @typedef {Object} VerifySnippetCtx
 * @property {string} cspUrl  - 平台 base URL（agent 主機可連到的那個）
 * @property {string} [caPath] - CA bundle 本機路徑提示
 */

const PLACEHOLDER_CSP = 'https://<csp-host-reachable-from-agent>'
const PLACEHOLDER_CA = '/path/to/cspki_ca_bundle.pem'

/**
 * 非祕密設定：平台 URL ＋ CA 路徑。不含任何長效憑證。
 * @param {VerifySnippetCtx} ctx
 */
export function buildEnvSnippet(ctx = {}) {
  const origin = (ctx.cspUrl || '').trim()
  const isLoopback = !origin || /localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0/.test(origin)
  const base = isLoopback ? PLACEHOLDER_CSP : origin.replace(/\/$/, '')
  const ca = ctx.caPath || PLACEHOLDER_CA
  const lines = []
  if (isLoopback) {
    lines.push('# CSP_BASE_URL：填 agent 主機「打得到」的平台位址（勿用本機 loopback）')
  }
  lines.push(`CSP_BASE_URL=${base}`)
  lines.push(`ANILA_CA_FILE=${ca}`)
  lines.push('# ⚠ 勿設 SSL_CERT_FILE——會整份取代系統信任庫')
  return lines.join('\n')
}

/**
 * 既有 Python 服務：複製單檔 anila_verify.py 後接入（stdlib + cryptography）。
 * @param {VerifySnippetCtx} ctx
 */
export function buildPythonVerifySnippet(ctx = {}) {
  const origin = (ctx.cspUrl || '').trim()
  const isLoopback = !origin || /localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0/.test(origin)
  const base = isLoopback ? PLACEHOLDER_CSP : origin.replace(/\/$/, '')
  const ca = ctx.caPath || PLACEHOLDER_CA
  return `# 1) 按治理中心「下載 anila_verify.py」，放在你的服務旁邊
# 2) pip install cryptography
#    （氣隙：僅在已有預先打包的 wheelhouse 時用 --no-index；樣板 zip 未必已含 .whl）
# 3) .env 只放非祕密設定：
#    CSP_BASE_URL=${base}
#    ANILA_CA_FILE=${ca}

import os
from anila_verify import verify_authorization

def require_dispatch(request):
    """驗平台派工 JWT；失敗丟例外（fail-closed）。"""
    claims = verify_authorization(
        request.headers.get("Authorization"),
        jwks_url=f"{os.environ['CSP_BASE_URL'].rstrip('/')}/.well-known/jwks.json",
        ca_file=os.environ.get("ANILA_CA_FILE") or None,
    )
    # claims: user_id / department / agent_id
    return claims`
}

/**
 * @param {VerifySnippetCtx} ctx
 * @returns {{ env: string, pythonVerify: string }}
 */
export function buildVerifySnippets(ctx = {}) {
  return {
    env: buildEnvSnippet(ctx),
    pythonVerify: buildPythonVerifySnippet(ctx),
  }
}
