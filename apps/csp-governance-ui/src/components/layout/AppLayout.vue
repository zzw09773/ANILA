<template>
  <div class="shell">
    <AppHeader />
    <div class="shell__body">
      <div
        v-if="narrow && open"
        class="shell__backdrop"
        @click="close()"
      />
      <AppSidebar />
      <main class="shell__main">
        <router-view v-slot="{ Component }">
          <transition name="shell-page" mode="out-in">
            <!-- 面板層錯誤網子：view 在 render/setup 期炸了，這裡出現可讀錯誤區塊，不是空白。 -->
            <ErrorPanel :key="$route.fullPath">
              <component :is="Component" />
            </ErrorPanel>
          </transition>
        </router-view>
      </main>
    </div>
    <AppStatusBar />
  </div>
</template>

<script setup>
import { watch } from 'vue'
import { useRoute } from 'vue-router'
import AppHeader from './AppHeader.vue'
import AppSidebar from './AppSidebar.vue'
import AppStatusBar from './AppStatusBar.vue'
import { ErrorPanel } from '../errorPanel.js'
import { provideShellNav } from '../../composables/useShellNav.js'

const { open, narrow, close } = provideShellNav()
const route = useRoute()
watch(() => route.fullPath, () => close())
</script>

<style scoped>
.shell {
  display: grid;
  grid-template-rows: var(--shell-topbar-h) minmax(0, 1fr) var(--shell-statusbar-h);
  height: 100%;
  min-height: 0;
  overflow: hidden;
  background: var(--c-bg);
  color: var(--c-fg-1);
}

.shell__body {
  display: grid;
  grid-template-columns: var(--shell-sidebar) 1fr;
  grid-template-rows: minmax(0, 1fr);
  min-height: 0;
  min-width: 0;
  overflow: hidden;
  position: relative;
  border-top: var(--border-w) solid var(--c-border);
}

.shell__main {
  min-width: 0;
  min-height: 0;
  overflow: auto;
  padding: var(--gap-5) var(--gap-6);
  background: var(--c-bg);
}

.shell__backdrop {
  display: none;
}

@media (max-width: 900px) {
  /* 側欄改抽屜、脫離文件流，主區獨佔整欄。不可再把側欄堆成上一列——
     sidenav height:100% 會把 main 壓成一條縫。 */
  .shell__body {
    grid-template-columns: 1fr;
  }
  .shell__main {
    padding: var(--gap-4);
  }
  .shell__backdrop {
    display: block;
    position: absolute;
    inset: 0;
    z-index: 20;
    background: var(--c-overlay);
  }
  :deep(.sidenav) {
    position: absolute;
    z-index: 30;
    top: 0;
    bottom: 0;
    left: 0;
    width: min(var(--shell-sidebar), 86vw);
    height: auto;
    transform: translateX(-105%);
    transition: transform var(--motion) var(--easing);
    box-shadow: 8px 0 24px rgba(17, 24, 39, 0.18);
    pointer-events: none;
  }
  :deep(.sidenav.is-open) {
    transform: translateX(0);
    pointer-events: auto;
  }
}

.shell-page-enter-active,
.shell-page-leave-active {
  transition: opacity var(--motion) var(--easing);
}
.shell-page-enter-from,
.shell-page-leave-to {
  opacity: 0;
}
</style>
