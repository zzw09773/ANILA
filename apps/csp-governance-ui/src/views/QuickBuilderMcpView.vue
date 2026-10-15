<template>
  <div class="page qb">
    <PageHead title="助手查詢工具" subtitle="登錄院內 MCP 服務，逐項確認唯讀工具後才能供助手使用。每次查詢以提問者身分執行。">
      <template #actions>
        <button :disabled="blocked" @click="load">重新整理</button>
      </template>
    </PageHead>
    <p v-if="identityMessage" class="qb-error" role="alert">{{ identityMessage }}</p>
    <p v-if="error" class="qb-error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading">正在讀取服務…</p>
    <fieldset class="qb-content" :disabled="blocked || !actorReady">
    <section class="qb-section">
      <h2>登錄服務</h2>
      <form @submit.prevent="register">
        <fieldset :disabled="busy">
          <div class="qb-meta-grid">
            <label>
              服務名稱
              <input v-model="form.name" required maxlength="120">
            </label>
            <label>
              院內服務網址
              <input v-model="form.endpoint_url" type="url" required placeholder="https://院內主機/mcp">
            </label>
            <label>
              所屬單位編號
              <input v-model.number="form.department_id" type="number" min="1" :required="!form.is_public" placeholder="非全院服務必填">
            </label>
            <label class="check">
              <input v-model="form.is_public" type="checkbox">
              開放全院使用
            </label>
          </div>
          <label>
            用途說明
            <textarea v-model="form.description" rows="2" maxlength="2000"/>
          </label>
          <p class="hint">登錄後平台讀取工具說明與參數。服務需通過院內主機與連線檢查；這裡不填資料庫帳密。</p>
          <button class="primary" type="submit">登錄並讀取工具</button>
        </fieldset>
      </form>
    </section>
    <p v-if="!loading && !services.length">尚未登錄查詢服務。先填寫上方欄位，再確認可開放的唯讀工具。</p>
    <section v-for="service in services" :key="service.id" class="qb-service">
      <div class="qb-actions">
        <h2>{{ service.name }}</h2>
        <span>{{ ({pending:'待確認工具',approved:'已核准',disabled:'已停用'})[service.status] || service.status }}</span>
        <span>{{ service.is_public ? '全院' : '限授權單位' }}</span>
      </div>
      <p>{{ service.description }}</p>
      <p class="hint">{{ service.endpoint_url }} · 單位 {{ service.department_id || '未指定' }}</p>
      <p v-if="service.discovery_error" class="qb-error">{{ service.discovery_error.message }}（{{ service.discovery_error.error_type }}）。請確認服務設定後重新擷取工具。</p>
      <button :disabled="busy" @click="refresh(service)">重新擷取工具</button>
      <fieldset :disabled="busy || service.status === 'disabled'">
        <legend>確認唯讀工具</legend>
        <p>工具宣告僅供參考。管理員須確認實際行為僅查詢資料；工具說明或參數變更後，必須重新核准。</p>
        <p v-if="!service.tools?.length">此服務沒有可核准的工具，請確認服務設定。</p>
        <div class="qb-table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">允許使用</th>
                <th scope="col">工具與說明</th>
                <th scope="col">參數與內容版本</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="tool in service.tools" :key="tool.id">
                <td>
                  <input v-model="selectedTools[service.id]" type="checkbox" :value="tool.id" :aria-label="`允許${tool.name}唯讀查詢`">
                  <p>{{ tool.is_approved ? '已核准' : '未核准' }}{{ tool.is_read_only ? ' · 唯讀' : '' }}</p>
                </td>
                <td>
                  <strong>{{ tool.name }}</strong>
                  <p>{{ tool.description }}</p>
                  <small>服務宣告：{{ tool.annotations?.readOnlyHint === true ? '唯讀' : '未宣告唯讀' }}</small>
                </td>
                <td>
                  <details>
                    <summary>檢視參數與宣告</summary>
                    <pre>{{ pretty({input_schema:tool.input_schema,annotations:tool.annotations}) }}</pre>
                  </details>
                  <small>
                    目前指紋：
                    {{ tool.schema_fingerprint }}
                    <br>
                    核准指紋：
                    {{ tool.approved_fingerprint || '尚未核准' }}
                  </small>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <label class="check">
          <input v-model="confirmed[service.id]" type="checkbox">
          我已確認所選工具僅供查詢
        </label>
        <button :disabled="!confirmed[service.id] || !selectedTools[service.id]?.length" @click="approve(service)">儲存唯讀核准清單</button>
        <small>至少選擇一項；未選取的工具會撤銷核准。全部停用請使用「停用此服務」。</small>
      </fieldset>
      <button class="danger" :disabled="busy || service.status === 'disabled'" @click="disable(service)">停用此服務</button>
    </section>
    </fieldset>
  </div>
