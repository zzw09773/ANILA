<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">打造 ANILA agent</h1>
        <p class="page-head__sub">
          在 MLSteam 開發 anila-agent · 模型走 CSP 真實 URL → 產 system prompt → port forward → 註冊 → app.py
        </p>
      </div>
    </header>

    <!-- TL;DR -->
    <TermBox title="tl;dr · 完整流程" pad="md">
      <ol class="tldr">
        <li>MLSteam 用樣板 image 建 Lab，掛載 <code>anila</code> 源碼資料夾到 workspace，<code>cp .env.example .env</code></li>
        <li><code>.env</code> 設模型：<code>ANILA_BASE_URL</code>＝<strong>CSP 平台真實 URL</strong>（非 docker 內部名）、<code>ANILA_MODEL</code>＝CSP 複製的 model 名、<code>ANILA_API_KEY</code>＝CSP 核發的 key</li>
        <li>用下方 <a href="#generator">🛠 產生器</a> 產 system prompt → 取代 <code>prompts/system.md</code></li>
        <li>MLSteam 設 port forwarding → 到 <router-link to="/developer/agents">/developer/agents</router-link> 註冊（endpoint 用 forward 後位址、填 <code>http://</code>），system prompt 貼到 description</li>
        <li>領 <code>csk-</code> → 把 CSP 給的那串貼進 <code>.env</code> → <code>python app.py</code> → 回 CSP 按 test connection</li>
      </ol>
    </TermBox>

    <!-- 這是什麼 -->
    <TermBox id="what" title="這是什麼（1.0.0）" pad="md">
      <p class="lead">
        <code>anila-agent</code> 是 air-gapped Agentic RAG 起手樣板，runtime 為
        <code>openai-agents</code> SDK <strong>0.17.5</strong>。內建 RAG 檢索、deny-all 工具政策、
        memdir 長期記憶、以及 <strong>service wrapper</strong>（<code>app.py</code> 一鍵起 OpenAI 相容服務）。
        填上 CSP 模型端點與 collection 即可跑，再註冊到 CSP router 對外服務。
      </p>
      <p class="hint">
        <strong>air-gap by construction：</strong>強制 Chat Completions、關閉 tracing exporter（不外連）、
        wire 層剝除 <code>strict</code> 工具欄位、reasoning 模型 <code>max_tokens</code> 下限。
      </p>
    </TermBox>

    <!-- system prompt 產生器 -->
    <TermBox id="generator" title="🛠 領域 system prompt 產生器" pad="md">
      <p class="lead">
        選一個 collection（知識庫）＋ 寫下你的 agent 構想 → LLM 會抽該知識庫的文件當依據，
        產生一份貼合領域的 system prompt。複製貼進你 anila-agent 的
        <code>prompts/system.md</code>，或直接存成某 agent 的 preset。
      </p>
      <div class="gen-form">
        <TermField label="collection（你的知識庫）">
          <select v-model="gen.collectionId" class="gen-input">
            <option value="">— 選一個 collection —</option>
            <option v-for="c in collections" :key="c.id" :value="c.id">
              #{{ c.id }} · {{ c.name }}（{{ c.document_count }} 份文件）
            </option>
          </select>
          <p v-if="!collections.length" class="hint">（沒看到 collection？先在平台建知識庫並 ingest 文件，或確認你已登入。）</p>
        </TermField>
        <TermField label="初始想法（agent 用途 / 對象 / 語氣）">
          <textarea v-model="gen.ideas" class="gen-input" rows="4"
            placeholder="例：給法務同仁的內規問答助手，只依公司規章回答，語氣正式，遇到規章沒寫的要請對方洽人資。"></textarea>
        </TermField>
        <div class="gen-actions">
          <TermButton :disabled="!gen.collectionId || !gen.ideas.trim() || gen.loading" @click="generate">
            {{ gen.loading ? '產生中…' : '產生 system prompt' }}
          </TermButton>
          <span v-if="gen.error" class="gen-err">{{ gen.error }}</span>
        </div>
      </div>

      <div v-if="gen.result" class="gen-result">
        <TermField label="產生的 system prompt（可編輯）">
          <textarea v-model="gen.result" class="gen-input gen-output" rows="10"></textarea>
        </TermField>
        <div class="gen-actions">
          <TermButton @click="copyResult">{{ gen.copied ? '✓ 已複製' : '複製' }}</TermButton>
          <span class="gen-sep">存成 preset →</span>
          <select v-model="gen.saveAgentId" class="gen-input gen-input--sm">
            <option value="">選 agent</option>
            <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
          </select>
          <TermButton :disabled="!gen.saveAgentId || gen.saving" @click="saveAsPreset">
            {{ gen.saving ? '存檔中…' : '存成 preset' }}
          </TermButton>
          <span v-if="gen.saveMsg" class="gen-msg">{{ gen.saveMsg }}</span>
        </div>
      </div>
    </TermBox>

    <!-- 完整流程 -->
    <TermBox id="flow" title="建立 agent 完整流程（11 步）" pad="md">
      <p class="lead">
        實際跑下來的步驟。<strong>最關鍵：agent 在 MLSteam，連不到 CSP 的 docker 內部名</strong>，
        所以模型與檢索都要走 CSP 平台的<strong>真實對外 URL</strong>。
      </p>
      <ol class="steps">
        <li><strong>建 Lab</strong>：MLSteam 用 anila-agent 樣板 image 建一個 Lab（image 只是環境、自帶 JupyterLab）。</li>
        <li><strong>掛載源碼</strong>：把 <code>anila</code> 資料夾（含 anila-agent 源碼 + configs）掛載進 workspace；<strong>在 repo 目錄內</strong>操作（configs 走 CWD 相對載入）。</li>
        <li><strong>建 .env</strong>：<code>cp .env.example .env</code>。</li>
        <li><strong>模型端點</strong>：<code>ANILA_BASE_URL</code> 改成 <strong>CSP 平台真實 URL</strong>（<code>https://&lt;csp-host&gt;/v1</code>，例 <code>https://172.16.120.35/v1</code>）。<strong>不是</strong> <code>http://gpt-oss-20b:8000</code> 那種 docker 內部名 —— MLSteam 連不到。</li>
        <li><strong>模型名</strong>：到 CSP 平台複製 model name → 取代 <code>ANILA_MODEL</code>（例 <code>openai/gpt-oss-20b</code>）。</li>
        <li><strong>模型 key</strong>：在 CSP 核發一把 API key → 取代 <code>ANILA_API_KEY</code>（agent 用它打 CSP 的 <code>/v1</code>）。CSP 自簽 https → 加 <code>ANILA_SSL_VERIFY=0</code>。</li>
        <li><strong>system prompt</strong>：用上方 <a href="#generator">🛠 產生器</a>（選 collection + 寫構想）產一份 → 取代 anila-agent 的 <code>anila_agent/prompts/system.md</code>。</li>
        <li><strong>port forwarding</strong>：MLSteam 設 port forwarding 把 agent 的 <code>:8200</code> 對外 → 到 <router-link to="/developer/agents">/developer/agents</router-link> 用 forward 後的位址註冊。<strong>endpoint 填 <code>http://</code>（agent 跑純 http；填 https 會 SSL WRONG_VERSION_NUMBER）。</strong></li>
        <li><strong>description</strong>：把 system prompt（或其摘要）貼到註冊的 <code>description</code> —— <strong>router 靠它判斷要不要把對話派給這支 agent</strong>。</li>
        <li><strong>csk-</strong>：核發 <code>csk-</code> service token，把 CSP 給的那串（<code>CSP_BASE_URL</code> / <code>CSP_SERVICE_TOKEN</code> / <code>ANILA_COLLECTION_ID</code>）複製貼進 MLSteam 的 <code>.env</code>。</li>
        <li><strong>啟動</strong>：<code>python app.py</code>（= <code>make serve</code>，起 <code>:8200</code>）→ 回 CSP 按 <strong>test connection</strong> → 審核後 router 自動發現上線。</li>
      </ol>
    </TermBox>

    <!-- .env 對照 -->
    <TermBox id="env" title=".env 對照（步驟 3–6、10 的結果）" pad="md">
      <pre class="code"># ── 模型：走 CSP 平台 OpenAI 相容端點（步驟 4–6）──
