> ⚠ **2026-08-01 P2.1**：本計畫／規格所描述的 `csk-` 入向守衛與靜態服務憑證路徑，
> 已由派工 JWT＋JWKS 驗簽取代。下文保留原設計決策軌跡；現行接入見
> `docs/guides/developer-guide.md`。

# csk- 入向守門碼 + 出向 RAG 可複製區塊 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在每個吐出一次性 csk- 的地方（詳情 modal 的 issue-static / rotate、register 精靈 step-2），新增 `AgentGuardPanel`，呈現可複製 Python：① fail-closed 入向守門 middleware ②（已綁 collection 時）同一把 csk- 的出向 RAG 用法 —— 讓非模板 agent 不必 import anila_core 就能正確接上。

**Architecture:** 純前端、零後端改動。仿既有 `bootstrapSnippets.js` + `BootstrapHowToTabs.vue` 的「builder 模組 + 顯示元件」分工。env 變數命名對齊平台既有 `newAgentEnvSnippet`（`CSP_BASE_URL` / `CSP_SERVICE_TOKEN` / `ANILA_COLLECTION_ID`）。csk- 只填進 `.env` 行，程式一律 `os.environ` 讀。

**Tech Stack:** Vue 3 `<script setup>`、`components/cli`（TermBox/TermButton）、Vite。**無 JS 測試框架**（無 vitest/jest，`bootstrapSnippets.js` 亦零測試）→ 驗證 = `npm run build` + 人工煙霧測（符合 CLAUDE.md「前端驗證用 npm run build」），不為此引入 runner。

---

## Codex plan-review（第 1 輪）已處理

- **[HIGH] 漏 register 精靈 step-2** → 新增 **Task 3b**（`newAgentCsk` 旁掛同元件，用 `registeredAgent.bound_collection_id`）。
- **[HIGH] Codex 讀不到 plan/spec（bwrap loopback + 檔未 commit→GitHub 404）** → 環境問題非缺陷；re-review 時把 snippet 內容**內嵌** prompt。已自驗 check 4/5：csk- 只在 `.env` 行、嵌入 Python 無反引號、Python f-string `{...}` 無 `$` 故不撞 JS `${...}`。
- **[LOW] 行 438 註解過時** → Task 3 Step 2 修正。
- **Codex 確認**：`_serialize_agent()` 回 `bound_collection_id`、守門碼忠實於真 middleware、出向 RAG 契約正確、trial-military 無此 view 故 Task 3 N/A。
- **讀 wizard 額外修正**：env 對齊 `CSP_BASE_URL`（非 `CSP_URL`）、不 bake admin localhost origin、出向 RAG **僅綁 collection 才顯示**並內嵌真實 id、**移除 `cspUrl` prop**。

### Codex impl-review（實作審）已處理 — APPROVE-WITH-NITS
- **[LOW] 已修**：詳情 modal 路徑出向 snippet 讀 `CSP_BASE_URL` 卻無 .env 指引 → `pythonInboundGuard` 在綁 collection 時於 `.env` 區自帶 `CSP_BASE_URL` 佔位（loopback 警語）。
- **[LOW] rotate 多 active 憑證 edge case → 經 user 要求已「治本+治標」修復**（獨立 concern，分開 commit）：
  - **治本**：`get_active_plaintext_for_agent` 出向選 token 改 `order_by(coalesce(rotated_at, issued_at) DESC, id DESC)` → issue/rotate 完那把就是 CSP 派送的那把；test-connection 探針 refactor 成複用同一 selector（移除 orphan `decode_service_token_envelope` import）。TDD：`test_dispatch_prefers_most_recently_changed_credential`（RED→GREEN）+ `test_dispatch_tie_breaks_deterministically_on_credential_id`。
  - **治標**：`DeveloperAgentsView.vue` 憑證表格對派送中那把標 `CSP 派送中` badge（`dispatchedCredentialId` computed 鏡像後端 coalesce+id tie-break）。
  - **Codex 審**：APPROVE-WITH-NITS（6 檢查全過；唯一 LOW=tie-break 不確定性，已加 `, id DESC` 兩層一致採納）。`npm run build` 綠、dispatch 測試綠。零 regression（8 個 SQLite naive/aware datetime 失敗 baseline main 同樣有）。
- 其餘 5 檢查（守門忠實/出向契約/csk- 不洩/Vue 無 TDZ/無回歸）全 confirmed。`npm run build` 綠。

---

## File Structure

- **Create** `myCSPPlatform/frontend/src/components/agents/inboundGuardSnippets.js` — `pythonInboundGuard(ctx)`、`pythonOutboundRag(ctx)`、`buildGuardSnippets(ctx)`；`ctx={csk, collectionId?}`。
- **Create** `myCSPPlatform/frontend/src/components/agents/AgentGuardPanel.vue` — 入向恆顯示、出向 `v-if` 非空才顯示；props `csk`(必)/`collectionId`(選)。
- **Modify** `myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue` — import + 兩個 collectionId computed + 兩處掛點（詳情 modal csk- 橫幅、register 精靈 step-2）+ 修行 438 註解。

---

