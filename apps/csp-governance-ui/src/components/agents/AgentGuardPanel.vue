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