#   ⚠ CSP 平台真實 URL，不是 docker 內部名（agent 在 MLSteam，連不到內部名）
ANILA_BASE_URL=https://&lt;csp-host&gt;/v1      # 例 https://172.16.120.35/v1
ANILA_MODEL=openai/gpt-oss-20b            # 從 CSP 平台複製
ANILA_API_KEY=&lt;CSP 核發的 API key&gt;
ANILA_SSL_VERIFY=0                        # CSP 自簽 https → 0

# ── 檢索 + 派工：註冊後把 CSP 給的那串貼進來（步驟 10）──
CSP_BASE_URL=https://&lt;csp-host&gt;
CSP_SERVICE_TOKEN=csk-...                 # 註冊時核發；空則派工回 401
ANILA_COLLECTION_ID=&lt;collection 數字 ID&gt;   # 見下方「指定 collection」

# ── 選用 ──
ANILA_MEMORY=0  ·  ANILA_CITED=0  ·  ANILA_MAX_TURNS=10  ·  ANILA_TIMEOUT=60</pre>
      <p class="hint">
        <strong>⚠ 兩套 CSP 名別搞混：</strong>跑 <code>python app.py</code>（service）→ 用
        <code>CSP_BASE_URL</code> ＋ <code>CSP_SERVICE_TOKEN</code>；改用 <code>anila</code> CLI →
        改讀 <code>ANILA_CSP_BASE_URL</code> ＋ <code>ANILA_CSP_API_KEY</code>。<code>ANILA_COLLECTION_ID</code> 兩邊共用。
      </p>
    </TermBox>

    <!-- Collection -->
    <TermBox id="collection" title="指定 collection" pad="md">
      <p class="lead">
        <code>ANILA_COLLECTION_ID</code> 是 <strong>CSP 平台某個知識庫（collection）的數字 ID</strong>。
        agent 的 RAG 就查這個 collection。
      </p>
      <ol class="steps">
        <li>在 CSP 平台建一個 <strong>collection</strong> 並 ingest 你的文件（embedding 由 CSP 端做）。</li>
        <li>取得它的 <strong>ID（正整數）</strong>，填進 <code>.env</code> 的 <code>ANILA_COLLECTION_ID</code>。</li>
        <li>搭配 <code>CSP_BASE_URL</code> ＋ <code>CSP_SERVICE_TOKEN</code> →
          agent 走 <code>POST /api/ingestion/collections/&#123;id&#125;/search</code>，
          <strong>認證 / 嵌入 / RLS 都在 CSP 端</strong>，agent 不碰 DB、不碰嵌入模型。</li>
      </ol>
    </TermBox>

    <!-- 常見的坑 -->
    <TermBox id="gotchas" title="常見的坑" pad="md">
      <table class="term-table">
        <thead><tr><th style="width: 240px">症狀</th><th>原因 / 解法</th></tr></thead>
        <tbody>
          <tr>
            <td>test connection <code>SSL: WRONG_VERSION_NUMBER</code></td>
            <td>agent 跑純 <strong>http</strong>，你卻把 endpoint 註冊成 <code>https://</code>。改填 <code>http://&lt;forward 位址&gt;</code>。</td>
          </tr>
          <tr>
            <td>agent 一直 <code>MaxTurnsExceeded</code> / 不回應</td>
            <td>gpt-oss（reasoning 模型）對同一題反覆呼叫檢索工具、不收斂。<code>system.md</code> 明寫「檢索一次就依結果作答、勿反覆檢索」；仍不穩可調 <code>ANILA_MAX_TURNS</code>。</td>
          </tr>
          <tr>
            <td>CSP search 回 <code>embedding_model ... not registered</code></td>
            <td>collection 建立時的 embedding 名（大小寫敏感，如 <code>nvidia/NV-embed-V2</code>）要存在 model_registry。請 admin 在平台補上該名。</td>
          </tr>
          <tr>
            <td>找不到 <code>configs/*.yaml</code> / 政策載入失敗</td>
            <td>沒在 repo 目錄內跑。<code>cd anila-agent</code> 再 <code>python app.py</code>（configs 走 CWD 相對）。</td>
          </tr>
          <tr>
            <td>service 全 401 / chat 503</td>
            <td>401＝沒設 <code>CSP_SERVICE_TOKEN</code>（本地測試設 <code>ANILA_ALLOW_NO_SERVICE_TOKEN=1</code>）；503＝缺 <code>ANILA_COLLECTION_ID</code>。</td>
          </tr>
          <tr>
            <td><code>APIConnectionError</code>（curl 卻通）</td>
            <td>自寫腳本沒 <code>load_dotenv()</code> → <code>.env</code> 沒生效退回 localhost。腳本開頭加 <code>load_dotenv()</code>（<code>app.py</code> / <code>anila</code> CLI 已自動載）。</td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- 後續 -->
    <TermBox title="後續" pad="sm">
      <ul class="next">
        <li>上游 ＋ CHANGELOG：<code>github.com/zzw09773/anila-agent</code></li>
        <li>image / MLSteam 細節：repo 內 <code>DOCKER.md</code></li>
        <li>完整重建藍圖與架構決策：repo 內 <code>REBUILD_PLAN.md</code></li>
        <li>到 <router-link to="/developer/agents">/developer/agents</router-link> 註冊你的 agent</li>
      </ul>
    </TermBox>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { extractError } from '../api/errors'
import { TermBox, TermField, TermButton } from '../components/cli'
import client from '../api/client'

const collections = ref([])
const agents = ref([])
const gen = reactive({
  collectionId: '', ideas: '', result: '', loading: false, error: '',
  copied: false, saveAgentId: '', saving: false, saveMsg: '',
})

onMounted(async () => {
  try { collections.value = (await client.get('/api/ingestion/collections')).data } catch { /* 未登入 / 無權限：留空，UI 有提示 */ }
  try { agents.value = (await client.get('/api/agents')).data } catch { /* ignore */ }
})

