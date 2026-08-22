<template>
  <div class="anila-topbar__account" ref="root">
    <button
      type="button"
      class="anila-topbar__who"
      :aria-expanded="open"
      aria-haspopup="menu"
      @click="open = !open"
    >
      {{ authStore.user?.username || '未登入' }}
    </button>
    <div v-if="open" class="anila-topbar__menu" role="menu">
      <span class="anila-topbar__ver">版本 {{ version }}</span>
      <button type="button" role="menuitem" @click="onChangePassword">變更密碼</button>
      <button type="button" role="menuitem" @click="onLogout">登出</button>
    </div>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useAuthStore } from '../../stores/auth'
import { ANILA_VERSION } from '../../../../shared/product.js'

const emit = defineEmits(['change-password', 'logout'])
const authStore = useAuthStore()
const version = ANILA_VERSION
const open = ref(false)
const root = ref(null)

function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}

function onChangePassword() {
  open.value = false
  emit('change-password')
}

function onLogout() {
  open.value = false
  emit('logout')
}

onMounted(() => document.addEventListener('click', onDocClick))
onUnmounted(() => document.removeEventListener('click', onDocClick))
</script>
