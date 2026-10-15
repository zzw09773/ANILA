<template>
  <fieldset v-if="schema.type === 'object' && depth < 5">
    <legend>{{ label }}</legend>
    <small>{{ schema.description }} 預設：{{ defaultLabel }}</small>
    <ToolParameter v-for="(child, key) in schema.properties || {}" :key="key" :label="`${child.title || key}${schema.required?.includes(key) ? '（必填）' : ''}`" :schema="child" :required="schema.required?.includes(key)" :depth="depth + 1" :model-value="modelValue?.[key]" @update:model-value="updateKey(key, $event)"/>
    <p v-if="!Object.keys(schema.properties || {}).length">此工具尚未定義可填寫的欄位，請聯絡服務維護人員。</p>
  </fieldset>
  <fieldset v-else-if="schema.type === 'array' && depth < 5">
    <legend>{{ label }}</legend>
    <small>{{ schema.description }} 預設：{{ defaultLabel }}</small>
    <div v-for="(item, index) in modelValue || []" :key="index">
      <ToolParameter :label="`第 ${index + 1} 項`" :schema="schema.items || {type:'string'}" :depth="depth + 1" :model-value="item" @update:model-value="updateIndex(index, $event)"/>
      <button @click="$emit('update:modelValue', modelValue.filter((_, i) => i !== index))">移除第 {{ index + 1 }} 項</button>
    </div>
    <button :disabled="(modelValue?.length || 0) >= Math.min(schema.maxItems || 30, 30)" @click="$emit('update:modelValue', [...(modelValue || []), defaultValue(schema.items || {type:'string'})])">
      新增一項
    </button>
  </fieldset>
  <div v-else-if="['object','array'].includes(schema.type)">
    <p>{{ label }}：結構超過可填寫層數，請聯絡工具維護人員簡化參數。</p>
  </div>
  <div v-else>
    <label v-if="!schema.enum && (!schema.type || schema.type === 'string')">
      {{ label }}
      的內容來源
      <select :value="modelValue === textMarker ? 'text' : 'literal'" @change="$emit('update:modelValue', $event.target.value === 'text' ? textMarker : defaultValue(schema))">
        <option value="literal">手動填寫</option>
        <option value="text">前一步文字</option>
      </select>
    </label>
    <label>
      {{ label }}
      <select v-if="schema.enum" :value="modelValue === undefined ? '' : schema.enum.indexOf(modelValue)" @change="selectValue($event)">
        <option value="">請選擇</option>
        <option v-for="(value, i) in schema.enum" :key="i" :value="i">{{ value }}</option>
      </select>
      <input v-else-if="schema.type === 'boolean'" :checked="modelValue === true" type="checkbox" @change="$emit('update:modelValue', $event.target.checked)">
      <input v-else-if="['number','integer'].includes(schema.type)" :value="modelValue" type="number" :min="schema.minimum" :max="schema.maximum" :step="schema.type === 'integer' ? 1 : 'any'" @input="$emit('update:modelValue', $event.target.value === '' ? undefined : Number($event.target.value))">
      <input v-else :value="modelValue === textMarker ? '使用前一步文字' : modelValue" :disabled="modelValue === textMarker" :maxlength="schema.maxLength || 8000" @input="$emit('update:modelValue', $event.target.value)">
      <small>{{ schema.description || '填入查詢條件；未提供的欄位由服務判斷。' }} 預設：{{ defaultLabel }}</small>
    </label>
  </div>
</template>
<script setup>
import { computed, watch } from 'vue';
const props = defineProps({
  schema: {
    type: Object,
    required: true
  },
  label: String,
  required: Boolean,
  modelValue: null,
  depth: {
    type: Number,
    default: 0
  }
});
const emit = defineEmits(['update:modelValue']);
const textMarker = '{{text}}';
watch(() => [props.schema, props.modelValue, props.required], () => {
  if (props.modelValue === undefined && (props.schema.default !== undefined || props.required && props.schema.type === 'boolean')) emit('update:modelValue', props.schema.default ?? false);
}, {immediate:true});
const defaultLabel = computed(() => props.schema.default === undefined ? '未填' : JSON.stringify(props.schema.default));
function defaultValue(schema) {
  return schema.default ?? (schema.type === 'object' ? {} : schema.type === 'array' ? [] : schema.type === 'boolean' ? false : schema.type === 'number' || schema.type === 'integer' ? undefined : '');
}
function updateKey(key, value) {
  const next = {
    ...(props.modelValue || {})
  };
  if (value === undefined) delete next[key];else next[key] = value;
  emit('update:modelValue', next);
}
function updateIndex(index, value) {
  const next = [...(props.modelValue || [])];
  next[index] = value;
  emit('update:modelValue', next);
}
function selectValue(event) {
  emit('update:modelValue', event.target.value === '' ? undefined : props.schema.enum[Number(event.target.value)]);
}
</script>
