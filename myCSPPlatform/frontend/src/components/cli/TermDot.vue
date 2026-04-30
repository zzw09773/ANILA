<template>
  <span
    class="term-dot"
    :class="`term-dot--${status}`"
    :title="title"
    :aria-label="title"
  />
</template>

<script setup>
defineProps({
  status: { type: String, default: 'idle' }, // ok | warn | danger | info | idle
  title: { type: String, default: '' },
})
</script>
