<!--
  Drop-in replacement for ConfirmDialog. Same prop surface so existing views
  can swap the import and keep working.
-->
<template>
  <TermModal :visible="visible" :title="title" :width="'440px'" @close="$emit('cancel')">
    <p class="term-confirm__msg">{{ message }}</p>
    <template #footer>
      <TermButton variant="ghost" @click="$emit('cancel')" label="Cancel" />
      <TermButton :variant="danger ? 'danger' : 'primary'" @click="$emit('confirm')" :label="confirmText" />
    </template>
  </TermModal>
</template>

<script setup>
import TermModal from './TermModal.vue'
import TermButton from './TermButton.vue'

defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: 'Confirm' },
  message: { type: String, default: '' },
  confirmText: { type: String, default: 'Confirm' },
  danger: { type: Boolean, default: false },
})
defineEmits(['confirm', 'cancel'])
</script>

<style scoped>
.term-confirm__msg {
  color: var(--c-fg-2);
  font-size: var(--t-base);
  white-space: pre-wrap;
  line-height: var(--lh-base);
}
</style>
