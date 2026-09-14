<template>
  <div class="user-search">
    <input
      v-model="q"
      class="term-input"
      :placeholder="placeholder"
      :disabled="disabled"
      autocomplete="off"
      @focus="open = true"
      @input="onInput"
    />
    <ul v-if="open && results.length" class="user-search__list" role="listbox">
      <li v-for="u in results" :key="u.id">
        <button type="button" class="user-search__item" @mousedown.prevent="pick(u)">
          <span>{{ u.username }}</span>
          <span v-if="u.department" class="cell-meta">{{ u.department }}</span>
        </button>
      </li>
    </ul>
    <p v-else-if="open && q && !loading && !results.length" class="cell-meta">找不到符合的人</p>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { searchDirectory } from '../../api/directory'

const props = defineProps({
  placeholder: { type: String, default: '搜尋帳號' },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['select'])

const q = ref('')
const results = ref([])
const open = ref(false)
const loading = ref(false)
let timer = 0

function onInput() {
  open.value = true
  clearTimeout(timer)
  timer = window.setTimeout(search, 200)
}

async function search() {
  loading.value = true
  try {
    const { data } = await searchDirectory(q.value, { includeSelf: true })
    results.value = data || []
  } catch {
    results.value = []
  } finally {
    loading.value = false
  }
}

function pick(user) {
  emit('select', user)
  q.value = ''
  results.value = []
  open.value = false
}
</script>

<style scoped>
.user-search { position: relative; min-width: 12rem; }
.user-search__list {
  position: absolute;
  z-index: 40;
  left: 0;
  right: 0;
  margin: 4px 0 0;
  padding: 4px;
  list-style: none;
  background: var(--c-elev, var(--c-surface-1));
  border: var(--border-w) solid var(--c-border-strong);
  border-radius: var(--r-md);
  max-height: 12rem;
  overflow: auto;
}
.user-search__item {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  text-align: start;
  background: transparent;
  border: 0;
  color: inherit;
  padding: 6px 8px;
  cursor: pointer;
}
.user-search__item:hover { background: var(--c-row-hover); }
</style>
