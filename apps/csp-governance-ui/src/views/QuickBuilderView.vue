<template>
  <div class="page qb">
    <PageHead title="助手快製" subtitle="從範本開始，用拖拉與設定建立單位助手。主管核准後註冊為 Agent，再由平台管理員核准使用。">
      <template #actions>
        <button :disabled="blocked || !actorReady" @click="newAssistant">建立助手</button>
        <button :disabled="blocked" @click="load">重新整理</button>
      </template>
    </PageHead>
    <p v-if="identityMessage" role="alert" class="qb-error">{{ identityMessage }}</p>
    <p v-if="error" role="alert" class="qb-error">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <p v-if="loading" role="status">正在讀取助手與可用資源…</p>
    <fieldset class="qb-content" :disabled="blocked || !actorReady">
    <div class="qb-top">
      <label>
        助手
        <select :value="detail?.id || ''" :disabled="busy" @change="chooseAssistant($event)">
          <option value="">新助手</option>
          <option v-for="a in assistants" :key="a.id" :value="a.id">{{ a.name }}{{ a.disabled ? '（已停用）' : '' }}</option>
        </select>
      </label>
      <label>
        情境範本
        <select v-model="templateId" :disabled="!editable || busy">
          <option value="">請選擇範本</option>
          <option v-for="t in templates" :key="t.id" :value="t.id">{{ t.name }}</option>
        </select>
      </label>
      <button :disabled="!templateId || !editable || busy" @click="applyTemplate">套用範本</button>
    </div>
    <p v-if="chosenTemplate" class="hint">
      {{ chosenTemplate.description }}
      {{ (chosenTemplate.configuration_notes || chosenTemplate.required_settings || []).join?.('；') || chosenTemplate.configuration_notes }}
    </p>
    <details v-if="catalog.can_manage_templates" class="qb-section">
      <summary>管理情境範本</summary>
      <p>將目前畫布與用途另存為範本；不包含模型、文件存取權或工具核准權。內建範本可複製新增，單位自訂範本可更新。</p>
      <label>
        範本名稱
        <input v-model="templateName" maxlength="100" required>
      </label>
      <label>
        範本用途
        <textarea v-model="templateDescription" maxlength="1000" rows="2"/>
      </label>
      <div class="qb-actions">
        <button :disabled="busy || !templateName.trim()" @click="saveTemplate(false)">另存為新範本</button>
        <button :disabled="busy || !templateName.trim() || typeof chosenTemplate?.id !== 'number'" @click="saveTemplate(true)">更新選取的自訂範本</button>
      </div>
    </details>
    <details v-if="catalog.can_manage_templates" class="qb-section">
      <summary>管理文件集開放範圍</summary>
      <p>這裡核准的是文件本身的查詢範圍。助手公開不會自動公開文件；開放後，指定範圍內且符合密等的人員才可檢索。</p>
      <p v-if="!catalog.collections?.length">尚無可管理的文件集。</p>
      <fieldset v-for="collection in catalog.collections" :key="collection.id" :disabled="busy">
        <legend>{{ collection.name }}</legend>
        <label>
          文件開放範圍
          <select v-model="collection.publication_scope">
            <option value="unit">本單位</option>
            <option value="departments">指定單位</option>
            <option value="public">全院</option>
          </select>
        </label>
        <label v-if="collection.publication_scope === 'departments'">
          可查詢的單位編號
          <input :value="(collection.target_department_ids || []).join(', ')" @input="collection.target_department_ids = parseDepartments($event.target.value)">
          <small>以逗號分隔單位編號。請確認文件內容適合開放給這些單位。</small>
        </label>
        <button :disabled="!collection.publication_scope" @click="saveCollectionPublication(collection)">儲存文件開放範圍</button>
      </fieldset>
    </details>
    <form class="qb-metadata" @submit.prevent="save">
      <fieldset :disabled="!editable || busy">
        <legend>助手設定</legend>
        <div class="qb-meta-grid">
          <label>
            助手名稱
            <input v-model="form.name" required maxlength="80" placeholder="例如：規章問答助手">
          </label>
          <label>
            平台模型
            <select v-model="form.model_id" required>
              <option value="">請選擇</option>
              <option v-for="m in catalog.models" :key="m.id" :value="m.id">{{ m.name || m.display_name }}</option>
            </select>
          </label>
          <label>
            密等
            <select v-model="form.classification_level">
              <option>無機密</option>
              <option>營業秘密</option>
              <option>密</option>
              <option>機密</option>
            </select>
          </label>
          <label>
            開放範圍
            <select v-model="form.scope">
              <option value="unit">本單位（預設）</option>
              <option value="departments">指定單位</option>
              <option value="public">全院</option>
            </select>
          </label>
        </div>
        <label>
          用途說明
          <textarea v-model="form.description" rows="2" maxlength="200"/>
        </label>
        <label v-if="form.scope === 'departments'">
          指定單位編號
          <input :value="form.target_department_ids.join(', ')" @input="setDepartments($event.target.value)">
          <small>以逗號分隔單位編號。實際存取權限由平台檢查。</small>
        </label>
        <small>公開助手不會自動公開文件。模型、文件與工具仍依提問者的權限使用。</small>
      </fieldset>
      <div class="qb-actions">
        <button type="submit" class="primary" :disabled="busy || !editable || !dirty">{{ busy ? '處理中…' : '儲存新版本' }}</button>
        <span>{{ dirty ? '有尚未儲存的變更' : '目前內容已儲存' }}</span>
      </div>
    </form>
    <div class="qb-tabs" aria-label="助手工作區">
      <button v-for="tab in tabs" :key="tab.id" :aria-pressed="panel === tab.id" @click="panel = tab.id">{{ tab.label }}</button>
    </div>
    <template v-if="panel === 'canvas'">
      <p class="hint">從模組庫拖入畫布，或按模組加入。拖曳步驟標題可移動；標題取得焦點後可用方向鍵移動。拖曳出口到另一個步驟的「接到這裡」即可連線，也可在下方表單選起點與終點。</p>
      <div class="qb-workspace">
        <aside class="qb-palette" aria-label="模組庫">
          <h2>模組庫</h2>
          <button v-for="m in modules" :key="m.kind" :disabled="!editable || busy" draggable="true" :title="m.description" @dragstart="paletteDrag($event, m.kind)" @click="addNode(m.kind)">
            {{ m.name }}
            <small>{{ m.description }}</small>
          </button>
          <h3>核准的查詢工具</h3>
          <button v-for="tool in catalog.mcp_tools" :key="tool.id" :disabled="!editable || busy" draggable="true" @dragstart="paletteDrag($event, 'mcp', tool.id)" @click="addNode('mcp', null, tool.id)">
            {{ tool.name }}
            <small>{{ tool.description }}</small>
          </button>
          <p v-if="!catalog.mcp_tools?.length">尚無可用工具。請聯絡管理員登錄與核准。</p>
        </aside>
        <div class="qb-canvas-scroll" tabindex="0" aria-label="流程畫布，可捲動查看所有步驟" @dragover.prevent @drop.prevent="dropNode">
          <div class="qb-canvas" :style="{width: canvasWidth + 'px', height: canvasHeight + 'px'}">
            <svg :width="canvasWidth" :height="canvasHeight" class="qb-lines" aria-hidden="true">
              <defs>
                <marker id="qb-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
                  <path d="M0,0 L0,6 L8,3 z" fill="currentColor"/>
                </marker>
              </defs>
              <g v-for="(edge, i) in form.graph.edges" :key="i">
                <path :d="edgePath(edge)" marker-end="url(#qb-arrow)"/>
                <text :x="edgePoint(edge).x" :y="edgePoint(edge).y">{{ edge.branch === 'true' ? '成立' : edge.branch === 'false' ? '不成立' : '' }}</text>
              </g>
            </svg>
            <article v-for="node in form.graph.nodes" :key="node.id" class="qb-node" :aria-label="`流程步驟：${node.label}`" :class="{selected: selectedId === node.id}" :style="{left: node.position.x + 'px', top: node.position.y + 'px'}">
              <button class="qb-node-title" :aria-label="`選取${node.label}；方向鍵移動`" @click="selectedId = node.id" @pointerdown="startMove($event, node)" @pointermove="moveNode($event, node)" @pointerup="endMove" @pointercancel="endMove" @keydown="moveKey($event, node)">
                {{ node.label }}
              </button>
              <small>{{ modules.find(m => m.kind === node.kind)?.name }}</small>
              <div class="qb-ports">
                <template v-if="node.kind === 'condition'">
                  <button :disabled="!editable || busy" draggable="true" :aria-label="`從${node.label}的成立出口連線`" @dragstart="edgeDrag($event, node.id, 'true')" @click="sourceId = node.id; branch = 'true'">成立 →</button>
                  <button :disabled="!editable || busy" draggable="true" :aria-label="`從${node.label}的不成立出口連線`" @dragstart="edgeDrag($event, node.id, 'false')" @click="sourceId = node.id; branch = 'false'">不成立 →</button>
                </template>
                <button v-else :disabled="!editable || busy || node.kind === 'output'" draggable="true" :aria-label="`從${node.label}連線`" @dragstart="edgeDrag($event, node.id)" @click="sourceId = node.id">從此連線</button>
                <button :disabled="!editable || busy || node.kind === 'input'" :aria-label="`接到${node.label}`" @dragover.prevent @drop.stop.prevent="edgeDrop($event, node.id)" @click="targetId = node.id; addEdge()">
                  接到這裡
                </button>
              </div>
            </article>
          </div>
        </div>
        <aside class="qb-config" aria-label="步驟設定">
          <h2>步驟設定</h2>
          <p v-if="!selected">選取畫布上的步驟，再調整設定。</p>
          <fieldset v-else :disabled="!editable || busy">
            <NodeSettings :key="selected.id" :node="selected" :catalog="catalog" :fields="extractFields"/>
            <button class="danger" @click="deleteNode">移除這個步驟</button>
          </fieldset>
        </aside>
      </div>
      <section class="qb-connections">
        <h2>流程連線</h2>
        <fieldset :disabled="!editable || busy">
          <div class="qb-top">
            <label>
              起點
              <select v-model="sourceId">
                <option value="">請選擇</option>
                <option v-for="n in form.graph.nodes.filter(n => n.kind !== 'output')" :key="n.id" :value="n.id">{{ n.label }}</option>
              </select>
            </label>
            <label v-if="sourceNode?.kind === 'condition'">
              條件出口
              <select v-model="branch">
                <option value="true">成立（true）</option>
                <option value="false">不成立（false）</option>
              </select>
            </label>
            <label>
              終點
              <select v-model="targetId">
                <option value="">請選擇</option>
                <option v-for="n in form.graph.nodes.filter(n => n.kind !== 'input')" :key="n.id" :value="n.id">{{ n.label }}</option>
              </select>
            </label>
            <button :disabled="!sourceId || !targetId" @click="addEdge">新增連線</button>
          </div>
          <ul>
            <li v-for="(e, i) in form.graph.edges" :key="i">
              {{ nodeLabel(e.source) }}
              →
              {{ nodeLabel(e.target) }}
              {{ e.branch === 'true' ? '／成立' : e.branch === 'false' ? '／不成立' : '' }}
              <button :aria-label="`移除${nodeLabel(e.source)}到${nodeLabel(e.target)}的連線`" @click="form.graph.edges.splice(i, 1)">移除</button>
            </li>
          </ul>
        </fieldset>
      </section>
    </template>
    <section v-if="panel === 'versions'" class="qb-section">
      <h2>版本與審核</h2>
      <p>每次儲存建立新版本；回復會另建草稿。修改後須重新送審，原已發布版本持續運作。</p>
      <p v-if="!versions.length">尚未儲存。請先填寫助手設定並儲存新版本。</p>
      <div class="qb-top">
        <label>
          檢視版本
          <select :value="versionId" @change="viewVersion($event)">
            <option v-for="v in versions" :key="v.id" :value="v.id">第 {{ v.revision }} 版 · {{ statusLabel(v.status) }}</option>
          </select>
        </label>
        <span v-if="version">{{ statusLabel(version.status) }}{{ detail?.disabled ? '／助手已停用' : '' }}</span>
      </div>
      <p v-if="version?.review_reason">審核說明：{{ version.review_reason }}</p>
      <p v-if="version?.status === 'rejected'">此版本已退回。請調整內容並儲存新版本，再重新送審。</p>
      <div v-if="version" class="qb-actions">
        <button :disabled="busy || dirty || !detail?.can_edit" @click="action('restore', {version_id:version.id, expected_revision: detail.draft_revision})">回復為新草稿</button>
        <button :disabled="busy || dirty || !detail?.can_edit || version.status !== 'draft'" @click="action('submit', {version_id:version.id})">送主管審核</button>
        <button :disabled="busy || dirty || !detail?.can_edit || version.status !== 'approved'" @click="action('publish', {version_id:version.id})">註冊為 Agent</button>
        <button class="danger" :disabled="blocked || dirty || !detail?.can_edit || detail?.disabled" @click="disableAssistant">停用助手</button>
      </div>
      <p v-if="dirty" role="status">有尚未儲存的變更。請先儲存或捨棄變更，再送審、註冊或停用助手。</p>
      <p>註冊後仍須平台管理員在 Agent 頁核准，同仁才可使用。</p>
      <fieldset v-if="detail?.can_review && version && ['pending','submitted'].includes(version.status)" :disabled="busy || dirty">
        <legend>主管審核第 {{ version.revision }} 版</legend>
        <label>
          審核說明
          <textarea v-model="reviewReason" maxlength="1000" rows="2"/>
        </label>
        <div class="qb-actions">
          <button @click="action('review', {version_id:version.id, approve:true, reason:reviewReason})">核准這個版本</button>
          <button :disabled="!reviewReason.trim()" @click="action('review', {version_id:version.id, approve:false, reason:reviewReason})">附理由退回</button>
        </div>
      </fieldset>
      <h3>比較兩個版本</h3>
      <div class="qb-top">
        <label>
          較早版本
          <select v-model="compareLeft">
            <option value="">請選擇</option>
            <option v-for="v in versions" :key="v.id" :value="v.id">第 {{ v.revision }} 版</option>
          </select>
        </label>
        <label>
          較新版本
          <select v-model="compareRight">
            <option value="">請選擇</option>
            <option v-for="v in versions" :key="v.id" :value="v.id">第 {{ v.revision }} 版</option>
          </select>
        </label>
        <button :disabled="busy || !compareLeft || !compareRight || compareLeft === compareRight" @click="compare">比較版本</button>
      </div>
      <div v-if="comparison" class="qb-table-scroll">
        <p v-if="!comparisonRows.length">兩個版本的內容相同。</p>
        <table v-else>
          <thead>
            <tr>
              <th scope="col">變更項目</th>
              <th scope="col">原版本</th>
              <th scope="col">新版</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="change in comparisonRows" :key="change.field">
              <th scope="row">{{ fieldLabels[change.field] || change.field }}</th>
              <td>
                <pre>{{ compareValue(change.field, change.before) }}</pre>
              </td>
              <td>
                <pre>{{ compareValue(change.field, change.after) }}</pre>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <section v-if="panel === 'test'" class="qb-section">
      <h2>測試對話</h2>
      <p>測試第 {{ version?.revision || '—' }} 版。請先儲存變更；會使用目前登入者的資料權限。</p>
      <form @submit.prevent="test">
        <label>
          測試問題
          <textarea v-model="message" rows="3" required maxlength="8000" placeholder="例如：出差申請需要哪些文件？"/>
        </label>
        <button class="primary" type="submit" :disabled="busy || dirty || !version || !message.trim()">{{ testing ? '正在測試…' : '傳送測試' }}</button>
      </form>
      <template v-if="testResult">
        <h3>助手回覆</h3>
        <pre>{{ pretty(testResult.output) }}</pre>
        <p>執行狀態：{{ testResult.status === 'completed' ? '完成' : testResult.status === 'failed' ? '失敗' : testResult.status }}</p>
        <ul v-if="testResult.artifacts?.length">
          <li v-for="artifact in testResult.artifacts" :key="artifact.download_url">
            <a v-if="safeArtifactUrl(artifact.download_url)" :href="artifact.download_url" download>{{ artifact.filename }}（下載文件草稿）</a>
          </li>
        </ul>
        <h3>逐步紀錄（敏感資料已遮罩）</h3>
        <details v-for="(step, i) in testResult.steps" :key="i">
          <summary>{{ i + 1 }}. {{ nodeLabel(step.node_id) }} · {{ step.elapsed_ms }} 毫秒{{ step.error ? ' · 執行失敗' : '' }}</summary>
          <h4>輸入</h4>
          <pre>{{ pretty(step.input) }}</pre>
          <h4>輸出</h4>
          <pre>{{ pretty(step.output) }}</pre>
          <template v-if="step.retrieval">
            <h4>檢索文件片段與來源</h4>
            <pre>{{ pretty(step.retrieval) }}</pre>
          </template>
          <template v-if="step.tool">
            <h4>工具參數與結果</h4>
            <pre>{{ pretty(step.tool) }}</pre>
          </template>
          <p v-if="step.error" role="alert">{{ pretty(step.error) }}；請檢查這個步驟的設定後重試。</p>
        </details>
      </template>
    </section>
    </fieldset>
  </div>
