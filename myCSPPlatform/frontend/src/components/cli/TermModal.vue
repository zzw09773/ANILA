<!--
  Terminal-style modal. Replaces ad-hoc bg-black/50 + rounded-xl + shadow-xl
  patterns scattered across the views. Renders a centered fixed-position box
  with a real header bar, body slot, and footer slot. Auto-locks <body> scroll
  while open. Closes on Escape unless `dismissible="false"`.
-->
<template>
  <Teleport to="body">
    <transition name="term-modal">
      <div v-if="visible" class="term-modal" @keydown.esc="onEscape" tabindex="-1" ref="root">
        <div class="term-modal__overlay" @click="onOverlay" />
        <div class="term-modal__dialog" :style="dialogStyle" role="dialog" aria-modal="true">
          <header class="term-modal__head">
            <span class="term-modal__corner">┌</span>
            <span class="term-modal__title">{{ title }}</span>
            <span class="term-modal__rule" />
            <button v-if="dismissible" class="term-modal__close" @click="$emit('close')" aria-label="close">×</button>
          </header>
          <div class="term-modal__body" :class="{ 'term-modal__body--flush': flush }">
            <slot />
          </div>
          <footer v-if="$slots.footer" class="term-modal__foot">
            <slot name="footer" />
          </footer>
        </div>
      </div>
    </transition>
  </Teleport>
</template>

<script setup>
import { computed, watch, ref, onUnmounted } from 'vue'

const props = defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: '' },
  width: { type: String, default: '520px' },
  dismissible: { type: Boolean, default: true },
  flush: { type: Boolean, default: false },
})
const emit = defineEmits(['close'])

const root = ref(null)
const dialogStyle = computed(() => ({ maxWidth: props.width }))

function onOverlay() { if (props.dismissible) emit('close') }
function onEscape() { if (props.dismissible) emit('close') }

watch(() => props.visible, (open) => {
  if (typeof document === 'undefined') return
  document.body.style.overflow = open ? 'hidden' : ''
}, { immediate: true })

onUnmounted(() => {
  if (typeof document !== 'undefined') document.body.style.overflow = ''
})
</script>

<style scoped>
.term-modal {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--gap-6);
}
.term-modal__overlay {
  position: absolute;
  inset: 0;
  background: var(--c-overlay);
  backdrop-filter: blur(2px);
}
.term-modal__dialog {
  position: relative;
  width: 100%;
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border-strong);
  border-radius: var(--r-sharp);
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - var(--gap-12));
}

.term-modal__head {
  display: flex;
  align-items: center;
  gap: var(--gap-2);
  padding: var(--gap-2) var(--gap-3);
  background: var(--c-surface-2);
  border-bottom: var(--border-w) solid var(--c-border);
  font-size: var(--t-xs);
  letter-spacing: var(--tracking-caps);
  text-transform: uppercase;
  color: var(--c-fg-1);
}
.term-modal__corner {
  color: var(--c-fg-3);
  font-size: var(--t-base);
  line-height: 1;
}
.term-modal__title {
  font-weight: 600;
}
.term-modal__rule {
  flex: 1;
  height: 1px;
  background: var(--c-border);
  margin: 0 var(--gap-2);
}
.term-modal__close {
  background: transparent;
  border: 0;
  color: var(--c-fg-3);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  padding: 0 6px;
  height: 22px;
}
.term-modal__close:hover { color: var(--c-fg-1); }

.term-modal__body {
  padding: var(--gap-4);
  overflow: auto;
  flex: 1;
}
.term-modal__body--flush { padding: 0; }

.term-modal__foot {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: var(--gap-2);
  padding: var(--gap-3) var(--gap-4);
  border-top: var(--border-w) solid var(--c-border);
  background: var(--c-surface-2);
}

.term-modal-enter-active,
.term-modal-leave-active {
  transition: opacity var(--motion) var(--easing);
}
.term-modal-enter-active .term-modal__dialog,
.term-modal-leave-active .term-modal__dialog {
  transition: transform var(--motion) var(--easing);
}
.term-modal-enter-from,
.term-modal-leave-to {
  opacity: 0;
}
.term-modal-enter-from .term-modal__dialog,
.term-modal-leave-to .term-modal__dialog {
  transform: translateY(-6px);
}
</style>
