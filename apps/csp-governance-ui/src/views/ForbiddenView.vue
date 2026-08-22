<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">這頁你看不到</h1>
        <p class="page-head__sub">請回工作臺，或請管理員開權限。</p>
      </div>
    </header>

    <div class="card">
      <p class="card__lead">
        這頁你看不到。請回工作臺，或請管理員開權限。
      </p>
      <p v-if="fromPath" class="card__meta">嘗試開啟：{{ fromPath }}</p>
      <div class="card__actions">
        <a class="term-btn term-btn--primary" :href="workbenchHref">回工作臺</a>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { shellWorkbenchHref } from '../utils/appOrigins'

const route = useRoute()
const workbenchHref = shellWorkbenchHref()

const fromPath = computed(() => {
  const value = route.query.from
  return typeof value === 'string' ? value : ''
})
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--gap-4);
  padding-bottom: var(--gap-8);
}
.page-head__title {
  margin: 4px 0 2px;
  font-size: var(--t-2xl);
  font-weight: 600;
}
.page-head__sub {
  margin: 0;
  color: var(--c-fg-3);
  font-size: var(--t-sm);
}
.card {
  max-width: 36rem;
  padding: var(--gap-5);
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-md);
}
.card__lead {
  margin: 0 0 var(--gap-3);
  color: var(--c-fg-1);
  line-height: var(--lh-loose);
}
.card__meta {
  margin: 0 0 var(--gap-4);
  color: var(--c-fg-3);
  font-size: var(--t-xs);
}
.card__actions {
  display: flex;
  gap: var(--gap-2);
  flex-wrap: wrap;
}
</style>
