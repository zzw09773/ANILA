<template>
  <div class="node-settings">
    <p>{{ module.description }}</p>
    <label>
      步驟名稱
      <input v-model="node.label" maxlength="100">
    </label>
    <template v-if="node.kind === 'input'">
      <label>
        問題提示
        <input v-model="node.config.placeholder">
        <small>預設：請輸入問題。提醒同仁可以問什麼。</small>
      </label>
    </template>
    <template v-if="node.kind === 'output'">
      <label class="check">
        <input v-model="node.config.include_sources" type="checkbox">
        附上來源依據（預設開啟）
      </label>
    </template>
    <template v-if="node.kind === 'model'">
      <label>
        回答變化程度
        <input v-model.number="node.config.temperature" type="number" min="0" max="2" step="0.1">
        <small>預設 0.2；越低越穩定。使用助手設定的同一個平台模型。</small>
      </label>
    </template>
    <template v-if="node.kind === 'prompt'">
      <p>角色：助手（固定）；平台安全規則由系統套用。</p>
      <label>
        工作指示
        <textarea v-model="node.config.instruction" rows="6" maxlength="8000"/>
        <small>預設：以台灣繁體中文回答，資料不足時明說。</small>
      </label>
    </template>
    <template v-if="node.kind === 'rag'">
      <label>
        文件取得方式
        <select v-model="ragMode"><option v-for="mode in RAG_MODES" :key="mode.id" :value="mode.id">{{ mode.name }}</option></select>
        <small v-if="ragMode === 'hybrid'">預設同時使用語意與關鍵字搜尋，適合大型文件，也能找編號、條號與專有名詞。</small>
        <small v-else>小量文件會完整提供內容。最多 20 份文件、合計 10 萬字；內容超出可用長度時自動改用語意與關鍵字搜尋，只提供相關片段。</small>
      </label>
      <label>
        最多取回幾段
        <input v-model.number="node.config.top_k" type="number" min="1" max="20" :disabled="ragMode === 'fulltext'">
        <small>預設 5 段。混合搜尋或全文超限改用混合搜尋時使用；全文模式本身不以這個數字截斷文件。</small>
      </label>
      <fieldset>
        <legend>搜尋文件集（預設不選）</legend>
        <label v-for="item in catalog.collections" :key="item.id" class="check">
          <input v-model="node.config.collection_ids" type="checkbox" :value="item.id">
          {{ item.name }}
        </label>
        <p v-if="!catalog.collections?.length">沒有可使用的文件集，請先到知識庫建立。</p>
      </fieldset>
    </template>
    <template v-if="node.kind === 'mcp'">
      <label>
        查詢工具
        <select v-model="node.config.tool_id" @change="node.config.arguments = {}; initializeArguments()">
          <option :value="null">請選擇已核准工具</option>
          <option v-for="tool in catalog.mcp_tools" :key="tool.id" :value="tool.id">{{ tool.name }}</option>
        </select>
        <small>預設未選。只列出有權限的核准唯讀工具。</small>
      </label>
      <p>{{ selectedTool?.description }}</p>
      <ToolParameter v-for="(schema, key) in toolProperties" :key="key" :label="`${schema.title || key}${selectedTool?.input_schema?.required?.includes(key) ? '（必填）' : ''}`" :schema="schema" :required="selectedTool?.input_schema?.required?.includes(key)" :model-value="node.config.arguments[key]" @update:model-value="setArgument(key, $event)"/>
    </template>
    <template v-if="node.kind === 'condition'">
      <label>
        比較欄位
        <select v-model="node.config.field">
          <option value="text">前一步文字</option>
          <option v-for="field in fields" :key="field" :value="field">擷取：{{ field }}</option>
        </select>
        <small>預設：前一步文字；只可選文字或已定義的擷取欄位。</small>
      </label>
      <label>
        比較方式
        <select v-model="node.config.operator">
          <option v-for="(label, value) in operators" :key="value" :value="value">{{ label }}</option>
        </select>
        <small>預設：包含文字。只比較資料，不執行運算式。</small>
      </label>
      <template v-if="node.config.operator !== 'is_empty'">
        <label>比較值型別<select v-model="conditionType"><option value="string">文字</option><option value="number">數字</option><option value="boolean">是／否</option></select><small>預設文字。等於比較會同時檢查型別，例如數字 3 與文字「3」不同。</small></label>
        <label>比較內容
          <input v-if="conditionType === 'number'" :value="node.config.value" type="number" step="any" @input="node.config.value = $event.target.value === '' ? 0 : Number($event.target.value)">
          <select v-else-if="conditionType === 'boolean'" :value="String(node.config.value)" @change="node.config.value = $event.target.value === 'true'"><option value="true">是（true）</option><option value="false">否（false）</option></select>
          <input v-else v-model="node.config.value">
          <small>請填入要比對的固定內容，不執行運算式。</small>
        </label>
      </template>
    </template>
    <template v-if="node.kind === 'extract'">
      <label>
        要擷取的欄位
        <textarea :value="node.config.fields.join('\n')" rows="4" @input="node.config.fields = $event.target.value.split('\n').map(s => s.trim()).filter(Boolean)"/>
        <small>每行一個。預設：主旨、承辦人；缺少的欄位不猜測。</small>
      </label>
    </template>
    <template v-if="node.kind === 'format'">
      <label>
        整理方式
        <select v-model="node.config.style">
          <option value="paragraph">段落</option>
          <option value="bullets">條列</option>
          <option value="table">表格</option>
        </select>
        <small>預設：段落。保留原意再調整呈現方式。</small>
      </label>
    </template>
    <template v-if="node.kind === 'document'">
      <label>
        文件產出方式
        <select v-model="documentMode"><option value="text">純文字範本（RTF，預設）</option><option value="studio">既有 Studio 文件範本</option></select>
        <small>預設使用純文字 RTF。Studio 使用平台既有版型排版前一步內容，不額外產生內容。</small>
      </label>
      <label>
        文件標題
        <input v-model="documentTitle" maxlength="200" required>
        <small>預設：助手草稿。請填寫 200 字以內的純文字標題。</small>
        <small v-if="!documentTitle.trim()" role="alert">請填寫文件標題，再儲存版本。</small>
      </label>
      <template v-if="documentMode === 'studio'">
        <label>
          Studio 文件範本
          <select v-model="documentPreset"><option v-for="preset in documentPresets" :key="preset.id" :value="preset.id">{{ preset.name }}</option></select>
          <small>預設：重點摘要。只提供既有四種固定範本。</small>
        </label>
        <label>
          文件輸出格式
          <select v-model="documentFormat"><option v-for="format in STUDIO_DOCUMENT_FORMATS" :key="format.id" :value="format.id">{{ format.name }}</option></select>
          <small>切換到 Studio 時預設 Word（DOCX）；可選 HTML、PDF 或 DOCX。下載仍依登入身分與密等檢查。</small>
        </label>
      </template>
      <p v-else class="hint">輸出格式：RTF 文字文件（預設）。</p>
      <label>
        文件文字範本
        <textarea v-model="node.config.template" rows="6" maxlength="8000"/>
        <small v-if="documentMode === 'studio'">可留白，直接排版前一步文字；也可用下方固定標記套入文字。</small>
        <small v-pre>預設：主旨：{{text}}。{{text}} 會換成前一步內容；不執行程式。</small>
      </label>
    </template>
    <template v-if="node.kind === 'agent'">
      <fieldset>
        <legend>允許查詢的工具（預設不選）</legend>
        <label v-for="tool in catalog.mcp_tools" :key="tool.id" class="check">
          <input v-model="node.config.tool_ids" type="checkbox" :value="tool.id">
          {{ tool.name }}
          <small>{{ tool.description }}</small>
        </label>
        <p v-if="!catalog.mcp_tools?.length">沒有核准的唯讀工具，請聯絡平台管理員。</p>
      </fieldset>
      <label>
        最多步數
        <input v-model.number="node.config.max_steps" type="number" min="1" max="10">
        <small>預設 5 步；包含規劃、工具查詢與結果檢查。</small>
      </label>
      <label>
        最多重試次數
        <input v-model.number="node.config.max_retries" type="number" min="0" max="2">
        <small>預設 1 次；失敗超過上限就停止並回報。</small>
      </label>
    </template>
  </div>
