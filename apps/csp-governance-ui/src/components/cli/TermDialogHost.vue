<!--
  Singleton host for the promise-based useDialog() confirm/toast. Mounted once
  in App.vue. Renders the active confirm dialog (TermModal + TermButton) and a
  bottom-right toast stack. Views never render this directly — they call
  confirm()/toast() from useDialog().
-->
<template>
  <TermModal
    :visible="s.confirm.open"
    :title="s.confirm.title"
    width="460px"
    elevated
    @close="settle(false)"
  >
    <p class="term-confirm__msg">{{ s.confirm.message }}</p>
    <div v-if="s.confirm.requireText" class="term-confirm__verify">
      <label class="term-confirm__hint">
        請輸入「<code>{{ s.confirm.requireText }}</code>」以確認：
      </label>
      <input
        class="term-confirm__input"
        v-model="s.confirm.input"
        autocomplete="off"
        spellcheck="false"
        @keydown.enter.prevent="onEnter"
      />
    </div>
    <template #footer>
      <TermButton variant="ghost" :label="s.confirm.cancelText" @click="settle(false)" />
      <TermButton
        :variant="s.confirm.danger ? 'danger' : 'primary'"
        :disabled="!canConfirm"
        :label="s.confirm.confirmText"
        @click="settle(true)"
      />
    </template>
  </TermModal>

  <Teleport to="body">
    <div class="term-toasts" aria-live="polite">
      <div
        v-for="t in s.toasts"
        :key="t.id"
        class="term-toast"
        :class="`term-toast--${t.tone}`"
        :role="t.tone === 'error' ? 'alert' : 'status'"
      >
        {{ t.message }}
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { computed } from 'vue'
import TermModal from './TermModal.vue'
import TermButton from './TermButton.vue'
import { _dialogState as s, _settleConfirm as settle } from '../../composables/useDialog'

const canConfirm = computed(
  () => !s.confirm.requireText || s.confirm.input === s.confirm.requireText,
)
function onEnter() {
  if (canConfirm.value) settle(true)
}
</script>

<style scoped>
.term-confirm__msg {
  color: var(--c-fg-2);
  font-size: var(--t-base);
  white-space: pre-wrap;
  line-height: var(--lh-base);
}
.term-confirm__verify {
  margin-top: var(--gap-3);
}
.term-confirm__hint {
  display: block;
  font-size: var(--t-xs);
  color: var(--c-fg-3);
  margin-bottom: var(--gap-1);
}
.term-confirm__hint code {
  color: var(--c-danger);
}
.term-confirm__input {
  width: 100%;
  background: var(--c-surface-2);
  border: var(--border-w) solid var(--c-border-strong);
  border-radius: var(--r-sharp);
  color: var(--c-fg-1);
  font-family: inherit;
  font-size: var(--t-base);
  padding: var(--gap-2) var(--gap-3);
}
.term-confirm__input:focus-visible {
  outline: 2px solid var(--c-focus-ring);
  outline-offset: 2px;
  border-color: var(--c-border-accent);
}

.term-toasts {
  position: fixed;
  right: var(--gap-4);
  bottom: var(--gap-4);
  z-index: 70; /* above TermModal (60) so toasts from within a modal flow show */
  display: flex;
  flex-direction: column;
  gap: var(--gap-2);
  pointer-events: none;
}
.term-toast {
  pointer-events: auto;
  max-width: 360px;
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  border-left-width: 3px;
  border-radius: var(--r-sharp);
  color: var(--c-fg-1);
  font-size: var(--t-sm);
  line-height: var(--lh-base);
  padding: var(--gap-2) var(--gap-3);
  white-space: pre-wrap;
  box-shadow: 0 8px 24px -8px var(--c-overlay);
}
.term-toast--error { border-left-color: var(--c-danger); }
.term-toast--success { border-left-color: var(--c-ok); }
.term-toast--warn { border-left-color: var(--c-warn); }
.term-toast--info { border-left-color: var(--c-accent); }
</style>
