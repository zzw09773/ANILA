<!--
  Thin shim around TermStat — kept so the existing DashboardView continues
  to compile if anyone re-imports it elsewhere. New views should import
  TermStat directly from `components/cli`.
-->
<template>
  <TermStat :label="label" :value="value" :hint="hint" :tone="tone" />
</template>

<script setup>
import TermStat from '../cli/TermStat.vue'

defineProps({
  label: { type: String, required: true },
  value: { type: Number, default: 0 },
  hint: { type: String, default: '' },
  tone: { type: String, default: 'default' },
})
</script>