async function generate() {
  gen.error = ''; gen.result = ''; gen.copied = false; gen.saveMsg = ''; gen.loading = true
  try {
    const { data } = await client.post('/api/agents/system-prompt/suggest', {
      collection_id: Number(gen.collectionId),
      ideas: gen.ideas,
    })
    gen.result = data.system_prompt
  } catch (e) {
    gen.error = extractError(e, '產生失敗，請稍後再試')
  } finally {
    gen.loading = false
  }
}

async function copyResult() {
  try {
    await navigator.clipboard.writeText(gen.result)
    gen.copied = true
    setTimeout(() => { gen.copied = false }, 2000)
  } catch { /* clipboard 不可用時略過 */ }
}

async function saveAsPreset() {
  gen.saveMsg = ''; gen.saving = true
  try {
    await client.post(`/api/agents/${gen.saveAgentId}/functions`, {
      kind: 'preset_prompt',
      label: '領域系統提示（產生）',
      config: { text: gen.result },
      sort_order: 0,
    })
    gen.saveMsg = '✓ 已存成該 agent 的 preset'
  } catch (e) {
    gen.saveMsg = extractError(e, '存檔失敗')
  } finally {
    gen.saving = false
  }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--gap-4); }
.page-head__title { font-size: var(--t-xl); font-weight: 500; color: var(--c-fg-1); margin: 0 0 4px; }
.page-head__sub { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; }