## Task 1: 入向守門 + 出向 RAG snippet builders

**Files:** Create `myCSPPlatform/frontend/src/components/agents/inboundGuardSnippets.js`

- [ ] **Step 1: 寫 builder 模組**

```javascript
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
// (CSP_BASE_URL / CSP_SERVICE_TOKEN / ANILA_COLLECTION_ID — see
// newAgentEnvSnippet in DeveloperAgentsView.vue). Mirrors bootstrapSnippets.js.

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
  // 綁 collection 時，出向 snippet 還需 CSP_BASE_URL → 在 .env 區一併帶出，
  // 讓詳情 modal 路徑也自足（沿用 newAgentEnvSnippet 的 loopback 佔位慣例）。
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
```

- [ ] **Step 2: 建置驗證**

Run: `cd myCSPPlatform/frontend && npm run build`
Expected: 成功（純 ESM、無新相依）。

---

## Task 2: AgentGuardPanel 顯示元件

**Files:** Create `myCSPPlatform/frontend/src/components/agents/AgentGuardPanel.vue`

- [ ] **Step 1: 寫元件**

```vue
<!--
  Inbound-guard + outbound-RAG how-to, surfaced after a csk- is issued
  (detail-modal issue-static / rotate, register wizard step-2) for agents
  that DON'T fork the AgenticRAG template. The inbound guard always shows;
  the outbound RAG block shows only when a collection is bound. The csk- is
  pre-filled into the .env line only; the code reads it from os.environ.
  Mirrors BootstrapHowToTabs styling (TermBox / TermButton).
-->
<template>
  <TermBox
    title="non-template agent — wire this csk-"
    inset
    hint="copy the inbound guard + RAG usage for a plain FastAPI agent"
  >
    <section class="guard__section">
      <header class="guard__head">
        <span class="guard__label">① inbound guard（驗 Router 來源 · fail-closed）</span>
        <TermButton
          size="sm"
          variant="ghost"
          :label="copied === 'in' ? 'copied!' : 'copy'"
          @click="copy('in')"
        />
      </header>
      <pre class="guard__code"><code>{{ snippets.inboundGuard }}</code></pre>
    </section>

    <section v-if="snippets.outboundRag" class="guard__section">
      <header class="guard__head">
        <span class="guard__label">② outbound RAG（同一把 csk- 查綁定 collection）</span>
        <TermButton
          size="sm"
          variant="ghost"
          :label="copied === 'out' ? 'copied!' : 'copy'"
          @click="copy('out')"
        />
      </header>
      <pre class="guard__code"><code>{{ snippets.outboundRag }}</code></pre>
    </section>
  </TermBox>
</template>

<script setup>
import { computed, ref } from 'vue'
import { TermBox, TermButton } from '../cli'
import { buildGuardSnippets } from './inboundGuardSnippets.js'

const props = defineProps({
  csk:          { type: String, required: true },
  collectionId: { type: Number, default: undefined },
})

const copied = ref('')

const snippets = computed(() =>
  buildGuardSnippets({ csk: props.csk, collectionId: props.collectionId })
)

async function copy(which) {
  const text =
    which === 'in' ? snippets.value.inboundGuard : snippets.value.outboundRag
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copied.value = which
    setTimeout(() => (copied.value = ''), 1500)
  } catch {
    // Clipboard API unavailable — non-fatal; user can select the text.
    copied.value = ''
  }
}
</script>

<style scoped>
.guard__section {
  margin-bottom: var(--gap-3);
}
.guard__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--gap-2);
  margin-bottom: var(--gap-2);
}
.guard__label {
  font-size: var(--t-2xs);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--c-fg-2);
}
.guard__code {
  margin: 0;
  padding: var(--gap-3);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  font-family: var(--font-mono);
  font-size: var(--t-xs);
  line-height: 1.5;
  white-space: pre;
  overflow-x: auto;
  overflow-y: auto;
  max-height: 320px;
  color: var(--c-fg-1);
}
</style>
```

- [ ] **Step 2: 建置驗證** — `cd myCSPPlatform/frontend && npm run build`（TermBox/TermButton 自 `../cli`，同 BootstrapHowToTabs）。

---

## Task 3: 詳情 modal 的 csk- 橫幅掛點（issue-static + rotate）

**Files:** Modify `myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue`

- [ ] **Step 1: 加 import（`import BootstrapHowToTabs ...` 之後，~行 580）**

```javascript
import AgentGuardPanel from '../components/agents/AgentGuardPanel.vue'
```

- [ ] **Step 2: 修行 438 過時註解**

把：
```
                 Only shown for bsk- since the csk- path differs (admin
                 hand-installs the token; no exchange flow). -->
```
改成：
```
                 bsk- uses this per-language exchange how-to; the csk-
                 path has its own guard block below. -->
```

- [ ] **Step 3: 加兩個 collectionId computed（`issuedSecret` 宣告附近，~行 618 之後）**

