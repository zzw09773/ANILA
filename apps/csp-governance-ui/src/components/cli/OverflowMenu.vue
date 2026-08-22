<template>
  <div class="overflow" ref="root">
    <button
      type="button"
      class="overflow__trigger"
      :aria-expanded="open ? 'true' : 'false'"
      aria-haspopup="menu"
      :aria-label="label"
      @click="toggle"
    >
      ⋯
    </button>
    <ul
      v-if="open"
      class="overflow__menu"
      :class="{ 'is-up': dropUp }"
      role="menu"
      @click="open = false"
    >
      <slot />
    </ul>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineProps({
  label: { type: String, default: '更多操作' },
})

const open = ref(false)
const dropUp = ref(false)
const root = ref(null)

function toggle() {
  const next = !open.value
  if (next && root.value) {
    const rect = root.value.getBoundingClientRect()
    dropUp.value = rect.bottom + 240 > window.innerHeight
  }
  open.value = next
}

function onDocClick(event) {
  if (!root.value) return
  if (!root.value.contains(event.target)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

defineExpose({ close: () => { open.value = false } })
</script>

<style scoped>
.overflow {
  position: relative;
  display: inline-flex;
}
.overflow__trigger {
  height: 28px;
  padding: 0 10px;
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-soft);
  background: var(--c-surface-1);
  color: var(--c-fg-2);
  font: inherit;
  font-size: var(--t-xs);
  cursor: pointer;
  white-space: nowrap;
}
.overflow__trigger:hover {
  color: var(--c-accent);
  border-color: var(--c-accent);
}
.overflow__menu {
  position: absolute;
  right: 0;
  top: calc(100% + 4px);
  z-index: 20;
  min-width: 168px;
  margin: 0;
  padding: 6px;
  list-style: none;
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-md);
  box-shadow: 0 10px 28px -18px rgba(17, 24, 39, 0.45);
}
.overflow__menu.is-up {
  top: auto;
  bottom: calc(100% + 4px);
}
.overflow__menu :deep(a),
.overflow__menu :deep(button) {
  display: block;
  width: 100%;
  text-align: left;
  background: transparent;
  border: 0;
  padding: 7px 8px;
  border-radius: var(--r-soft);
  color: var(--c-fg-1);
  font: inherit;
  font-size: var(--t-sm);
  cursor: pointer;
  text-decoration: none;
}
.overflow__menu :deep(a:hover),
.overflow__menu :deep(button:hover) {
  background: var(--c-accent-soft);
  color: var(--c-accent-strong);
}
.overflow__menu :deep(button.is-danger) {
  color: var(--c-danger);
}
.overflow__menu :deep(button:disabled) {
  color: var(--c-fg-mute);
  cursor: not-allowed;
}
</style>
