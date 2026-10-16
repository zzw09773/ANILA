<template>
  <section class="qb-run-review" aria-label="試跑結果">
    <header class="qb-run-heading">
      <div><h3>助手回覆</h3><p role="status">執行狀態：{{ testRunStatus(result) }} · 第 {{ revision || '—' }} 版</p></div>
      <button v-if="result.status === 'failed'" type="button" @click="$emit('edit', failedNode)">回到流程修正</button>
    </header>
    <p v-if="question" class="qb-tested-question"><strong>這次提問</strong> {{ question }}</p>
    <div class="qb-answer">{{ resultText(result.output) || (result.status === 'failed' ? '這次沒有取得完整答案。請先查看失敗步驟的說明。' : '這次沒有留下回答文字，請查看步驟與模型完成資訊。') }}</div>
    <p v-for="item in completionNotices(result)" :key="item.type" role="status">{{ item.message }}</p>
    <div v-if="result.error && !completionErrorIsShown(result)" class="qb-step-error" role="alert">
      <p>{{ executionErrorNotice(result.error).message }}</p><p>錯誤類型：{{ executionErrorNotice(result.error).type }}</p>
      <p v-if="executionErrorNotice(result.error).action">{{ executionErrorNotice(result.error).action }}</p>
    </div>
    <ul v-if="result.artifacts?.length">
      <li v-for="artifact in result.artifacts" :key="artifact.download_url"><a v-if="safeUrl(artifact.download_url)" :href="artifact.download_url" download>{{ artifact.filename }}（下載文件草稿）</a></li>
    </ul>
    <h3>這次怎麼完成的</h3>
    <p class="hint">以下是伺服器實際回傳的步驟；未列出的分支不表示失敗。詳細紀錄依目前權限提供，敏感資料已遮罩。</p>
    <p v-if="!result.steps?.length">這次沒有取得逐步紀錄。請確認執行狀態與錯誤類型後再試。</p>
    <ol class="qb-run-steps">
      <li v-for="(step, i) in result.steps" :key="i" class="qb-run-step">
        <div class="qb-run-step-heading"><strong>{{ i + 1 }}. {{ nodeLabel(step.node_id) }}</strong><span>{{ step.status === 'incomplete' ? '回答未完成' : step.error ? '執行失敗' : '已收到紀錄' }}</span></div>
        <p>{{ stepSummary(step, nodeKind(step.node_id)) }}</p>
        <p v-for="item in completionNotices(step)" :key="item.type" role="status">{{ item.message }}</p>
        <div v-if="step.error && !completionErrorIsShown(step)" class="qb-step-error" role="alert">
          <p>{{ executionErrorNotice(step.error).message }}</p><p>錯誤類型：{{ executionErrorNotice(step.error).type }}</p>
          <p v-if="executionErrorNotice(step.error).action">{{ executionErrorNotice(step.error).action }}</p>
        </div>
        <button v-if="step.error && nodeKind(step.node_id)" type="button" @click="$emit('edit', step.node_id)">調整這個步驟</button>
        <details class="qb-technical">
          <summary>查看詳細紀錄 · {{ Number.isFinite(step.elapsed_ms) ? step.elapsed_ms : '未提供' }} 毫秒</summary>
          <section class="qb-model-completions" aria-label="模型完成資訊"><h4>模型完成資訊</h4>
            <p v-if="!modelCompletionDetails(step).length">此步驟未提供模型完成資訊。</p>
            <div v-for="(item, index) in modelCompletionDetails(step)" :key="index"><p>第 {{ index + 1 }} 次模型呼叫 · 結束原因：{{ item.reason }}</p><ul><li v-for="usage in item.usage" :key="usage.label">{{ usage.label }}：{{ usage.value }}</li></ul><p>{{ item.usageStatus }}</p></div>
          </section>
          <h4>輸入</h4><pre>{{ pretty(step.input) }}</pre><h4>輸出</h4><pre>{{ pretty(step.output) }}</pre>
          <template v-if="step.retrieval || step.retrieval_context"><h4>檢索文件片段與來源</h4><p v-for="text in retrievalNotices(step.retrieval, step.retrieval_context)" :key="text" role="status">{{ text }}</p><pre v-if="step.retrieval">{{ pretty(step.retrieval) }}</pre></template>
          <template v-if="step.tool"><h4>工具參數與結果</h4><pre>{{ pretty(step.tool) }}</pre></template>
          <template v-if="modelContextNotices(step.context_replacements).length"><h4>模型內容調整</h4><ul><li v-for="(event, index) in modelContextNotices(step.context_replacements)" :key="index">{{ contextLabel(event.sourceId) }}：{{ event.message }}</li></ul></template>
        </details>
      </li>
    </ol>
  </section>
</template>
<script setup>
import { computed } from 'vue';
import { maskTrace } from './graph.js';
import { completionNotices, completionErrorIsShown, testRunStatus, modelCompletionDetails } from './completion.js';
import { executionErrorNotice } from './executionError.js';
import { retrievalNotices, modelContextNotices } from './retrievalNotices.js';
import { stepSummary, resultText } from './builderGuidance.js';
const props = defineProps({result:Object,graph:Object,revision:Number,question:String});
defineEmits(['edit']);
const nodeLabel = id => props.graph?.nodes.find(n => n.id === id)?.label || '未標示的步驟';
const nodeKind = id => props.graph?.nodes.find(n => n.id === id)?.kind;
const contextLabel = id => props.graph?.nodes.find(n => n.id === id)?.label || '未標示的文件步驟';
const failedNode = computed(() => props.result.steps?.find(s => s.error)?.node_id);
const pretty = value => typeof value === 'string' ? maskTrace(value) : JSON.stringify(maskTrace(value), null, 2) ?? '未提供';
const safeUrl = v => typeof v === 'string' && v.startsWith('/api/') && !v.includes('\\');
</script>