```javascript
// Bound collection id for the csk- guard panel's outbound-RAG snippet.
// undefined (→ panel hides the RAG block) when the agent has no bound
// collection or the payload doesn't carry it.
const detailAgentCollectionId = computed(() =>
  typeof detailAgent.value?.bound_collection_id === 'number'
    ? detailAgent.value.bound_collection_id
    : undefined
)
const registeredAgentCollectionId = computed(() =>
  typeof registeredAgent.value?.bound_collection_id === 'number'
    ? registeredAgent.value.bound_collection_id
    : undefined
)
```

- [ ] **Step 4: 加 csk- `<details>`（緊接 bsk- `<details>` 結束 `</details>` 之後，~行 454）**

```html
            <!-- csk- direct-issue / rotate: non-template agents need the
                 inbound guard (anila_core isn't pip-installable) + (when a
                 collection is bound) the outbound RAG usage. -->
            <details
              v-if="issuedSecret.kind === 'csk' && issuedSecret.meta"
              class="secret-banner__howto"
              open
            >
              <summary class="secret-banner__howto-summary">非模板 agent？如何接上這把 csk- →</summary>
              <AgentGuardPanel
                :csk="issuedSecret.value"
                :collection-id="detailAgentCollectionId"
              />
            </details>
```

- [ ] **Step 5: 建置驗證** — `npm run build`，無未使用 import / 未定義變數。

---

## Task 3b: register 精靈 step-2 掛點（Codex [HIGH]）

**Files:** Modify `myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue`

- [ ] **Step 1: 在 step-2 `.env` 區之後、`verify connection` 之前插入（~行 279 與 281 之間）**

於 `<TermButton ... label="copy .env" />`（~行 279）與 `<TermSection title="verify connection" />`（~行 281）之間插入：

```html
          <TermSection title="inbound guard + RAG usage（非模板 agent）" />
          <AgentGuardPanel
            :csk="newAgentCsk"
            :collection-id="registeredAgentCollectionId"
          />
```

理由：先 `.env` → 再貼守門碼 → 才 `verify connection`（驗證入向 token 被接受才有意義）。`registeredAgentCollectionId` computed 已於 Task 3 Step 3 一併加入。

- [ ] **Step 2: 建置驗證** — `npm run build`。

---

## Task 4: 端到端人工驗證

- [ ] **Step 1: build 全綠** — `cd myCSPPlatform/frontend && npm run build`。
- [ ] **Step 2: 人工煙霧測**（dev 伺服器或本機 stack 前端）：
  - **詳情 modal**：開已綁 collection 的 agent → issue static (csk-) → 橫幅下 `<details>` 出現面板，兩塊（① 守門 ② RAG，RAG 內嵌正確 collection id）；未綁 collection 的 agent → 只出現 ① 守門、無 ②。
  - **rotate**：rotate 一把 csk- → 同樣出現面板（共用 `issuedSecret.kind==='csk'`）。
  - **register 精靈**：跑註冊 → step-2 issue csk- → `.env` 區與 verify 之間出現面板；綁了 collection 則含 ② 區。
  - csk- 只在 `.env` 行；middleware/RAG 程式內**無** csk- 硬編；兩 copy 按鈕各自運作。
  - bsk- 流程不受影響（仍只顯示 `BootstrapHowToTabs`）；行 438 註解已更新。

---

## Task 5: 散 7 分支（**commit 一律等 user 點頭**）

- [ ] **Step 1**：main 起 canonical commit（待 user 要求）。
- [ ] **Step 2**：cherry-pick `-x` → dev-public / dev-military / prod-public-passwd / prod-military-passwd / prod-intranet-card。
- [ ] **Step 3**：`trial-military` —— 已移除 `DeveloperAgentsView.vue` 且 router 無 `developer/agents`（Codex 確認）→ **跳過 Task 3/3b**；新檔（Task 1/2）可落可不落（功能 N/A）；以該分支實際樹狀解 modify/delete，不強推 view。
- [ ] **Step 4**：各分支 `npm run build` 抽驗（trial-military 除外）後 push origin。

---

## Self-Review

- **Spec coverage**：D1 Python-only ✓、D2 入向+出向 ✓、D3 fail-closed ✓、D4 csk- 只進 .env 行 ✓、D5 後端不動 ✓、D6 三掛點（issue-static/rotate/register 精靈）↔ Task 3+3b ✓、D7 `_PUBLIC_PATHS` 對齊 ✓、D8 散 7 分支+trial N/A ✓、D9 `CSP_BASE_URL`/出向僅綁 collection ✓。
- **Placeholder scan**：`csk-PASTE-FROM-ADMIN-UI` 為執行期佔位（非 plan 缺口）；無 TODO/TBD。
- **型別一致**：`buildGuardSnippets` 回 `{ inboundGuard, outboundRag }` ↔ 元件 `snippets.value.inboundGuard/outboundRag` 一致；props `csk`(String)/`collectionId`(Number) ↔ 兩掛點 `:csk`/`:collection-id` 一致；computed 皆回 `number|undefined` 對齊 prop 型別。
- **JS template-literal 安全**：嵌入 Python 無反引號；f-string `{...}` 無 `$`，不撞 JS `${...}`；刻意內插僅 `${cskLiteral}`/`${ctx.collectionId}`。
