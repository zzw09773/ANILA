<template>
  <div class="shell">
    <AppHeader />
    <div class="shell__body" :style="bodyStyle">
      <div
        v-if="narrow && open"
        class="shell__backdrop"
        @click="close()"
      />
      <AppSidebar />
      <div
        v-if="!narrow"
        ref="resizerEl"
        class="shell__resizer"
        role="separator"
        aria-orientation="vertical"
        aria-label="調整側欄寬度"
        :aria-valuemin="bounds.min"
        :aria-valuemax="bounds.max"
        :aria-valuenow="sidebarWidth"
        tabindex="0"
        @keydown="onResizerKey"
        @dblclick="resetSidebarWidth"
      />
      <main id="gov-main" class="shell__main" tabindex="-1">
        <OpenAlertBanner v-if="isSteward" />
        <UnreadFeedbackBanner v-if="isSteward" />
        <InactivityNoticeBanner v-if="isSteward" />
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
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppHeader from './AppHeader.vue'
import AppSidebar from './AppSidebar.vue'
import AppStatusBar from './AppStatusBar.vue'
import OpenAlertBanner from './OpenAlertBanner.vue'
import UnreadFeedbackBanner from './UnreadFeedbackBanner.vue'
import InactivityNoticeBanner from './InactivityNoticeBanner.vue'
import { ErrorPanel } from '../errorPanel.js'
import { provideShellNav } from '../../composables/useShellNav.js'
import {
  SIDEBAR_DEFAULT,
  bindSidebarPointer,
  clampSidebarWidth,
  shouldEndSidebarDrag,
  sidebarWidthBounds,
  sidebarWidthFromKey,
} from '../../composables/sidebarWidth.js'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()
const isSteward = computed(() => auth.isSteward)
const { open, narrow, close } = provideShellNav()
const route = useRoute()
watch(() => route.fullPath, () => close())

const viewportWidth = () => (typeof window === 'undefined' ? 1280 : window.innerWidth)
const viewport = ref(viewportWidth())
const sidebarWidth = ref(clampSidebarWidth(SIDEBAR_DEFAULT, viewport.value))
const preferredWidth = ref(SIDEBAR_DEFAULT)
const bounds = computed(() => sidebarWidthBounds(viewport.value))
const bodyStyle = computed(() => (
  narrow.value ? null : { '--shell-sidebar': `${sidebarWidth.value}px` }
))
const resizerEl = ref(null)
let unbindPointer = () => {}

function applyViewport() {
  const next = viewportWidth()
  if (shouldEndSidebarDrag(next)) unbindPointer.release?.()
  viewport.value = next
  sidebarWidth.value = clampSidebarWidth(preferredWidth.value, viewport.value)
}
function resetSidebarWidth() {
  preferredWidth.value = SIDEBAR_DEFAULT
  sidebarWidth.value = clampSidebarWidth(SIDEBAR_DEFAULT, viewportWidth())
}
function onResizerKey(event) {
  const next = sidebarWidthFromKey(event.key, sidebarWidth.value, viewportWidth())
  if (next == null) return
  event.preventDefault()
  preferredWidth.value = next
  sidebarWidth.value = next
}

onMounted(() => {
  applyViewport()
  window.addEventListener('resize', applyViewport)
})
watch(resizerEl, (el, prev) => {
  if (prev) unbindPointer()
  unbindPointer = () => {}
  if (!el) return
  unbindPointer = bindSidebarPointer(el, {
    getWidth: () => sidebarWidth.value,
    setWidth: (next) => {
      preferredWidth.value = next
      sidebarWidth.value = next
    },
    viewportWidth,
  })
})
watch(narrow, (isNarrow) => {
  if (isNarrow) unbindPointer.release?.()
  else applyViewport()
}, { flush: 'sync' })
onUnmounted(() => {
  window.removeEventListener('resize', applyViewport)
  unbindPointer()
})
</script>

<style scoped>
.shell {
  position: relative;
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

.shell__resizer {
  position: absolute;
  top: 0;
  bottom: 0;
  left: calc(var(--shell-sidebar) - 12px);
  z-index: 5;
  box-sizing: content-box;
  width: 6px;
  padding: 0 9px;
  background-clip: content-box;
  background-color: transparent;
  cursor: col-resize;
  touch-action: none;
}
.shell__resizer:hover,
.shell__resizer:focus-visible {
  background-color: var(--c-accent);
}
@media (forced-colors: active) {
  .shell__resizer:hover,
  .shell__resizer:focus-visible {
    background-color: Highlight;
    outline: 2px solid Highlight;
  }
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