</template>
<script setup>
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue';
import { onBeforeRouteLeave } from 'vue-router';
import { PageHead } from '../components/cli';
import { extractError, errorType } from '../api/errors';
import * as api from '../api/quickBuilder';
import NodeSettings from '../quickBuilder/NodeSettings.vue';
import { useDraftActor } from '../quickBuilder/useDraftActor';
import { initializeToolArguments } from '../quickBuilder/toolArguments';
import { clone, modules, initialGraph, makeNode, connect, removeNode, maskTrace, statusLabel } from '../quickBuilder/graph';
const assistants = ref([]),
  templates = ref([]),
  catalog = ref({
    models: [],
    collections: [],
    mcp_tools: []
  }),
  detail = ref(null),
  versionId = ref(''),
  selectedId = ref(''),
  sourceId = ref(''),
  targetId = ref(''),
  branch = ref('true'),
  templateId = ref(''),
  panel = ref('canvas'),
  error = ref(''),
  notice = ref(''),
  loading = ref(false),
  busy = ref(false),
  testing = ref(false),
  message = ref(''),
  testResult = ref(null),
  reviewReason = ref(''),
  compareLeft = ref(''),
  compareRight = ref(''),
  comparison = ref(null),
  baseline = ref(''),
  templateName = ref(''),
  templateDescription = ref('');
