<template>
  <div class="row-actions">
    <slot />
    <div v-if="$slots.more" class="row-more">
      <button
        type="button"
        class="row-more__summary"
        :aria-expanded="open"
        aria-haspopup="menu"
        @click.stop="toggle"
      >更多</button>
      <Teleport to="body">
        <div
          v-if="open"
          class="row-more__menu row-more__menu--portal"
          :style="menuStyle"
          role="menu"
          @click.stop
        >
          <slot name="more" />
        </div>
      </Teleport>
    </div>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

const CLOSE = 'anila-row-more-close'
const open = ref(false)
const menuStyle = ref({})

function place(el) {
  const r = el.getBoundingClientRect()
  menuStyle.value = {
    position: 'fixed',
    top: `${Math.round(r.bottom + 4)}px`,
    right: `${Math.round(window.innerWidth - r.right)}px`,
    zIndex: 80,
  }
}

function close() {
  open.value = false
}

function toggle(e) {
  if (open.value) {
    close()
    return
  }
  window.dispatchEvent(new Event(CLOSE))
  open.value = true
  place(e.currentTarget)
}

function onKey(e) {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  window.addEventListener(CLOSE, close)
  document.addEventListener('click', close)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  window.removeEventListener(CLOSE, close)
  document.removeEventListener('click', close)
  document.removeEventListener('keydown', onKey)
})
</script>