</template>
<script setup>
import { computed, watch } from 'vue';
import { modules } from './graph.js';
import ToolParameter from './ToolParameter.vue';
import { initializeToolArguments } from './toolArguments.js';
import { DOCUMENT_DEFAULTS, DOCUMENT_PRESETS, STUDIO_DOCUMENT_FORMATS, availableDocumentPresets } from './documentSettings.js';
import { RAG_MODES } from './ragSettings.js';
const props = defineProps({
  node: Object,
  catalog: Object,
  fields: Array
});
const ragMode = computed({
  get: () => props.node.config.mode === 'fulltext' ? 'fulltext' : 'hybrid',
  set: mode => { if (RAG_MODES.some(item => item.id === mode)) props.node.config.mode = mode; }
});
const documentPresets = computed(() => availableDocumentPresets(props.catalog));
const documentMode = computed({
  get: () => props.node.config.mode === 'studio' ? 'studio' : 'text',
  set: mode => {
    if (!['text', 'studio'].includes(mode)) return;
    props.node.config.mode = mode;
    props.node.config.format = mode === 'studio' ? 'docx' : 'rtf';
    props.node.config.title ??= DOCUMENT_DEFAULTS.title;
    props.node.config.preset ??= DOCUMENT_DEFAULTS.preset;
    props.node.config.template_id ??= null;
  }
});
const documentTitle = computed({ get: () => props.node.config.title ?? DOCUMENT_DEFAULTS.title, set: title => { props.node.config.title = title; } });
const documentPreset = computed({
  get: () => props.node.config.preset || DOCUMENT_DEFAULTS.preset,
  set: preset => { if (DOCUMENT_PRESETS.some(item => item.id === preset)) props.node.config.preset = preset; }
});
const documentFormat = computed({
  get: () => STUDIO_DOCUMENT_FORMATS.some(item => item.id === props.node.config.format) ? props.node.config.format : 'docx',
  set: format => { if (STUDIO_DOCUMENT_FORMATS.some(item => item.id === format)) props.node.config.format = format; }
});
const module = computed(() => modules.find(m => m.kind === props.node.kind) || {
  description: '無法辨識的模組，請重新選擇。'
});
const selectedTool = computed(() => props.catalog.mcp_tools?.find(t => t.id === props.node.config.tool_id));
const toolProperties = computed(() => selectedTool.value?.input_schema?.properties || {});
const conditionType = computed({
  get: () => typeof props.node.config.value === 'number' ? 'number' : typeof props.node.config.value === 'boolean' ? 'boolean' : 'string',
  set: type => {
    const value = props.node.config.value;
    props.node.config.value = type === 'number' ? (Number.isFinite(Number(value)) ? Number(value) : 0) : type === 'boolean' ? value === true || value === 'true' : String(value ?? '');
  }
});
function initializeArguments() {
  if (props.node.kind !== 'mcp' || !selectedTool.value) return;
  props.node.config.arguments = initializeToolArguments(selectedTool.value.input_schema || {}, props.node.config.arguments);
}
watch(() => [props.node.id, props.node.config.tool_id, selectedTool.value?.input_schema], initializeArguments, { immediate:true });
function setArgument(key, value) {
  if (value === undefined) delete props.node.config.arguments[key];else props.node.config.arguments[key] = value;
}
const operators = {
  equals: '等於',
  not_equals: '不等於',
  contains: '包含文字',
  starts_with: '開頭是',
  gt: '大於',
  gte: '大於或等於',
  lt: '小於',
  lte: '小於或等於',
  is_empty: '未提供內容'
};
</script>
