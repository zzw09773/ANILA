<template>
  <div class="workbench">
    <header class="workbench__hero">
      <h1 class="workbench__title">院內 AI 工作臺</h1>
      <p class="workbench__lead">
        從這裡進入日常工作。系統管理是進階管理畫面，不是一般使用者的入口。
      </p>
    </header>

    <ul class="workbench__grid">
      <li v-for="entry in entries" :key="entry.id">
        <a class="workbench__card" :href="entry.href">
          <h2>{{ entry.label }}</h2>
          <p>{{ entry.description }}</p>
          <span class="workbench__go">進入</span>
        </a>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { workbenchEntries } from '../utils/postLoginDestination'

const entries = workbenchEntries()
</script>

<style scoped>
.workbench {
  max-width: 920px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--gap-8);
  padding: var(--gap-4) 0 var(--gap-10);
}
.workbench__title {
  margin: 0;
  font-size: var(--t-3xl);
  font-weight: 650;
  letter-spacing: var(--tracking-tight);
  color: var(--c-fg-1);
}
.workbench__lead {
  margin: var(--gap-3) 0 0;
  max-width: 36rem;
  color: var(--c-fg-2);
  font-size: var(--t-md);
  line-height: var(--lh-loose);
}
.workbench__grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--gap-4);
}
@media (max-width: 720px) {
  .workbench__grid { grid-template-columns: 1fr; }
}
.workbench__card {
  display: flex;
  flex-direction: column;
  gap: var(--gap-2);
  min-height: 148px;
  padding: var(--gap-5);
  background: var(--c-surface-1);
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-lg);
  text-decoration: none;
  color: inherit;
  box-shadow: 0 1px 2px rgba(17, 24, 39, 0.04);
  transition: border-color var(--motion), box-shadow var(--motion);
}
.workbench__card:hover {
  border-color: var(--c-accent);
  box-shadow: 0 8px 24px -16px rgba(43, 76, 126, 0.45);
  text-decoration: none;
}
.workbench__card h2 {
  margin: 0;
  font-size: var(--t-lg);
  font-weight: 600;
}
.workbench__card p {
  margin: 0;
  flex: 1;
  color: var(--c-fg-2);
  font-size: var(--t-sm);
  line-height: var(--lh-base);
}
.workbench__go {
  color: var(--c-accent);
  font-size: var(--t-sm);
  font-weight: 600;
}
</style>
