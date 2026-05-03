<!--
  How-to tabs surfaced after admin issues a bsk- bootstrap token.

  Shows five mutually-exclusive panes:
    - AgenticRAG fork — pure .env + docker compose, no code
    - Python  — httpx-based reference impl
    - Node.js — fetch-based reference impl
    - Go      — net/http reference impl
    - curl    — bash + jq for ops / debugging

  Plus a [docs] link to the full protocol spec for any other language.

  Each snippet is pre-filled with the agent's id / endpoint_url / bsk-
  so devs can copy without editing. The bsk- is one-shot; this panel
  is shown immediately after issue and clears with the secret-banner.
-->
<template>
  <TermBox title="how to use this bootstrap token" inset hint="copy a snippet for your agent's language">
    <nav class="how-to__tabs" role="tablist">
      <button
        v-for="t in tabs"
        :key="t.id"
        class="how-to__tab"
        :class="{ 'how-to__tab--active': active === t.id }"
        :aria-selected="active === t.id"
        role="tab"
        @click="active = t.id"
      >
        {{ t.label }}
      </button>
      <a
        class="how-to__tab how-to__tab--link"
        :href="docsUrl"
        target="_blank"
        rel="noopener"
      >docs ↗</a>
    </nav>

    <div class="how-to__pane">
      <p v-if="activeTab.note" class="how-to__note">{{ activeTab.note }}</p>
      <div class="how-to__codewrap">
        <pre class="how-to__code"><code>{{ snippetText }}</code></pre>
        <TermButton
          class="how-to__copy"
          size="sm"
          variant="ghost"
          :label="copied ? 'copied!' : 'copy'"
          @click="copy"
        />
      </div>
    </div>
  </TermBox>
</template>

<script setup>
import { computed, ref } from 'vue'
import { TermBox, TermButton } from '../cli'
import { buildAllSnippets } from './bootstrapSnippets.js'

const props = defineProps({
  cspUrl:       { type: String,  required: true },
  agentId:      { type: Number,  required: true },
  endpointUrl:  { type: String,  required: true },
  bsk:          { type: String,  default: '' },
  docsUrl:      { type: String,  default: '/docs/csp-agent-bootstrap-protocol.md' },
})

const active = ref('agenticRagFork')
const copied = ref(false)

const tabs = [
  {
    id: 'agenticRagFork',
    label: 'AgenticRAG fork',
    note: '官方 RAG agent template — fork 後不需安裝 anila-core，bootstrap CLI 已內建。',
  },
  {
    id: 'python',
    label: 'Python',
    note: '自寫 Python agent（非 fork AgenticRAG）— httpx 範例。',
  },
  {
    id: 'node',
    label: 'Node.js',
    note: '自寫 TS / JS agent — fetch + ESM 範例。',
  },
  {
    id: 'go',
    label: 'Go',
    note: '自寫 Go agent — net/http 範例。',
  },
  {
    id: 'curl',
    label: 'curl',
    note: '純 bash + curl + jq — ops / debugging 用。',
  },
]

const snippets = computed(() =>
  buildAllSnippets({
    cspUrl:      props.cspUrl,
    agentId:     props.agentId,
    endpointUrl: props.endpointUrl,
    bsk:         props.bsk,
  })
)

const activeTab = computed(
  () => tabs.find((t) => t.id === active.value) || tabs[0]
)

const snippetText = computed(() => snippets.value[active.value] || '')

async function copy() {
  if (!snippetText.value) return
  try {
    await navigator.clipboard.writeText(snippetText.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    // Clipboard API not available — fall back to selecting the text
    // so the user can copy manually. Failure is non-fatal.
    copied.value = false
  }
}
</script>

<style scoped>
.how-to__tabs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--gap-1);
  border-bottom: var(--border-w) solid var(--c-border);
  margin-bottom: var(--gap-3);
}

.how-to__tab {
  appearance: none;
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--c-fg-2);
  cursor: pointer;
  font: inherit;
  font-size: var(--t-2xs);
  letter-spacing: 0.04em;
  padding: var(--gap-2) var(--gap-3);
  text-transform: uppercase;
  transition: color var(--duration-fast) var(--ease-out-expo),
              border-color var(--duration-fast) var(--ease-out-expo);
}
.how-to__tab:hover { color: var(--c-fg-1); }
.how-to__tab--active {
  color: var(--c-fg-1);
  border-bottom-color: var(--c-accent);
}
.how-to__tab--link {
  margin-left: auto;
  text-decoration: none;
  color: var(--c-fg-3);
}
.how-to__tab--link:hover { color: var(--c-fg-1); }

.how-to__note {
  margin: 0 0 var(--gap-2) 0;
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  line-height: 1.5;
}

.how-to__codewrap {
  position: relative;
}
.how-to__code {
  margin: 0;
  padding: var(--gap-3);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  font-family: var(--font-mono);
  font-size: var(--t-xs);
  line-height: 1.5;
  white-space: pre;
  overflow-x: auto;
  color: var(--c-fg-1);
  max-height: 360px;
  overflow-y: auto;
}
.how-to__copy {
  position: absolute;
  top: var(--gap-2);
  right: var(--gap-2);
}
</style>
