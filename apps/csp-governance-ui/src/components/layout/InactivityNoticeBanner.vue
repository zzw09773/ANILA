<template>
  <router-link
    v-if="model"
    class="inactivity-notice-banner"
    role="status"
    data-testid="inactivity-notice-banner"
    to="/users"
  >{{ model.text }}</router-link>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import {
  inactivityNoticeModel,
  subscribeInactivityNotice,
} from '../../utils/inactivityNotice'

const model = ref(null)
let unsubscribe = () => {}

onMounted(() => {
  unsubscribe = subscribeInactivityNotice((summary) => {
    model.value = inactivityNoticeModel(summary)
  })
})

onUnmounted(() => {
  unsubscribe()
})
</script>

<style scoped>
.inactivity-notice-banner {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: var(--gap-4);
  padding: 12px 16px;
  background: var(--c-info-soft);
  color: var(--c-info);
  border: 2px solid var(--c-info);
  font-size: var(--t-sm);
  font-weight: 650;
  line-height: 1.45;
  text-decoration: underline;
}

.inactivity-notice-banner:focus-visible {
  outline: 2px solid var(--c-info);
  outline-offset: 2px;
}
</style>
