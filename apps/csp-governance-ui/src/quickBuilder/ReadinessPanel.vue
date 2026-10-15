<template>
  <details class="qb-section qb-readiness">
    <summary>離線文件工具檢查</summary>
    <p>僅檢查目前回應請求的 CSP 程序、本機必要模組與轉檔、OCR 工具。模組檢查只確認套件資料存在，未初始化套件或實際解析文件。結果不代表背景工作程序、遠端 Docling 服務或正式部署映像已就緒，也不保證所有文件都能完整解析。</p>
    <div class="qb-actions">
      <button :disabled="checking" @click="check">{{ checking ? '正在檢查離線工具…' : '檢查離線工具' }}</button>
      <span v-if="checking" role="status">檢查可能需要十幾秒，可繼續編輯助手草稿。</span>
    </div>
    <p v-if="error" role="alert" class="qb-error">{{ error }}</p>
    <template v-if="result">
      <p role="status"><strong>{{ result.ready ? '目前 CSP 程序的工具已就緒' : '目前 CSP 程序的工具需要處理' }}</strong>；此結果只適用本次檢查。<span v-if="result.checkedAt">檢查時間：{{ formatDate(result.checkedAt) }}（台北時間）</span></p>
      <div class="qb-table-scroll">
        <table>
          <caption>本機工具檢查結果</caption>
          <thead><tr><th scope="col">項目</th><th scope="col">狀態</th><th scope="col">說明與處理方式</th></tr></thead>
          <tbody>
            <tr v-for="item in result.checks" :key="item.id">
              <th scope="row">{{ item.label }}</th>
              <td>{{ item.ready ? '已就緒' : '需要處理' }}</td>
              <td>{{ item.message }} {{ item.action }}<small v-if="!item.ready" class="qb-readiness-type">錯誤類型：{{ item.type }}</small></td>
            </tr>
          </tbody>
        </table>
      </div>
      <details v-if="result.formats.length" class="qb-readiness-formats">
        <summary>查看文件格式預檢（{{ result.formats.length }} 項）</summary>
        <p class="hint">僅確認格式所需工具，不代表文件內容已成功擷取。</p>
        <div class="qb-table-scroll">
          <table>
            <caption>文件格式與必要工具狀態</caption>
            <thead><tr><th scope="col">文件格式</th><th scope="col">副檔名</th><th scope="col">狀態</th></tr></thead>
            <tbody><tr v-for="item in result.formats" :key="item.id"><th scope="row">{{ item.label }}</th><td>{{ item.extensions }}</td><td>{{ item.ready ? '必要工具已就緒' : '尚缺必要工具' }}</td></tr></tbody>
          </table>
        </div>
      </details>
      <p class="hint">未檢查：背景文件工作程序、遠端 Docling、正式部署映像。工具就緒狀態不會變更助手草稿或文件權限。</p>
    </template>
  </details>
</template>
<script setup>
import { onBeforeUnmount, ref } from 'vue'
import { getReadiness } from '../api/quickBuilder'
import { formatDate } from '../utils/formatDate'
import { readinessView } from './readiness'

const checking = ref(false), result = ref(null), error = ref('')
let active = true
onBeforeUnmount(() => { active = false; result.value = null })
async function check() {
  if (checking.value) return
  checking.value = true
  result.value = null
  error.value = ''
  try {
    const response = await getReadiness()
    if (active) result.value = readinessView(response.data)
  } catch (e) {
    if (!active) return
    const status = e?.response?.status
    const type = e?.code === 'READINESS_INVALID_RESPONSE' ? e.code : Number.isInteger(status) ? `HTTP_${status}` : 'READINESS_UNAVAILABLE'
    error.value = status === 401 || status === 403
      ? `目前沒有檢查權限，請用管理員身分登入（${type}）。`
      : `暫時無法完成離線工具檢查，請稍後重試（${type}）。`
  } finally {
    if (active) checking.value = false
  }
}
</script>
<style scoped>
.qb-readiness caption { text-align:left; font-weight:600; margin-bottom:10px; }
.qb-readiness th:first-child { width:24%; }
.qb-readiness td:nth-child(2) { width:12%; white-space:nowrap; }
.qb-readiness-type { display:block; margin-top:4px; }
.qb-readiness-formats { margin-top:16px; }
</style>