.lead { font-size: var(--t-sm); color: var(--c-fg-2); margin: 0 0 var(--gap-3); }
.lead strong { color: var(--c-fg-1); }

.tldr, .steps {
  list-style: decimal inside; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.steps strong { color: var(--c-fg-1); }

.feats {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.feats strong { color: var(--c-fg-1); }

.tldr code, .steps code, .feats code, .next code, .term-table code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}

.code {
  margin: var(--gap-2) 0; padding: var(--gap-3);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border);
  font-family: var(--font-mono); font-size: var(--t-2xs);
  color: var(--c-fg-1); white-space: pre; overflow-x: auto;
  line-height: 1.55;
}

.hint {
  font-size: var(--t-xs); color: var(--c-fg-3); margin: 4px 0 0;
  font-style: italic;
}
.hint strong { color: var(--c-fg-2); font-style: normal; }

.next {
  list-style: none; padding: 0; margin: 0;
  display: flex; flex-direction: column; gap: var(--gap-2);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.next a, .tldr a, .steps a { color: var(--c-accent); text-decoration: none; }
.next a:hover, .tldr a:hover, .steps a:hover { text-decoration: underline; }

.term-table { width: 100%; }

/* system prompt 產生器 */
.gen-form { display: flex; flex-direction: column; gap: var(--gap-3); }
.gen-result { margin-top: var(--gap-3); }
.gen-input {
  width: 100%; box-sizing: border-box;
  background: var(--c-bg); color: var(--c-fg-1);
  border: var(--border-w) solid var(--c-border); border-radius: 0;
  font-family: inherit; font-size: var(--t-sm); padding: 6px 8px;
}
.gen-input--sm { width: auto; min-width: 140px; }
.gen-output { font-family: var(--font-mono); font-size: var(--t-2xs); line-height: 1.55; white-space: pre-wrap; }
.gen-input:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 1px; }
.gen-actions { display: flex; flex-wrap: wrap; align-items: center; gap: var(--gap-2); margin-top: var(--gap-2); }
.gen-sep { color: var(--c-fg-mute); font-size: var(--t-xs); margin-left: var(--gap-2); }
.gen-err { color: var(--c-danger, #b3261e); font-size: var(--t-xs); }
.gen-msg { color: var(--c-ok, #157f4a); font-size: var(--t-xs); }
</style>
