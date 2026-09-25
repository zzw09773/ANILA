<!--
  P2.1 三級接入：範本／單檔驗簽／sidecar。
  畫面上不出現任何要保管的祕密字串。
  每一級：今日能端到端就寫作法；不能就用畫面文案說清楚缺什麼。
-->
<template>
  <TermBox
    title="接入驗簽 · 三級制"
    inset
    hint="平台每次派工現簽 5 分鐘憑條；你的 agent 用公開 JWKS 驗簽，不領任何鑰匙"
  >
    <section class="guard__section">
      <header class="guard__head">
        <span class="guard__label">① 新 agent — 快速起步（先下載，在 lab 啟動後再註冊）</span>
      </header>
      <p class="guard__prose">
        一般路徑先下載通用快速起步，在 MLSteam lab 改 <code>agent.py</code> 並啟動。
        設好 port forwarding 後再註冊，把 agent id 填回 deployment.env 後重啟。
        快速起步已是可運行的服務，不用再包一層 FastAPI。
        需要工具迴圈或長期狀態時，改下載進階實作範例；那是另一個下載，不是這包的下一章。
        發行包若尚未備妥，下載會顯示失敗，不會假裝已下載。
        註冊要填名稱、至少 24 字的用途說明、endpoint，以及基礎模型。不核發長效祕密。
      </p>
    </section>

    <section class="guard__section">
      <header class="guard__head">
        <span class="guard__label">② 既有 Python 服務 — 單檔 anila_verify.py</span>
        <div class="guard__actions">
          <TermButton
            size="sm"
            variant="ghost"
            label="下載 anila_verify.py"
            @click="downloadVerify"
          />
          <TermButton
            size="sm"
            variant="ghost"
            :label="copied === 'py' ? '已複製' : '複製片段'"
            @click="copy('py')"
          />
        </div>
      </header>
      <p class="guard__prose">
        按「下載 anila_verify.py」取得單檔（stdlib＋cryptography），放到你的服務旁，
        再接上下列幾行。無需向平台申請任何憑證。
        此檔由<strong>治理中心發行</strong>，給既有服務自行接驗簽；不是快速起步 zip 的替代品。
        <strong>若下載端點尚未上線會顯示提示</strong>——在那之前則此級暫時無法在氣隙內取得該檔。
      </p>
      <p v-if="dlMsg" class="guard__notice" :class="dlOk ? 'is-ok' : 'is-err'">{{ dlMsg }}</p>
      <pre class="guard__code"><code>{{ snippets.pythonVerify }}</code></pre>
    </section>

    <section class="guard__section">
      <header class="guard__head">
        <span class="guard__label">③ 無法改碼 — 驗證 sidecar</span>
      </header>
      <p class="guard__prose">
        規劃中的選項：在 agent 前方放驗證 sidecar（驗完再轉發，本體不動），
        映像走內網既有搬運通道。
        <strong>今日尚無公開映像與部署說明</strong>——若你無法改碼，請先走①快速起步，
        或等候 sidecar 套件釋出；此處不捏造操作步驟。
      </p>
    </section>

    <section class="guard__section">
      <header class="guard__head">
        <span class="guard__label">非祕密設定（可選）</span>
        <TermButton
          size="sm"
          variant="ghost"
          :label="copied === 'env' ? '已複製' : '複製'"
          @click="copy('env')"
        />
      </header>
      <p class="guard__prose">
        僅平台位址與 CA 檔路徑。用頁面「下載平台 CA」嘗試取得 PEM
        （<strong>端點未上線時會提示，勿假設檔案已到手</strong>），
        以 <code>ANILA_CA_FILE</code> 指向它——<strong>不要</strong>設 <code>SSL_CERT_FILE</code>。
      </p>
      <pre class="guard__code"><code>{{ snippets.env }}</code></pre>
    </section>
  </TermBox>
</template>

<script setup>
import { computed, ref } from 'vue'
import { TermBox, TermButton } from '../cli'
import { downloadAnilaVerify } from '../../api/agents'
import { buildVerifySnippets } from './verifySnippets.js'
import { extractError } from '../../api/errors'

const props = defineProps({
  cspUrl: { type: String, default: '' },
  caPath: { type: String, default: '/path/to/cspki_ca_bundle.pem' },
})

const copied = ref('')
const dlMsg = ref('')
const dlOk = ref(false)

const snippets = computed(() =>
  buildVerifySnippets({ cspUrl: props.cspUrl, caPath: props.caPath })
)

async function copy(which) {
  const text =
    which === 'py' ? snippets.value.pythonVerify : snippets.value.env
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    copied.value = which
    setTimeout(() => (copied.value = ''), 1500)
  } catch {
    copied.value = ''
  }
}

async function downloadVerify() {
  dlMsg.value = ''
  dlOk.value = false
  try {
    const { data } = await downloadAnilaVerify()
    const url = URL.createObjectURL(
      new Blob([data], { type: 'text/x-python; charset=utf-8' })
    )
    const link = document.createElement('a')
    link.href = url
    link.download = 'anila_verify.py'
    link.click()
    URL.revokeObjectURL(url)
    dlOk.value = true
    dlMsg.value = 'anila_verify.py 已下載'
  } catch (e) {
    const status = e.response?.status
    if (status === 404) {
      dlMsg.value =
        'anila_verify.py 下載端點尚未上線（需後端提供 GET /api/agents/anila-verify/download）'
    } else {
      dlMsg.value = extractError(e, '下載 anila_verify.py 失敗')
    }
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
.guard__actions {
  display: inline-flex;
  align-items: center;
  gap: var(--gap-2);
  flex-shrink: 0;
}
.guard__label {
  font-size: var(--t-2xs);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--c-fg-2);
}
.guard__prose {
  margin: 0 0 var(--gap-2);
  font-size: var(--t-xs);
  color: var(--c-fg-2);
  line-height: 1.55;
}
.guard__prose code {
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 4px;
  color: var(--c-accent);
}
.guard__prose strong {
  color: var(--c-fg-1);
  font-weight: 500;
}
.guard__notice {
  margin: 0 0 var(--gap-2);
  font-size: var(--t-2xs);
  padding: var(--gap-2) var(--gap-3);
  border: var(--border-w) solid;
  line-height: 1.45;
}
.guard__notice.is-ok {
  color: var(--c-ok);
  border-color: var(--c-ok);
  background: var(--c-ok-soft);
}
.guard__notice.is-err {
  color: var(--c-danger);
  border-color: var(--c-danger);
  background: var(--c-danger-soft);
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
  max-height: 280px;
  color: var(--c-fg-1);
}
</style>