const actor = useDraftActor();
const { identityChanged, identityMessage, ready: actorReady } = actor;
const blocked = computed(() => busy.value || loading.value || identityChanged.value);
const tabs = [{
  id: 'canvas',
  label: '流程畫布'
}, {
  id: 'versions',
  label: '版本與審核'
}, {
  id: 'test',
  label: '測試對話'
}];
const blank = () => ({
  name: '',
  description: '',
  graph: initialGraph(),
  model_id: '',
  classification_level: '無機密',
  collection_ids: [],
  scope: 'unit',
  target_department_ids: []
});
const form = reactive(blank());
const versions = computed(() => detail.value?.versions || []);
const version = computed(() => versions.value.find(v => String(v.id) === String(versionId.value)));
const editable = computed(() => !blocked.value && actorReady.value && (!detail.value || detail.value.can_edit === true && (!version.value || Number(version.value.revision) === Number(detail.value.draft_revision))));
const dirty = computed(() => JSON.stringify(form) !== baseline.value);
const selected = computed(() => form.graph.nodes.find(n => n.id === selectedId.value));
const sourceNode = computed(() => form.graph.nodes.find(n => n.id === sourceId.value));
const chosenTemplate = computed(() => templates.value.find(t => String(t.id) === String(templateId.value)));
const extractFields = computed(() => [...new Set(form.graph.nodes.filter(n => n.kind === 'extract').flatMap(n => n.config.fields || []))]);
const canvasWidth = computed(() => Math.max(740, ...form.graph.nodes.map(n => n.position.x + 220)));
const canvasHeight = computed(() => Math.max(500, ...form.graph.nodes.map(n => n.position.y + 150)));
const fieldLabels = {
  name: '助手名稱',
  description: '用途說明',
  graph: '流程與步驟設定',
  model_id: '平台模型',
  classification_level: '密等',
  scope: '開放範圍',
  target_department_ids: '指定單位',
  collection_ids: '文件集'
};
const comparisonRows = computed(() => Array.isArray(comparison.value?.changes) ? comparison.value.changes : Object.entries(comparison.value?.changes || {}).map(([field, values]) => ({
  field,
  before: values.left,
  after: values.right
})));
function compareValue(field, value) {
  if (field === 'scope') return {
    unit: '本單位',
    public: '全院',
    departments: '指定單位'
  }[value] || value;
  if (field === 'model_id') return catalog.value.models.find(m => m.id === value)?.name || value;
  if (field === 'collection_ids') return (value || []).map(id => catalog.value.collections.find(c => c.id === id)?.name || id).join('、') || '未選擇';
  return pretty(value);
}
const nodeLabel = id => form.graph.nodes.find(n => n.id === id)?.label || id;
const pretty = value => typeof value === 'string' ? maskTrace(value) : JSON.stringify(maskTrace(value), null, 2) ?? '未提供';
function safeArtifactUrl(value) {
  return typeof value === 'string' && value.startsWith('/api/') && !value.includes('\\');
}
function fail(e) {
  const type = errorType(e, e.code || 'QUICK_BUILDER');
  if (type === 'IDENTITY_CHANGED') { actor.lockIdentity(); error.value = ''; notice.value = ''; return; }
  error.value = `${extractError(e, '暫時無法完成，請稍後重試')}（${type}）。請檢查設定後重試；若版本衝突，先保留變更內容再重新整理。`;
}
function discard() {
  return !dirty.value || window.confirm('目前有尚未儲存的變更。確定捨棄這些變更？');
}
function setForm(data) {
  const next = blank();
  for (const key of Object.keys(next)) {
    if (data?.[key] !== undefined) next[key] = clone(data[key]);
  }
  for (const node of next.graph.nodes) {
    if (node.kind === 'mcp') {
      const tool = catalog.value.mcp_tools?.find(tool => tool.id === node.config.tool_id);
      if (tool) node.config.arguments = initializeToolArguments(tool.input_schema || {}, node.config.arguments);
    }
  }
  Object.assign(form, next);
  selectedId.value = form.graph.nodes[0]?.id || '';
  baseline.value = JSON.stringify(form);
  testResult.value = null;
}
function newAssistant() {
  if (blocked.value || !actorReady.value || !discard()) return;
  detail.value = null;
  versionId.value = '';
  templateId.value = '';
  setForm(blank());
  panel.value = 'canvas';
  notice.value = '';
}
async function load() {
  if (blocked.value || !discard()) return;
  loading.value = true;
  error.value = '';
  try {
    const [a, t, c] = await Promise.all([api.listAssistants(), api.listTemplates(), api.getCatalog()]);
    if (!actor.captureCatalog(c.data)) return;
    assistants.value = a.data.assistants;
    templates.value = t.data.templates;
    catalog.value = c.data;
    if (detail.value) await fetchDetail(detail.value.id);
  } catch (e) {
    fail(e);
  } finally {
    loading.value = false;
  }
}
async function fetchDetail(id) {
  comparison.value = null;
  const current = await api.getCatalog();
  if (!actor.captureCatalog(current.data)) return false;
  const res = await api.getAssistant(id);
  detail.value = res.data;
  const latest = [...versions.value].sort((a, b) => b.revision - a.revision)[0];
  versionId.value = latest?.id || '';
  setForm(latest || res.data);
  return true;
}
async function chooseAssistant(event) {
  if (blocked.value || !discard()) {
    event.target.value = String(detail.value?.id || '');
    return;
  }
  await openAssistant(event.target.value);
}
async function openAssistant(id) {
  if (!id) {
    detail.value = null;
    versionId.value = '';
    templateId.value = '';
    setForm(blank());
    panel.value = 'canvas';
    return;
  }
  busy.value = true;
  error.value = '';
  try {
    await fetchDetail(id);
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
function viewVersion(event) {
  if (blocked.value || !discard()) {
    event.target.value = String(versionId.value);
    return;
  }
  versionId.value = event.target.value;
  setForm(version.value);
}
function applyTemplate() {
  if (!editable.value || !discard()) return;
  form.graph = clone(chosenTemplate.value.graph);
  form.name = chosenTemplate.value.name;
  form.description = chosenTemplate.value.description;
  selectedId.value = form.graph.nodes[0]?.id;
  notice.value = '已套用範本，請選擇模型、文件集或查詢工具後儲存。';
}
function parseDepartments(value) {
  return value.split(/[,，]/).map(s => Number(s.trim())).filter(n => Number.isInteger(n) && n > 0);
}
function setDepartments(value) {
  form.target_department_ids = parseDepartments(value);
}
async function saveCollectionPublication(collection) {
  if (blocked.value) return;
  if (!window.confirm(`確定將「${collection.name}」的文件查詢範圍改為${collection.publication_scope === 'public' ? '全院' : collection.publication_scope === 'departments' ? '指定單位' : '本單位'}？請先確認文件內容可供這些人員使用。`)) return;
  busy.value = true;
  error.value = '';
  try {
    await api.setCollectionPublication(collection.id, {
      scope: collection.publication_scope,
      target_department_ids: collection.publication_scope === 'departments' ? collection.target_department_ids || [] : []
    }, actor.mutationOptions());
    const current = (await api.getCatalog()).data;
    if (!actor.captureCatalog(current)) return;
    catalog.value = current;
    notice.value = '文件集的開放範圍已更新。';
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
async function saveTemplate(update) {
  if (blocked.value) return;
  if (update && !window.confirm('更新此範本後，之後套用會使用目前畫布。確定更新？')) return;
  busy.value = true;
  error.value = '';
  try {
    const body = {
      name: templateName.value.trim(),
      description: templateDescription.value,
      graph: clone(form.graph)
    };
    if (update) await api.updateTemplate(chosenTemplate.value.id, body, actor.mutationOptions());else await api.createTemplate(body, actor.mutationOptions());
    templates.value = (await api.listTemplates()).data.templates;
    notice.value = '範本已儲存，之後可由情境範本套用。';
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
async function save() {
  if (busy.value || !editable.value) return;
  error.value = '';
  busy.value = true;
  try {
    const body = clone(form);
    for (const node of body.graph.nodes) {
      if (node.kind === 'mcp') {
        const tool = catalog.value.mcp_tools?.find(tool => tool.id === node.config.tool_id);
        if (tool) node.config.arguments = initializeToolArguments(tool.input_schema || {}, node.config.arguments);
      }
    }
    body.collection_ids = [...new Set(body.graph.nodes.filter(n => n.kind === 'rag').flatMap(n => n.config.collection_ids || []))];
    const r = detail.value ? await api.saveVersion(detail.value.id, {
      ...body,
      expected_revision: detail.value.draft_revision
    }, actor.mutationOptions()) : await api.createAssistant(body, actor.mutationOptions());
    if (!await fetchDetail(r.data.id || detail.value.id)) return;
    assistants.value = (await api.listAssistants()).data.assistants;
    notice.value = '已儲存新版本；可先測試，再送主管審核。';
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
async function action(name, body) {
  if (blocked.value || dirty.value) return;
  busy.value = true;
  error.value = '';
  try {
    await api.assistantAction(detail.value.id, name, body, actor.mutationOptions());
    if (!await fetchDetail(detail.value.id)) return;
    assistants.value = (await api.listAssistants()).data.assistants;
    notice.value = {
      restore: '已另建回復草稿，請重新測試與送審。',
      submit: '已送主管審核。',
      review: '已記錄這個版本的審核結果。',
      publish: '已註冊為 Agent，仍需平台管理員核准後才能使用。',
      disable: '助手已停用。'
    }[name];
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
function disableAssistant() {
  if (blocked.value || dirty.value) { notice.value = '請先儲存或捨棄變更，再停用助手。'; return; }
  if (window.confirm('停用後同仁將無法使用這個助手。確定停用？')) action('disable', {});
}
async function compare() {
  busy.value = true;
  error.value = '';
  try {
    comparison.value = (await api.compareVersions(detail.value.id, compareLeft.value, compareRight.value)).data;
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
  }
}
async function test() {
  if (blocked.value || dirty.value || !version.value) return;
  busy.value = true;
  testing.value = true;
  error.value = '';
  testResult.value = null;
  try {
    testResult.value = maskTrace((await api.assistantAction(detail.value.id, 'test', {
      version_id: version.value.id,
      message: message.value
    }, actor.mutationOptions())).data);
  } catch (e) {
    fail(e);
  } finally {
    busy.value = false;
    testing.value = false;
  }
}
function addNode(kind, position, toolId) {
  if (!editable.value || busy.value) return;
  const n = makeNode(kind, form.graph.nodes.length, position);
  if (toolId) n.config.tool_id = toolId;
  form.graph.nodes.push(n);
  selectedId.value = n.id;
}
function edgeDrag(event, source, branch) {
  if (!editable.value || busy.value) {
    event.preventDefault();
    return;
  }
  event.dataTransfer.setData('application/anila-edge', JSON.stringify({
    source,
    branch
  }));
  event.dataTransfer.effectAllowed = 'link';
}
function edgeDrop(event, target) {
  if (!editable.value || busy.value) return;
  try {
    const data = JSON.parse(event.dataTransfer.getData('application/anila-edge'));
    connect(form.graph, data.source, target, data.branch);
    error.value = '';
    sourceId.value = '';
    targetId.value = '';
  } catch (e) {
    fail(e);
  }
}
function paletteDrag(event, kind, toolId) {
  event.dataTransfer.setData('application/anila-node', JSON.stringify({
    kind,
    toolId
  }));
  event.dataTransfer.effectAllowed = 'copy';
}
function dropNode(event) {
  if (!editable.value || busy.value) return;
  try {
    const raw = event.dataTransfer.getData('application/anila-node');
    if (!raw) return;
    const data = JSON.parse(raw);
    const rect = event.currentTarget.getBoundingClientRect();
    addNode(data.kind, {
      x: Math.max(0, event.clientX - rect.left + event.currentTarget.scrollLeft - 80),
      y: Math.max(0, event.clientY - rect.top + event.currentTarget.scrollTop - 20)
    }, data.toolId);
  } catch (e) {
    fail(e);
  }
}
let moving = null;
function startMove(e, node) {
  if (!editable.value || busy.value || e.button !== 0) return;
  selectedId.value = node.id;
  moving = {
    id: node.id,
    x: e.clientX,
    y: e.clientY,
    left: node.position.x,
    top: node.position.y
  };
  e.currentTarget.setPointerCapture?.(e.pointerId);
}
function moveNode(e, node) {
  if (moving?.id !== node.id) return;
  node.position.x = Math.max(0, moving.left + e.clientX - moving.x);
  node.position.y = Math.max(0, moving.top + e.clientY - moving.y);
}
function endMove() {
  moving = null;
}
function moveKey(e, node) {
  if (!editable.value || busy.value) return;
  const deltas = {
    ArrowLeft: [-20, 0],
    ArrowRight: [20, 0],
    ArrowUp: [0, -20],
    ArrowDown: [0, 20]
  };
  if (!deltas[e.key]) return;
  e.preventDefault();
  node.position.x = Math.max(0, node.position.x + deltas[e.key][0]);
  node.position.y = Math.max(0, node.position.y + deltas[e.key][1]);
}
function deleteNode() {
  if (window.confirm('移除此步驟與它的連線？儲存前可重新開啟已儲存版本。')) {
    removeNode(form.graph, selectedId.value);
    selectedId.value = '';
  }
}
function addEdge() {
  error.value = '';
  try {
    connect(form.graph, sourceId.value, targetId.value, branch.value);
    sourceId.value = '';
    targetId.value = '';
  } catch (e) {
    fail(e);
  }
}
function edgePoint(edge) {
  const a = form.graph.nodes.find(n => n.id === edge.source)?.position || {
    x: 0,
    y: 0
  };
  const b = form.graph.nodes.find(n => n.id === edge.target)?.position || {
    x: 0,
    y: 0
  };
  return {
    x: (a.x + b.x) / 2 + 90,
    y: (a.y + b.y) / 2 + 44
  };
}
function edgePath(edge) {
  const a = form.graph.nodes.find(n => n.id === edge.source)?.position || {
    x: 0,
    y: 0
  };
  const b = form.graph.nodes.find(n => n.id === edge.target)?.position || {
    x: 0,
    y: 0
  };
  return `M ${a.x + 180} ${a.y + 54} C ${a.x + 230} ${a.y + 54}, ${b.x - 50} ${b.y + 54}, ${b.x} ${b.y + 54}`;
}
function beforeUnload(e) {
  if (dirty.value) {
    e.preventDefault();
    e.returnValue = '';
  }
}
baseline.value = JSON.stringify(form);
onMounted(() => {
  load();
  window.addEventListener('beforeunload', beforeUnload);
});
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload));
onBeforeRouteLeave(() => discard());
</script>
<style src="../quickBuilder/builder.css"></style>
