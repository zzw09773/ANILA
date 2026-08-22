<template>
  <div class="shell">
    <AppHeader />
    <div class="shell__body">
      <AppSidebar />
      <main class="shell__main">
        <router-view v-slot="{ Component, route: viewRoute }">
          <component :is="Component" :key="viewRoute.fullPath" />
        </router-view>
      </main>
    </div>
    <AppStatusBar />
  </div>
</template>

<script setup>
import AppHeader from './AppHeader.vue'
import AppSidebar from './AppSidebar.vue'
import AppStatusBar from './AppStatusBar.vue'
</script>

<style scoped>
.shell {
  display: grid;
  grid-template-rows: var(--shell-topbar-h) 1fr var(--shell-statusbar-h);
  height: 100dvh;
  background: var(--c-bg);
  color: var(--c-fg-1);
}

.shell__body {
  display: grid;
  grid-template-columns: var(--shell-sidebar) 1fr;
  min-height: 0;
  border-top: var(--border-w) solid var(--c-border);
}

.shell__main {
  min-width: 0;
  overflow: auto;
  padding: var(--gap-5) var(--gap-6);
  background: var(--c-bg);
}

@media (max-width: 900px) {
  .shell__body {
    grid-template-columns: 1fr;
  }
  .shell__main {
    padding: var(--gap-4);
  }
}

</style>
