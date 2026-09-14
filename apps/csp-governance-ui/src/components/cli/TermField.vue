<!--
  Form field wrapper. Takes a `label`, optional `hint` line below the input,
  and an `error` slot/prop for validation feedback. Body is the field control
  itself (passed via default slot — usually <input class="term-input">).
-->
<template>
  <label class="term-field" :class="{ 'term-field--invalid': !!error }">
    <span v-if="label" class="term-field__label">
      <span>{{ label }}</span>
      <span v-if="optional" class="term-field__optional">選填</span>
    </span>
    <span class="term-field__control">
      <slot />
    </span>
    <span v-if="error" class="term-field__error">! {{ error }}</span>
    <span v-else-if="hint" class="term-field__hint">{{ hint }}</span>
  </label>
</template>

<script setup>
defineProps({
  label: { type: String, default: '' },
  hint: { type: String, default: '' },
  error: { type: String, default: '' },
  optional: { type: Boolean, default: false },
})
</script>

<style scoped>
.term-field {
  display: flex;
  flex-direction: column;
  gap: var(--gap-1);
}
.term-field__label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: var(--t-2xs);
  letter-spacing: 0;
  color: var(--c-fg-3);
}
.term-field__optional {
  text-transform: none;
  letter-spacing: 0.05em;
  font-size: var(--t-2xs);
  color: var(--c-fg-mute);
}
.term-field__hint {
  font-size: var(--t-2xs);
  color: var(--c-fg-3);
  letter-spacing: 0.04em;
  margin-top: 2px;
}
.term-field__error {
  font-size: var(--t-2xs);
  color: var(--c-danger);
  letter-spacing: 0.04em;
  margin-top: 2px;
}
.term-field--invalid :deep(.term-input),
.term-field--invalid :deep(.term-select),
.term-field--invalid :deep(.term-textarea) {
  border-color: var(--c-danger) !important;
}
</style>
