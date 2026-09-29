<template>
  <div
    v-if="model"
    class="open-alert-banner"
    role="status"
    data-testid="open-alert-banner"
  >
    <p class="open-alert-banner__text">{{ model.text }}</p>
    <router-link class="open-alert-banner__link" to="/alerts">查看警報</router-link>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { getAlertSummary } from '../../api/alerts'
import { createPoller } from '../../utils/polling'
import {
  OPEN_ALERT_BANNER_POLL_MS,
  openAlertBannerModel,
  subscribeOpenAlertBanner,
} from '../../utils/openAlertBanner'
import { publishUnreadFeedback } from '../../utils/unreadFeedbackBanner'
import { publishInactivityNotice } from '../../utils/inactivityNotice'

const model = ref(null)
let requestGen = 0

async function load() {
  const gen = ++requestGen
  try {
    const { data } = await getAlertSummary()
    if (gen !== requestGen) return
    model.value = openAlertBannerModel(data)
    publishUnreadFeedback(data)
    publishInactivityNotice(data)
  } catch {
    // 這次讀不到就留著上次的橫幅，不要把整頁弄壞。
  }
}

const poller = createPoller(load, { intervalMs: OPEN_ALERT_BANNER_POLL_MS })
let unsubscribe = () => {}

onMounted(() => {
  load()
  poller.start()
  unsubscribe = subscribeOpenAlertBanner(load)
})

onUnmounted(() => {
  poller.stop()
  unsubscribe()
})
</script>

<style scoped>
.open-alert-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 16px;
  margin-bottom: var(--gap-4);
  padding: 12px 16px;
  background: #9d1c1c;
  color: #fff;
  border: 2px solid #6e1010;
  font-weight: 650;
}

.open-alert-banner__text {
  margin: 0;
  font-size: var(--t-sm);
  line-height: 1.45;
}

.open-alert-banner__link {
  color: #fff;
  font-weight: 700;
  text-decoration: underline;
  white-space: nowrap;
}

.open-alert-banner__link:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 2px;
}
</style>
