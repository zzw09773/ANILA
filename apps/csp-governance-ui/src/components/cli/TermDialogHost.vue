<!--
  Singleton host for the promise-based useDialog() confirm/toast. Mounted once
  in App.vue. Renders the active confirm dialog (TermModal + TermButton) and a
  bottom-right toast stack. Views never render this directly — they call
  confirm()/toast() from useDialog().
-->
<template>
  <template v-for="confirmation in [s.confirm]" :key="confirmation.id">
  <TermModal v-if="confirmation.open"
    :visible="confirmation.open"
    :title="confirmation.title"
    width="460px"
    elevated
    @close="settleFor(confirmation, false)"
  >
    <p class="term-confirm__msg">{{ confirmation.message }}</p>
    <div v-if="confirmation.requireText" class="term-confirm__verify">
      <label class="term-confirm__hint">
        請輸入「<code>{{ confirmation.requireText }}</code>」以確認：
      </label>
      <input
        class="term-confirm__input"
        :value="confirmation.input"
        @input="onInput(confirmation, $event)"
        @compositionstart="onCompositionStart(confirmation, $event)"
        @compositionend="onCompositionEnd(confirmation, $event)"
        autocomplete="off"
        spellcheck="false"
        @keydown.enter.prevent="onEnter(confirmation, $event)"
      />
    </div>
    <template #footer>
      <TermButton variant="ghost" :label="confirmation.cancelText" @click="settleFor(confirmation, false)" />
      <TermButton
        :variant="confirmation.danger ? 'danger' : 'primary'"
        :disabled="!canConfirm(confirmation)"
        :label="confirmation.confirmText"
        @click="settleFor(confirmation, true)"
      />
    </template>
  </TermModal>
  </template>

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
import TermModal from './TermModal.vue'
import TermButton from './TermButton.vue'
import { _dialogState as s, _settleConfirm as settle } from '../../composables/useDialog'

const current = confirmation => s.confirm === confirmation && confirmation.open
const canConfirm = confirmation => !confirmation.requireText || confirmation.input === confirmation.requireText
function settleFor(confirmation, accepted) {
  if (!current(confirmation) || accepted && !canConfirm(confirmation)) return
  settle(accepted, confirmation)
}
function onEnter(confirmation, event) {
  if (event.isComposing || event.keyCode === 229 || event.target.composing) return
  settleFor(confirmation, true)
}
function onInput(confirmation, event) {
  if (current(confirmation) && !event.target.composing) confirmation.input = event.target.value
}
function onCompositionStart(confirmation, event) {
  if (current(confirmation)) event.target.composing = true
}
function onCompositionEnd(confirmation, event) {
  if (!current(confirmation)) return
  event.target.composing = false
  onInput(confirmation, event)
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
