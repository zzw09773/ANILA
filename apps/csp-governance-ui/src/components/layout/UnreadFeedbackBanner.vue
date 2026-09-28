<template>
  <router-link
    v-if="model"
    class="unread-feedback-banner"
    role="status"
    data-testid="unread-feedback-banner"
    to="/feedback"
  >{{ model.text }}</router-link>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import {
  subscribeUnreadFeedback,
  unreadFeedbackBannerModel,
} from '../../utils/unreadFeedbackBanner'

const model = ref(null)
let unsubscribe = () => {}

onMounted(() => {
  unsubscribe = subscribeUnreadFeedback((summary) => {
    model.value = unreadFeedbackBannerModel(summary)
  })
})

onUnmounted(() => {
  unsubscribe()
})
</script>

<style scoped>
.unread-feedback-banner {
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

.unread-feedback-banner:focus-visible {
  outline: 2px solid var(--c-info);
  outline-offset: 2px;
}
</style>
