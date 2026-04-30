<!--
  Backwards-compatible shim — existing views import this module by path.
  All behavior delegates to TermConfirm so we don't have to touch import
  sites that aren't otherwise being rewritten.
-->
<template>
  <TermConfirm
    :visible="visible"
    :title="title"
    :message="message"
    :confirm-text="confirmText"
    :danger="danger"
    @confirm="$emit('confirm')"
    @cancel="$emit('cancel')"
  />
</template>

<script setup>
import TermConfirm from '../cli/TermConfirm.vue'

defineProps({
  visible: { type: Boolean, default: false },
  title: { type: String, default: 'Confirm' },
  message: { type: String, default: '' },
  confirmText: { type: String, default: 'Confirm' },
  danger: { type: Boolean, default: false },
})
defineEmits(['confirm', 'cancel'])
</script>
