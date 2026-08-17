<template>
  <div class="page">
    <header class="page-head">
      <div>
        <span class="term-label">頁面</span>
        <h1 class="page-head__title">頁面不存在</h1>
      </div>
    </header>

    <TermBox title="找不到頁面">
      <p class="not-found__message">你要前往的頁面不存在。</p>
      <router-link to="/" class="term-btn term-btn--primary">返回儀表板</router-link>
    </TermBox>
  </div>
</template>

<script setup>
import { TermBox } from '../components/cli'
</script>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--gap-4);
  padding-bottom: var(--gap-8);
}

.page-head {
  display: flex;
  align-items: flex-end;
}

.page-head__title {
  margin: 4px 0 2px;
  font-size: var(--t-2xl);
  font-weight: 600;
  letter-spacing: var(--tracking-tight);
}

.not-found__message {
  margin: 0 0 var(--gap-4);
  color: var(--c-fg-2);
}
</style>
