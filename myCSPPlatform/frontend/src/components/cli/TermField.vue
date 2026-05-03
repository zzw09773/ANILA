<!--
  Form field wrapper. Takes a `label`, optional `hint` line below the input,
  and an `error` slot/prop for validation feedback. Body is the field control
  itself (passed via default slot — usually <input class="term-input">).
-->
<template>
  <div class="term-field" :class="{ 'term-field--invalid': !!error }">
    <label v-if="label" class="term-field__label">
      <span>{{ label }}</span>
      <span v-if="optional" class="term-field__optional">optional</span>
    </label>
    <div class="term-field__control">
      <slot />
    </div>
    <p v-if="error" class="term-field__error">! {{ error }}</p>
    <p v-else-if="hint" class="term-field__hint">{{ hint }}</p>
  </div>
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
  text-transform: uppercase;
  letter-spacing: var(--tracking-caps);
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