</template>
<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import client from '../api/client';
import { extractError, errorType } from '../api/errors';
import { PageHead } from '../components/cli';
import { useDraftActor } from '../quickBuilder/useDraftActor';
import { maskTrace } from '../quickBuilder/graph';
const services = ref([]),
  error = ref(''),
  notice = ref(''),
  loading = ref(false),
  busy = ref(false),
  selectedTools = reactive({}),
  confirmed = reactive({});
const form = reactive({
  name: '',
  description: '',
  endpoint_url: '',
  department_id: null,
  is_public: false
});
const root = '/api/quick-builder/mcp';
const actor = useDraftActor();
const { identityChanged, identityMessage, ready: actorReady } = actor;
const blocked = computed(() => busy.value || loading.value || identityChanged.value);
const pretty = value => JSON.stringify(maskTrace(value), null, 2);
function fail(e) {
  const type = errorType(e, e.code || 'MCP_GOVERNANCE');
  if (type === 'IDENTITY_CHANGED') { actor.lockIdentity(); error.value = ''; notice.value = ''; return; }
  error.value = `${extractError(e, '服務操作未完成')}（${type}）。請檢查院內服務設定後重試。`;
}
async function load() {
  if (loading.value || identityChanged.value) return;
  loading.value = true;
  error.value = '';
  try {
    const [r, c] = await Promise.all([client.get(root), client.get('/api/quick-builder/catalog')]);
    if (!actor.captureCatalog(c.data)) return;
    services.value = r.data.services || r.data;
    for (const s of services.value) {
      selectedTools[s.id] = s.tools.filter(t => t.is_approved).map(t => t.id);
      confirmed[s.id] = false;
    }
  } catch (e) {
    fail(e);
  } finally {
    loading.value = false;
  }
}
async function mutate(path, body, success) {
  if (blocked.value || !actorReady.value) return;
  busy.value = true;
  error.value = '';
  try {
    await client.post(path, body, actor.mutationOptions());
    await load();
    notice.value = success;
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
async function register() {
  await mutate(root, {
    ...form,
    department_id: form.department_id || null
  }, '已登錄服務，請確認每項工具的實際行為，再核准唯讀清單。');
}
async function approve(service) {
  if (!confirmed[service.id]) return;
  await mutate(`${root}/${service.id}/approve`, {
    approved_tool_ids: selectedTools[service.id]
  }, '唯讀核准清單已更新。只有這些工具可在助手畫布選用。');
}
async function refresh(service) {
  if (window.confirm('重新擷取會撤銷原核准清單，需重新確認工具後才能使用。確定重新擷取？')) await mutate(`${root}/${service.id}/refresh`, {}, '已重新擷取工具，請重新確認唯讀核准清單。');
}
async function disable(service) {
  if (window.confirm(`停用「${service.name}」後，使用此服務的助手無法再查詢。確定停用？`)) await mutate(`${root}/${service.id}/disable`, {}, '服務已停用。');
}
onMounted(load);
</script>
<style src="../quickBuilder/builder.css"></style>
