<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="page-head__title">Agent</h1>
        <p class="page-head__sub">
          {{ authStore.isAdmin ? '審查並治理每個已註冊的 Agent' : '管理你的 Agent · 下載樣板 · 上線到 router' }}
        </p>
      </div>
      <div class="page-head__actions">
        <TermButton @click="handleDownloadTemplate" label="下載樣板" />
        <TermButton @click="handleDownloadPlatformCa" label="下載平台 CA" />
        <TermButton variant="primary" @click="openRegisterModal" label="註冊 Agent" />
      </div>
      <p class="cell-meta page-head__ca-hint">
        「下載平台 CA」會呼叫平台端點；若下載失敗，畫面會顯示錯誤提示。
      </p>
    </header>

    <div v-if="feedback.message" class="feedback" :class="feedback.type === 'error' ? 'is-err' : 'is-ok'">
      <span>{{ feedback.type === 'error' ? '!' : '✓' }}</span>
      <span>{{ feedback.message }}</span>
    </div>

    <TermBox title="開發者 · 指南" pad="md">
      <button type="button" class="guide-toggle" @click="showGuide = !showGuide">
        <span>{{ showGuide ? '▾' : '▸' }} 下載 anila-agent 樣板 · 用 FastAPI 包裝 · 註冊 · 等待審核</span>
        <span class="cell-meta">{{ showGuide ? '收合' : '展開' }}</span>
      </button>
      <div v-if="showGuide" class="guide">
        <p class="guide__lead">
          下載樣板、加工具、包 FastAPI、註冊名稱與 endpoint——
          <strong>不核發、不保管任何長效祕密</strong>。
          派工身分改為短效 JWT；驗簽接法見下方三級制（請核對樣板 zip 實際內容，勿假設已內建）。
          完整說明見
          <router-link to="/developer/guide" class="guide__link">開發者指南 →</router-link>
        </p>
        <ol class="guide__list">
          <li>
            <span class="guide__step">01</span>
            <div>
              <p><strong>install &amp; verify</strong> · click <em>download template</em> above, then <code>uv pip install -e '.[dev,pgvector]'</code>, copy <code>.env.example</code>, run <code>anila --prompt "ping"</code> to confirm the REPL works.</p>
            </div>
          </li>
          <li>
            <span class="guide__step">02</span>
            <div>
              <p><strong>add your tool</strong> · use the <code>@anila_tool</code> decorator (auto JSON schema from type hints + docstring):</p>
              <pre class="guide__code">from anila_agent.tools.base import anila_tool

@anila_tool(is_read_only=True, category="domain")
def employee_count(department: str) -&gt; int:
    """Count active employees.

    Args:
        department: Department name.
    """
    return _query_hr_db(department)</pre>
              <p>or list it in <code>configs/tools.yaml</code> under <code>builtin:</code>. Hook events (<code>pre_tool_use</code> / <code>post_tool_use</code> / <code>stop</code>) registered in the same yaml.</p>
            </div>
          </li>
          <li>
            <span class="guide__step">02b</span>
            <div>
              <p><strong>wrap in FastAPI</strong> · anila-agent is CLI/library, not a service. Add a thin wrapper exposing <code>/health</code> + <code>/v1/chat/completions</code> + <code>/v1/models</code> bridging <code>AnilaRunner</code> ↔ OpenAI-compat SSE. Boilerplate on the dev guide page.</p>
            </div>
          </li>
          <li>
            <span class="guide__step">03</span>
            <div>
              <p>your agent must expose these s2s endpoints:</p>
              <table class="term-table guide__table">
                <thead><tr><th style="width: 70px">method</th><th>path</th><th>purpose</th></tr></thead>
                <tbody>
                  <tr><td><code>GET</code></td><td><code>/health</code></td><td>discovery + health probe (public)</td></tr>
                  <tr><td><code>GET</code></td><td><code>/v1/models</code></td><td>list available model ids</td></tr>
                  <tr><td><code>POST</code></td><td><code>/v1/chat/completions</code></td><td>main inference, openai-compat</td></tr>
                </tbody>
              </table>
            </div>
          </li>
          <li>
            <span class="guide__step">04</span>
            <div>
              <p><strong>register here</strong> · status starts as <TermBadge variant="warn">pending</TermBadge> · admin review unblocks router auto-discovery.</p>
            </div>
          </li>
        </ol>
        <AgentGuardPanel :csp-url="cspUrl" />
      </div>
    </TermBox>

    <div class="kpi-row">
      <TermStat label="Agent · 總數" :value="agents.length" />
      <TermStat label="待審核" :value="pendingCount" :tone="pendingCount ? 'warn' : 'default'" />
      <TermStat label="已核准" :value="approvedCount" tone="accent" />
      <TermStat label="健康" :value="healthyCount" />
    </div>

    <TermBox title="篩選" pad="sm">
      <div class="filters">
        <TermField label="搜尋">
          <input v-model="filters.query" class="term-input" placeholder="名稱 · 描述 · endpoint" />
        </TermField>
        <TermField label="審批">
          <select v-model="filters.approval" class="term-select">
            <option value="all">全部</option>
            <option v-for="s in approvalFilterOptions" :key="s" :value="s">{{ approvalLabel(s) }}</option>
          </select>
        </TermField>
        <TermField label="健康">
          <select v-model="filters.health" class="term-select">
            <option value="all">全部</option>
            <option value="healthy">健康</option>
            <option value="unhealthy">異常</option>
            <option value="unknown">未知</option>
          </select>
        </TermField>
        <TermField label="排序">
          <select v-model="filters.sort" class="term-select">
            <option value="newest">最新</option>
            <option value="oldest">最舊</option>
            <option value="name">名稱 a→z</option>
            <option value="approval">待審核優先</option>
          </select>
        </TermField>
      </div>
    </TermBox>

    <div v-if="authStore.isAdmin && pendingCount > 0" class="banner">
      <span>{{ pendingCount }} 個 Agent 待審核 · 用篩選器分流佇列。</span>
    </div>

    <TermBox :title="`Agent · ${filteredAgents.length}/${agents.length}`" pad="none" flush>
      <div v-if="loading" class="loading">載入 Agent 中…</div>
      <div v-else-if="filteredAgents.length === 0" style="padding: var(--gap-6);">
        <TermEmpty :message="agents.length === 0 ? '尚無 Agent · 下載樣板開始' : '無符合篩選的 Agent'" />
      </div>
      <table v-else class="term-table">
        <thead>
          <tr>
            <th>名稱</th>
            <th>端點</th>
            <th style="width: 140px">型別 / 版本</th>
            <th style="width: 100px">健康</th>
            <th style="width: 120px">審批狀態</th>
            <th style="width: 100px">分類等級</th>
            <th style="width: 13%">建立時間</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="agent in filteredAgents" :key="agent.id">
            <td>
              <div class="cell-strong">{{ agent.name }}</div>
              <div class="cell-meta">id #{{ agent.id }}</div>
            </td>
            <td>
              <code class="cell-url" :title="agent.endpoint_url">{{ agent.endpoint_url }}</code>
            </td>
            <td>
              <div class="cell-strong" style="font-family: var(--font-mono); font-size: var(--t-2xs);">{{ agent.runtime_type || '—' }}</div>
              <div class="cell-meta">{{ agent.agent_version || '—' }}</div>
            </td>
            <td><TermBadge :variant="healthVariant(agent.health_status)" dot>{{ agent.health_status }}</TermBadge></td>
            <td><TermBadge :variant="approvalVariant(agent.approval_status)" dot>{{ approvalLabel(agent.approval_status) }}</TermBadge></td>
            <td>
              <TermBadge :variant="levelBadgeVariant(agent.default_classification_level)">
                {{ agent.default_classification_level || '無機密' }}
              </TermBadge>
            </td>
            <td class="cell-meta tnum">{{ formatDate(agent.created_at) }}</td>
            <td>
              <div class="row-actions">
                <button class="term-action" @click="openDetailModal(agent)">詳情</button>
                <span class="row-actions__sep">·</span>
                <button v-if="canEditAgent(agent)" class="term-action" @click="openEditModal(agent)">編輯</button>
                <span v-if="canEditAgent(agent) && authStore.isAdmin" class="row-actions__sep">·</span>
                <button v-if="authStore.isAdmin" class="term-action" :disabled="healthCheckingId === agent.id" @click="handleHealthCheck(agent)">
                  {{ healthCheckingId === agent.id ? '探測中…' : '探測' }}
                </button>
                <template v-if="authStore.isAdmin && isPendingReview(agent.approval_status)">
                  <span class="row-actions__sep">·</span>
                  <button
                    class="term-action"
                    :disabled="!isApprovable(agent.approval_status)"
                    @click="handleApprove(agent)"
                  >核准</button>
                  <span class="row-actions__sep">·</span>
                  <button class="term-action term-action--danger" @click="openRejectModal(agent)">停用</button>
                </template>
                <template v-if="authStore.isAdmin">
                  <span class="row-actions__sep">·</span>
                  <button class="term-action term-action--danger" :disabled="deletingId === agent.id" @click="handleDeleteAgent(agent)">
                    {{ deletingId === agent.id ? '刪除中…' : '刪除' }}
                  </button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <!-- Register: single step — name / endpoint / base model（不發任何祕密） -->
    <TermModal
      :visible="showRegisterModal"
      title="註冊 · Agent"
      width="640px"
      @close="finishRegister"
    >
      <div class="form-grid">
        <p class="cell-meta">
          平台派工時會現簽 5 分鐘憑條；你不需要領取或保管任何長效祕密。
        </p>
        <TermField label="名稱" hint="不可變更的識別碼 · 英數字與連字號" :error="formErrors.name">
          <input v-model="form.name" class="term-input" placeholder="hr-policy-agent" />
        </TermField>
        <TermField label="端點 URL" :error="formErrors.endpoint_url">
          <input v-model="form.endpoint_url" class="term-input" placeholder="http://host:port" />
        </TermField>
        <TermField label="router 說明" hint="≥ 24 字 · 用白話描述此 agent 解決什麼" :error="formErrors.description_for_router">
          <textarea v-model="form.description_for_router" rows="3" class="term-textarea" />
        </TermField>
        <div class="form-row-2">
          <TermField label="API 版本">
            <input v-model="form.api_version" class="term-input" placeholder="v1" />
          </TermField>
          <TermField label="基礎模型" :error="formErrors.base_model_id" hint="用量歸屬對象">
            <select v-model.number="form.base_model_id" class="term-select">
              <option :value="null" disabled>— 選擇基礎模型 —</option>
              <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
                {{ m.display_name }} ({{ m.name }} · {{ m.model_type }})
              </option>
            </select>
          </TermField>
        </div>
        <TermField label="RAG 知識庫（選填）" hint="可綁定多個；非 RAG agent 不勾選。任務內回呼沿用派工憑條">
          <div v-if="collections.length" class="collection-checks">
            <label v-for="c in collections" :key="c.id" class="collection-check">
              <input type="checkbox" :value="c.id" v-model="form.collection_ids" />
              <span>{{ c.name }} (#{{ c.id }})</span>
            </label>
          </div>
          <p v-else class="collection-checks__empty">尚無可選知識庫</p>
        </TermField>

        <TermSection title="治理設定 · governance" />
        <TermField
          label="預設分類等級"
          hint="此 agent 回覆的對話會以所選等級列管記錄；營業秘密起的讀取與外流會落稽核，密與機密另會阻擋複製、匯出與分享。對已列管的對話，效果不可自行逆轉。"
        >
          <select v-model="form.default_classification_level" class="term-select">
            <option v-for="lv in CLASSIFICATION_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
          </select>
        </TermField>
        <TermField label="runtime 型別" :hint="runtimeTypeHint">
          <select v-model="form.runtime_type" class="term-select">
            <option v-for="o in RUNTIME_TYPE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </TermField>
        <TermField label="版本" hint="agent 版本字串，例如 1.0.0（選填）">
          <input v-model="form.agent_version" class="term-input" placeholder="1.0.0" />
        </TermField>

        <TermSection title="起飛前檢查" />
        <ul class="check">
          <li :class="form.name ? 'is-ok' : 'is-pending'">{{ form.name ? '●' : '○' }} agent 名稱已設定</li>
          <li :class="/^https?:\/\//.test(form.endpoint_url) ? 'is-ok' : 'is-pending'">{{ /^https?:\/\//.test(form.endpoint_url) ? '●' : '○' }} endpoint 為 http(s) URL</li>
          <li :class="form.description_for_router.trim().length >= 24 ? 'is-ok' : 'is-pending'">{{ form.description_for_router.trim().length >= 24 ? '●' : '○' }} 說明 ≥ 24 字</li>
          <li :class="form.base_model_id ? 'is-ok' : 'is-pending'">{{ form.base_model_id ? '●' : '○' }} 已選基礎模型</li>
          <li class="is-pending">○ 已實作 <code>GET /health</code> + <code>POST /v1/chat/completions</code>（手動確認）</li>
        </ul>
      </div>

      <template #footer>
        <TermButton variant="ghost" @click="finishRegister" label="取消" />
        <TermButton variant="primary" :loading="registering" :disabled="registering" :label="registering ? '送出中' : '註冊'" @click="handleRegister" />
      </template>
    </TermModal>

    <!-- Edit modal ------------------------------------------------- -->
    <TermModal :visible="showEditModal" title="編輯 · Agent" width="640px" @close="closeEditModal">
      <div class="form-grid" v-if="editTarget">
        <TermField label="名稱" hint="不可變更">
          <input :value="editTarget.name" class="term-input" disabled />
        </TermField>
        <TermField label="端點 URL">
          <input v-model="editForm.endpoint_url" class="term-input" />
        </TermField>
        <TermField label="router 說明" hint="router 依此派送 — 請精確">
          <textarea v-model="editForm.description_for_router" rows="4" class="term-textarea" />
        </TermField>
        <div class="form-row-2">
          <TermField label="API 版本">
            <input v-model="editForm.api_version" class="term-input" />
          </TermField>
          <TermField label="基礎模型">
            <select v-model.number="editForm.base_model_id" class="term-select">
              <option :value="null" disabled>— 選擇基礎模型 —</option>
              <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
                {{ m.display_name }} ({{ m.name }} · {{ m.model_type }})
              </option>
            </select>
          </TermField>
        </div>
        <TermField
          label="預設分類等級"
          hint="此 agent 回覆的對話會以所選等級列管記錄；營業秘密起的讀取與外流會落稽核，密與機密另會阻擋複製、匯出與分享。對已列管的對話，效果不可自行逆轉。"
        >
          <select v-model="editForm.default_classification_level" class="term-select">
            <option v-for="lv in CLASSIFICATION_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
          </select>
        </TermField>
        <TermField label="RAG 知識庫（選填）" hint="可綁定多個；目前已綁定的會預先勾選 · 全部取消＝解除綁定">
          <div v-if="collections.length" class="collection-checks">
            <label v-for="c in collections" :key="c.id" class="collection-check">
              <input type="checkbox" :value="c.id" v-model="editForm.collection_ids" />
              <span>{{ c.name }} (#{{ c.id }})</span>
            </label>
          </div>
          <p v-else class="collection-checks__empty">尚無可選知識庫</p>
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeEditModal" label="取消" />
        <TermButton variant="primary" :disabled="editing" :loading="editing" :label="editing ? '儲存中' : '儲存'" @click="handleUpdateAgent" />
      </template>
    </TermModal>

    <!-- Detail drawer (modal-style) ------------------------------- -->
    <TermModal :visible="showDetailModal" :title="detailAgent ? `詳情 · ${detailAgent.name}` : '詳情'" width="720px" @close="closeDetailModal">
      <div v-if="detailAgent" class="detail">
        <TermSection title="總覽" />
        <dl class="detail__list">
          <div><dt>端點</dt><dd><code>{{ detailAgent.endpoint_url }}</code></dd></div>
          <div><dt>API 版本</dt><dd>{{ detailAgent.api_version || 'v1' }}</dd></div>
          <div><dt>runtime 型別</dt><dd><code>{{ detailAgent.runtime_type || '—' }}</code></dd></div>
          <div><dt>版本</dt><dd>{{ detailAgent.agent_version || '—' }}</dd></div>
          <div><dt>健康</dt><dd>{{ detailAgent.health_status }}</dd></div>
          <div>
            <dt>審批狀態</dt>
            <dd><TermBadge :variant="approvalVariant(detailAgent.approval_status)" dot>{{ approvalLabel(detailAgent.approval_status) }}</TermBadge></dd>
          </div>
          <div><dt>建立時間</dt><dd class="tnum">{{ formatDate(detailAgent.created_at) }}</dd></div>
          <div><dt>擁有者</dt><dd>{{ ownerDisplay(detailAgent) }}</dd></div>
          <div><dt>基礎模型</dt><dd>{{ detailAgent.base_model_id || '—' }}</dd></div>
          <div>
            <dt>綁定知識庫</dt>
            <dd>
              <template v-if="(detailAgent.bound_collection_ids || []).length">
                <code v-for="cid in detailAgent.bound_collection_ids" :key="cid" style="margin-right: 0.4rem;">#{{ cid }}</code>
              </template>
              <template v-else>—</template>
            </dd>
          </div>
          <div>
            <dt>預設分類等級</dt>
            <dd>
              <div style="display: flex; flex-wrap: wrap; align-items: center; gap: 8px;">
                <TermBadge :variant="levelBadgeVariant(detailAgent.default_classification_level)" dot>
                  {{ detailAgent.default_classification_level || '無機密' }}
                </TermBadge>
                <template v-if="canEditAgent(detailAgent)">
                  <select
                    :key="`cls-${detailAgent.id}-${detailAgent.default_classification_level}-${classificationSelectEpoch}`"
                    class="term-select"
                    style="width: auto; min-width: 8rem;"
                    :value="detailAgent.default_classification_level || '無機密'"
                    :disabled="classificationBusyId === detailAgent.id"
                    @change="handleSetClassification(detailAgent, $event.target.value)"
                  >
                    <option v-for="lv in CLASSIFICATION_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
                  </select>
                  <span v-if="classificationBusyId === detailAgent.id" class="cell-meta">更新中…</span>
                </template>
              </div>
              <p class="cell-meta" style="margin: 6px 0 0;">
                此 agent 回覆的對話會以所選等級列管記錄；營業秘密起的讀取與外流會落稽核，密與機密另會阻擋複製、匯出與分享。對已列管的對話，效果不可自行逆轉。
              </p>
            </dd>
          </div>
        </dl>

        <TermSection title="router 說明" />
        <p class="detail__desc">{{ detailAgent.description_for_router || '—' }}</p>

        <!-- OE-1 — 核准不需診斷。「測試連線」已接 POST …/test-connection（派工 JWT）；
             核准仍不依賴探測結果。 -->
        <TermSection title="測試連線" />
        <p class="cell-meta">
          以真實派工 JWT 探測 <code>/v1/chat/completions</code>。三個事實分開顯示——
          「無法判定」不是失敗，也絕不是通過。核准不依賴此探測。
        </p>
        <div class="row-actions" style="margin: 8px 0;">
          <TermButton
            size="xs"
            variant="primary"
            :disabled="testingConnectionId === detailAgent.id"
            :label="testingConnectionId === detailAgent.id ? '測試中…' : '測試連線'"
            @click="handleTestConnection(detailAgent)"
          />
        </div>
        <dl v-if="testConnectionView" class="probe-facts">
          <div v-for="fact in testConnectionView.facts" :key="fact.key" class="probe-facts__row">
            <dt>{{ fact.label }}</dt>
            <dd :class="`probe-facts__val is-${fact.tone}`">{{ fact.display }}</dd>
          </div>
          <div v-if="testConnectionView.statusCode != null" class="probe-facts__row">
            <dt>HTTP</dt>
            <dd class="tnum">{{ testConnectionView.statusCode }}</dd>
          </div>
          <div class="probe-facts__row probe-facts__row--detail">
            <dt>說明</dt>
            <dd class="probe-facts__detail">{{ testConnectionView.detail }}</dd>
          </div>
        </dl>

        <template v-if="authStore.isAdmin && isPendingReview(detailAgent.approval_status)">
          <TermSection title="核准" />
          <p class="cell-meta">
            管理員核准後即可被 router 發現。端點健康用列上的「探測」；
            派工 JWT 驗簽用上方「測試連線」與下方接入驗簽。核准不依賴連線探測結果。
          </p>
          <div class="row-actions" style="margin: 8px 0;">
            <TermButton
              size="xs" variant="primary" :disabled="!detailIsApprovable"
              label="核准" @click="handleApprove(detailAgent)"
            />
            <TermButton size="xs" variant="ghost" label="停用" @click="openRejectModal(detailAgent)" />
          </div>
        </template>

        <TermSection title="狀態時間軸" />
        <ol class="timeline">
          <li v-for="entry in buildStatusHistory(detailAgent)" :key="entry.label + entry.timestamp">
            <span class="timeline__dot" />
            <div>
              <p class="timeline__label">{{ entry.label }}</p>
              <p class="cell-meta tnum">{{ entry.timestamp }}</p>
              <p v-if="entry.detail" class="timeline__detail">{{ entry.detail }}</p>
            </div>
          </li>
        </ol>

        <TermSection title="接入驗簽" />
        <AgentGuardPanel :csp-url="cspUrl" />
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeDetailModal" label="關閉" />
      </template>
    </TermModal>

    <!-- Reject modal ---------------------------------------------- -->
    <TermModal :visible="!!rejectTarget" title="駁回 · Agent" width="440px" @close="closeRejectModal">
      <p class="cell-meta">留下原因讓開發者可以修改。</p>
      <TermField label="原因">
        <textarea v-model="rejectReason" rows="4" class="term-textarea" placeholder="例：缺 /health 端點 · 說明太短" />
      </TermField>
      <template #footer>
        <TermButton variant="ghost" @click="closeRejectModal" label="取消" />
        <TermButton variant="danger" @click="handleReject" label="確認駁回" />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import {
  approveAgent, deleteAgent, downloadPlatformCa, downloadTemplate, getAgent, listMyAgents,
  registerAgent, rejectAgent, setAgentClassification,
  testAgentConnection, triggerAgentHealthCheck, updateAgent,
} from '../api/agents'
import {
  APPROVAL_STATUSES, approvalLabel, approvalVariant, isApprovable, isPendingReview,
} from '../utils/approvalStatus'
import { formatTestConnectionFacts } from '../utils/testConnectionFacts'
import { listCollections } from '../api/ingestionCollections'
import { listModels } from '../api/models'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat, TermSection } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import AgentGuardPanel from '../components/agents/AgentGuardPanel.vue'

// CSP base URL the snippets should reference. Derived from the
// browser origin so the dev's copy-pasted code targets whatever host
// they're actually viewing — `localhost:5173` in dev, prod hostname
// in prod. Override via VITE_CSP_BASE_URL when the API lives on a
// different origin from the SPA.
const cspUrl = import.meta.env?.VITE_CSP_BASE_URL || (typeof window !== 'undefined' ? window.location.origin : '')

const { confirm, toast } = useDialog()
const authStore = useAuthStore()

const agents = ref([])
const loading = ref(false)
const showGuide = ref(false)
const showRegisterModal = ref(false)
const showDetailModal = ref(false)
const detailAgent = ref(null)
const registering = ref(false)
const rejectTarget = ref(null)
const rejectReason = ref('')
const classificationBusyId = ref(null)
const classificationSelectEpoch = ref(0)
const deletingId = ref(null)
const healthCheckingId = ref(null)
// P2.1 — 詳情「測試連線」：testingConnectionId 只鎖正在測的那顆；
// testConnectionResult 快取最近一次三事實（null 顯示「無法判定」，不可當成功）。
const testingConnectionId = ref(null)
const testConnectionResult = ref(null)
const testConnectionView = computed(() =>
  testConnectionResult.value ? formatTestConnectionFacts(testConnectionResult.value) : null
)
const showEditModal = ref(false)
const editTarget = ref(null)
const editing = ref(false)
const editFormError = ref('')
const editForm = ref({
  endpoint_url: '', description_for_router: '', api_version: '',
  base_model_id: null, default_classification_level: '無機密',
  collection_ids: [],
})
const feedback = ref({ type: 'success', message: '' })

const filters = ref({ query: '', approval: 'all', health: 'all', sort: 'newest' })
const form = ref({
  name: '', endpoint_url: '', description_for_router: '', api_version: 'v1',
  base_model_id: null, collection_ids: [],
  runtime_type: 'openai_compatible_agent', agent_version: '',
  default_classification_level: '無機密',
})
const formErrors = ref({})

const approvalFilterOptions = APPROVAL_STATUSES

const RUNTIME_TYPE_OPTIONS = [
  { value: 'anila_agent', label: 'anila_agent', hint: '官方 anila-agent 樣板（openai-agents runtime）' },
  { value: 'langchain', label: 'langchain', hint: 'LangChain / LangGraph 服務' },
  { value: 'openwebui_pipe_compatible', label: 'openwebui_pipe_compatible', hint: '相容 OpenWebUI pipe 介面的既有 agent' },
  { value: 'openai_compatible_agent', label: 'openai_compatible_agent', hint: 'OpenAI 相容 /v1/chat/completions（預設）' },
  { value: 'custom_http', label: 'custom_http', hint: '自訂 HTTP 介面（需自行對齊契約）' },
]

const CLASSIFICATION_LEVELS = ['無機密', '營業秘密', '密', '機密']

function levelBadgeVariant(level) {
  if (level === '機密' || level === '密') return 'danger'
  if (level === '營業秘密') return 'accent'
  return ''
}

const runtimeTypeHint = computed(() =>
  RUNTIME_TYPE_OPTIONS.find(o => o.value === form.value.runtime_type)?.hint || '')

const collections = ref([])
const availableModels = ref([])
const baseModelOptions = computed(() =>
  availableModels.value.filter(m => m.is_active && (m.model_type === 'llm' || m.model_type === 'vlm'))
)

const pendingCount = computed(() => agents.value.filter(a => isPendingReview(a.approval_status)).length)
const approvedCount = computed(() => agents.value.filter(a => a.approval_status === 'approved').length)
const healthyCount = computed(() => agents.value.filter(a => a.health_status === 'healthy' || a.health_status === 'online').length)

function ownerDisplay(agent) {
  if (!agent) return '—'
  if (agent.owner_username) return `${agent.owner_username}${agent.owner_user_id ? ` (#${agent.owner_user_id})` : ''}`
  return agent.owner_user_id ? `#${agent.owner_user_id}` : '—'
}

const filteredAgents = computed(() => {
  const query = filters.value.query.trim().toLowerCase()
  let next = agents.value.filter(a => {
    if (filters.value.approval !== 'all' && a.approval_status !== filters.value.approval) return false
    if (filters.value.health !== 'all' && a.health_status !== filters.value.health) return false
    if (!query) return true
    return [a.name, a.endpoint_url, a.description_for_router, a.id].filter(Boolean).join(' ').toLowerCase().includes(query)
  })
  next = [...next].sort((l, r) => {
    if (filters.value.sort === 'name') return (l.name || '').localeCompare(r.name || '')
    if (filters.value.sort === 'oldest') return new Date(l.created_at) - new Date(r.created_at)
    if (filters.value.sort === 'approval') return (isPendingReview(l.approval_status) ? -1 : 1) - (isPendingReview(r.approval_status) ? -1 : 1)
    return new Date(r.created_at) - new Date(l.created_at)
  })
  return next
})

function setFeedback(type, message) {
  feedback.value = { type, message }
  if (message) setTimeout(() => { feedback.value = { type: 'success', message: '' } }, 5000)
}

function resetForm() {
  form.value = {
    name: '', endpoint_url: '', description_for_router: '', api_version: 'v1',
    base_model_id: null, collection_ids: [],
    runtime_type: 'openai_compatible_agent', agent_version: '',
    default_classification_level: '無機密',
  }
  formErrors.value = {}
}

function validateForm() {
  const errors = {}
  if (!form.value.name.trim()) errors.name = '請輸入 Agent 名稱'
  if (!/^https?:\/\//.test(form.value.endpoint_url.trim())) {
    errors.endpoint_url = '網址要以 http:// 或 https:// 開頭，例如 http://172.16.120.153:8043'
  }
  if (form.value.description_for_router.trim().length < 24) {
    errors.description_for_router = '請輸入至少 24 個字元的用途說明'
  }
  if (!form.value.base_model_id) {
    errors.base_model_id = '請選擇基礎模型，才能正確歸屬用量'
  }
  formErrors.value = errors
  return Object.keys(errors).length === 0
}

async function fetchAgents() {
  loading.value = true
  try { const { data } = await listMyAgents(); agents.value = data }
  catch (e) { setFeedback('error', e.response?.data?.detail || '載入 Agent 失敗') }
  finally { loading.value = false }
}
async function fetchAvailableModels() {
  try { const { data } = await listModels(); availableModels.value = data }
  catch { availableModels.value = [] }
}
onMounted(async () => { await Promise.all([fetchAgents(), fetchAvailableModels()]) })

async function openRegisterModal() {
  resetForm()
  showRegisterModal.value = true
  // Load the caller's collections for the optional RAG bind dropdown. Best
  // effort — a failure just leaves the dropdown empty (binding stays optional).
  try {
    const { data } = await listCollections()
    collections.value = Array.isArray(data) ? data : (data?.items || [])
  } catch { collections.value = [] }
}

async function openDetailModal(agent) {
  testConnectionResult.value = null
  try { const { data } = await getAgent(agent.id); detailAgent.value = data }
  catch { detailAgent.value = agent }
  showDetailModal.value = true
}

const detailIsApprovable = computed(() =>
  isApprovable(detailAgent.value?.approval_status))

function closeDetailModal() {
  showDetailModal.value = false
  detailAgent.value = null
  testConnectionResult.value = null
}

function openRejectModal(agent) { rejectTarget.value = agent; rejectReason.value = '' }
function closeRejectModal() { rejectTarget.value = null; rejectReason.value = '' }

async function handleRegister() {
  if (!validateForm()) return
  registering.value = true
  try {
    // ⚠ 這裡刻意「逐欄列出」，不要寫成 `...form.value`。
    // POST /api/agents/register 的 AgentRegisterRequest 是 extra="forbid"：
    // 未宣告的欄位會 422，不再被安靜丟掉。用展開的話，任何人日後在 form
    // 上多加一個純 UI 用的暫存欄位（例如折疊狀態、草稿旗標），就會讓全院
    // 每一次註冊都 422，而他不會知道原因。要送新欄位 → 先在後端宣告。
    await registerAgent({
      name: form.value.name.trim(),
      endpoint_url: form.value.endpoint_url.trim(),
      description_for_router: form.value.description_for_router.trim(),
      api_version: form.value.api_version,
      base_model_id: form.value.base_model_id,
      collection_ids: Array.isArray(form.value.collection_ids)
        ? [...form.value.collection_ids]
        : [],
      runtime_type: form.value.runtime_type || 'openai_compatible_agent',
      agent_version: form.value.agent_version.trim() || null,
      default_classification_level: form.value.default_classification_level || '無機密',
    })
    setFeedback('success', 'Agent 已註冊 · 待管理員指派使用者後即可使用（無需保管任何祕密）')
    finishRegister()
    await fetchAgents()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'register failed') }
  finally { registering.value = false }
}

function finishRegister() {
  showRegisterModal.value = false
}

function canEditAgent(agent) {
  if (!agent) return false
  if (authStore.isAdmin) return true
  return agent.owner_user_id === authStore.user?.id
}
function openEditModal(agent) {
  editTarget.value = agent
  const bound = Array.isArray(agent.bound_collection_ids)
    ? [...agent.bound_collection_ids]
    : (agent.bound_collection_id ? [agent.bound_collection_id] : [])
  editForm.value = {
    endpoint_url: agent.endpoint_url || '',
    description_for_router: agent.description_for_router || '',
    api_version: agent.api_version || 'v1',
    base_model_id: agent.base_model_id ?? null,
    default_classification_level: agent.default_classification_level || '無機密',
    collection_ids: bound,
  }
  editFormError.value = ''
  showEditModal.value = true
}
function closeEditModal() { showEditModal.value = false; editTarget.value = null; editFormError.value = '' }

async function handleUpdateAgent() {
  if (!editTarget.value || editing.value) return
  if (!editForm.value.base_model_id) { editFormError.value = 'base model required'; return }
  const patch = {
    endpoint_url: editForm.value.endpoint_url.trim() || null,
    description_for_router: (editForm.value.description_for_router || '').trim() || null,
    api_version: (editForm.value.api_version || '').trim() || null,
    base_model_id: editForm.value.base_model_id,
    default_classification_level: editForm.value.default_classification_level || '無機密',
    collection_ids: Array.isArray(editForm.value.collection_ids)
      ? [...editForm.value.collection_ids]
      : [],
  }
  editing.value = true
  try {
    const { data } = await updateAgent(editTarget.value.id, patch)
    const idx = agents.value.findIndex(a => a.id === data.id)
    if (idx >= 0) agents.value[idx] = data
    if (detailAgent.value && detailAgent.value.id === data.id) detailAgent.value = data
    setFeedback('success', `已更新「${data.name}」`)
    closeEditModal()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'update failed') }
  finally { editing.value = false }
}

async function handleApprove(agent) {
  try {
    await approveAgent(agent.id)
    setFeedback('success', `已核准「${agent.name}」`)
    await fetchAgents()
    syncDetailFromList(agent.id)
  }
  catch (e) { setFeedback('error', e.response?.data?.detail || '核准失敗') }
}

// 核准／駁回後把最新狀態同步回開啟中的 detail modal（若操作對象就是它）。
function syncDetailFromList(id) {
  if (!detailAgent.value || detailAgent.value.id !== id) return
  const fresh = agents.value.find(a => a.id === id)
  if (fresh) detailAgent.value = { ...detailAgent.value, ...fresh }
}

async function handleSetClassification(agent, level) {
  if (!agent || classificationBusyId.value === agent.id) return
  const next = level || '無機密'
  if (next === (agent.default_classification_level || '無機密')) return
  // Confirm for every level above 無機密: 營業秘密 already one-way-locks
  // conversations and forces audited reads/outbound; 密/機密 also block copy/export/share.
  if (next !== '無機密' && !(await confirm({
    message: (
      `將「${agent.name}」設為「${next}」？此 agent 之後回覆的對話會以該等級列管，`
      + `且對已列管的對話效果不可逆（無法自行降級）。`
      + (next === '營業秘密'
        ? '營業秘密起的讀取與外流動作都會落稽核。'
        : '密與機密會阻擋複製、匯出與分享，讀取與外流亦會落稽核。')
    ),
    confirmText: '確認',
    danger: true,
  }))) {
    // Remount the select so :value snaps back to the stored level.
    classificationSelectEpoch.value += 1
    return
  }
  classificationBusyId.value = agent.id
  try {
    const { data } = await setAgentClassification(agent.id, next)
    const applied = data?.default_classification_level || next
    const controlled = Boolean(data?.requires_encryption)
    const idx = agents.value.findIndex(a => a.id === agent.id)
    if (idx !== -1) {
      agents.value[idx] = {
        ...agents.value[idx],
        default_classification_level: applied,
        requires_encryption: controlled,
      }
    }
    if (detailAgent.value && detailAgent.value.id === agent.id) {
      detailAgent.value = {
        ...detailAgent.value,
        default_classification_level: applied,
        requires_encryption: controlled,
      }
    }
    setFeedback('success', `已將「${agent.name}」預設分類等級設為「${applied}」`)
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || '分類等級更新失敗')
  } finally {
    classificationBusyId.value = null
  }
}

async function handleHealthCheck(agent) {
  if (!agent || healthCheckingId.value === agent.id) return
  healthCheckingId.value = agent.id
  try {
    const { data } = await triggerAgentHealthCheck(agent.id)
    const idx = agents.value.findIndex(a => a.id === agent.id)
    if (idx >= 0) agents.value[idx] = { ...agents.value[idx], health_status: data.status }
    setFeedback(data.status === 'healthy' ? 'success' : 'error',
      `「${agent.name}」健康：${data.status}${data.detail ? ` — ${data.detail}` : ''}`)
  } catch (e) { setFeedback('error', e.response?.data?.detail || `「${agent.name}」健康探測失敗`) }
  finally { healthCheckingId.value = null }
}

// P2.1 — POST …/test-connection。結果以三事實渲染；detail 原樣顯示，不自拼 URL。
async function handleTestConnection(agent) {
  if (!agent || testingConnectionId.value === agent.id) return
  const probeId = agent.id
  testingConnectionId.value = probeId
  try {
    const { data } = await testAgentConnection(probeId)
    // 探測可逾秒；期間若已關詳情／換另一支 agent，勿寫入或 toast（錯 agent 假綠）。
    if (detailAgent.value?.id !== probeId) return
    testConnectionResult.value = data
    const view = formatTestConnectionFacts(data)
    const summary = view.facts.map((f) => `${f.label}：${f.display}`).join(' · ')
    const allOk = view.facts.every((f) => f.tone === 'ok')
    toast(
      `測試連線 → ${summary}${data.status_code != null ? `（HTTP ${data.status_code}）` : ''}`,
      { tone: allOk ? 'success' : 'warn' },
    )
  } catch (e) {
    if (detailAgent.value?.id !== probeId) return
    testConnectionResult.value = null
    const detail = e.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : (detail?.message || '測試連線失敗')
    toast(msg, { tone: 'error' })
  } finally {
    if (testingConnectionId.value === probeId) testingConnectionId.value = null
  }
}

async function handleDeleteAgent(agent) {
  if (!agent || deletingId.value === agent.id) return
  if (!(await confirm({ message: `刪除「${agent.name}」？不可復原 · 使用中的引用會失效。`, confirmText: '刪除', danger: true }))) return
  deletingId.value = agent.id
  try {
    await deleteAgent(agent.id)
    agents.value = agents.value.filter(a => a.id !== agent.id)
    if (detailAgent.value && detailAgent.value.id === agent.id) { showDetailModal.value = false; detailAgent.value = null }
    setFeedback('success', `已刪除「${agent.name}」`)
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'delete failed') }
  finally { deletingId.value = null }
}

async function handleReject() {
  try {
    const rejectedId = rejectTarget.value.id
    await rejectAgent(rejectedId, rejectReason.value.trim())
    setFeedback('success', `已駁回「${rejectTarget.value.name}」`)
    closeRejectModal()
    await fetchAgents()
    syncDetailFromList(rejectedId)
  } catch (e) { setFeedback('error', e.response?.data?.detail || '駁回失敗') }
}

async function handleDownloadTemplate() {
  try {
    const { data } = await downloadTemplate()
    const url = URL.createObjectURL(new Blob([data]))
    const link = document.createElement('a')
    link.href = url
    link.download = 'anila-agent.zip'
    link.click()
    URL.revokeObjectURL(url)
    setFeedback('success', '樣板已下載')
  } catch (e) { setFeedback('error', e.response?.data?.detail || '下載失敗') }
}

async function handleDownloadPlatformCa() {
  // Public CSPKI CA bundle used by agents for JWKS over https.
  try {
    const { data } = await downloadPlatformCa()
    const url = URL.createObjectURL(new Blob([data], { type: 'application/x-pem-file' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'anila-platform-ca.pem'
    link.click()
    URL.revokeObjectURL(url)
    setFeedback('success', '平台 CA 已下載')
  } catch (e) {
    const status = e.response?.status
    const detail = e.response?.data?.detail
    if (status === 404) {
      setFeedback('error', '平台 CA 下載失敗，請稍後再試')
    } else {
      setFeedback('error', detail || '下載平台 CA 失敗')
    }
  }
}

function healthVariant(s) {
  if (s === 'healthy' || s === 'online') return 'ok'
  if (s === 'unhealthy' || s === 'offline') return 'danger'
  return ''
}
function formatDate(s) { return s ? new Date(s).toLocaleString('en-GB') : '—' }
function prettyJson(value) { return JSON.stringify(value || {}, null, 2) }
function buildStatusHistory(agent) {
  const history = [{ label: 'agent registered', timestamp: formatDate(agent.created_at), detail: 'endpoint and description bound to registry' }]
  if (agent.approval_status === 'approved') {
    history.push({ label: 'approved', timestamp: formatDate(agent.approved_at), detail: agent.approved_by ? `by user #${agent.approved_by}` : '' })
  }
  if (agent.approval_status === 'rejected') {
    history.push({ label: 'rejected', timestamp: formatDate(agent.updated_at || agent.created_at), detail: 'check health, description, and endpoint then re-submit' })
  }
  history.push({ label: `health · ${agent.health_status || 'unknown'}`, timestamp: formatDate(agent.updated_at || agent.created_at), detail: 'derived from latest probe' })
  return history
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__actions { display: inline-flex; gap: var(--gap-2); }
.page-head__ca-hint { width: 100%; margin: 0; text-align: right; }

.feedback { display: flex; gap: var(--gap-2); align-items: center; font-size: var(--t-xs); padding: var(--gap-2) var(--gap-3); border: var(--border-w) solid; }
.feedback.is-err { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.feedback.is-ok  { color: var(--c-ok);     border-color: var(--c-ok);     background: var(--c-ok-soft); }

.banner {
  font-size: var(--t-xs);
  color: var(--c-warn);
  border: var(--border-w) solid var(--c-warn);
  background: var(--c-warn-soft);
  padding: var(--gap-2) var(--gap-3);
}

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-3); }
@media (max-width: 800px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }

.filters { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr; gap: var(--gap-3); }
@media (max-width: 800px) { .filters { grid-template-columns: 1fr 1fr; } }

.guide-toggle {
  background: transparent;
  border: 0;
  color: var(--c-fg-1);
  font: inherit;
  cursor: pointer;
  display: flex;
  width: 100%;
  align-items: center;
  justify-content: space-between;
  padding: 0;
}
.guide-toggle:hover { color: var(--c-accent); }
.guide { margin-top: var(--gap-3); }
.guide__lead {
  margin: 0 0 var(--gap-3); padding: var(--gap-2) var(--gap-3);
  background: var(--c-bg); border-left: 2px solid var(--c-accent);
  font-size: var(--t-sm); color: var(--c-fg-2);
}
.guide__lead strong { color: var(--c-fg-1); }
.guide__link { color: var(--c-accent); text-decoration: none; }
.guide__link:hover { text-decoration: underline; }
.guide__list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--gap-3); }
.guide__list li { display: grid; grid-template-columns: 28px 1fr; gap: var(--gap-3); font-size: var(--t-sm); color: var(--c-fg-2); }
.guide__list li p { margin: 0 0 4px; }
.guide__list li p strong { color: var(--c-fg-1); }
.guide__list li code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 4px;
  font-size: var(--t-2xs); color: var(--c-accent);
}
.guide__step {
  font-size: var(--t-xs); color: var(--c-fg-mute);
  font-variant-numeric: tabular-nums;
}
.guide__table { margin-top: 6px; }
.guide__code {
  margin: 6px 0; padding: var(--gap-2) var(--gap-3);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border);
  font-family: var(--font-mono); font-size: var(--t-2xs);
  color: var(--c-fg-1); white-space: pre; overflow-x: auto;
}

.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-desc {
  font-size: var(--t-xs); color: var(--c-fg-2);
  max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.cell-url {
  display: inline-block;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  background: var(--c-bg);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 6px;
}

.row-actions { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: var(--t-xs); }
.row-actions__sep { color: var(--c-border-strong); }

.loading { padding: var(--gap-6); text-align: center; color: var(--c-fg-3); font-size: var(--t-sm); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }

.collection-checks {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 12rem;
  overflow-y: auto;
  padding: var(--gap-2);
  border: var(--border-w) solid var(--c-border);
  background: var(--c-bg);
}
.collection-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--t-xs);
  color: var(--c-fg-1);
  cursor: pointer;
}
.collection-checks__empty {
  margin: 0;
  font-size: var(--t-xs);
  color: var(--c-fg-3);
}

.check { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: 1fr 1fr; gap: 4px; font-size: var(--t-2xs); }
.check li.is-ok { color: var(--c-ok); }
.check li.is-pending { color: var(--c-fg-3); }
.check code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 0 4px;
  color: var(--c-fg-2);
}

/* Detail drawer */
.detail__list {
  display: grid; grid-template-columns: 110px 1fr; gap: 4px var(--gap-3); margin: 0;
  font-size: var(--t-sm);
}
.detail__list dt { color: var(--c-fg-3); font-size: var(--t-2xs); text-transform: uppercase; letter-spacing: var(--tracking-caps); }
.detail__list dd { margin: 0; color: var(--c-fg-1); }
.detail__list code { font-family: var(--font-mono); font-size: var(--t-2xs); color: var(--c-fg-2); }
.detail__desc { color: var(--c-fg-2); white-space: pre-wrap; font-size: var(--t-sm); margin: 0; }
.detail__pre {
  background: var(--c-bg); border: var(--border-w) solid var(--c-border);
  padding: var(--gap-3); margin: 0; font-size: var(--t-2xs); color: var(--c-fg-2);
  max-height: 240px; overflow: auto;
}

/* P2.1 — 測試連線三事實（null＝無法判定，不用綠燈假裝通過） */
.probe-facts {
  display: grid; grid-template-columns: 110px 1fr; gap: 4px var(--gap-3);
  margin: 8px 0 0; font-size: var(--t-sm);
}
.probe-facts__row { display: contents; }
.probe-facts__row dt {
  color: var(--c-fg-3); font-size: var(--t-2xs);
  text-transform: uppercase; letter-spacing: var(--tracking-caps);
}
.probe-facts__row dd { margin: 0; color: var(--c-fg-1); }
.probe-facts__val.is-ok { color: var(--c-ok); }
.probe-facts__val.is-fail { color: var(--c-danger); }
.probe-facts__val.is-unknown { color: var(--c-fg-3); }
.probe-facts__detail {
  color: var(--c-fg-2); font-size: var(--t-xs); white-space: pre-wrap;
}

.timeline { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: var(--gap-2); }
.timeline li {
  display: grid; grid-template-columns: 12px 1fr; gap: var(--gap-2);
  padding: var(--gap-2) 0; border-bottom: var(--border-w) dashed var(--c-border);
}
.timeline li:last-child { border-bottom: 0; }
.timeline__dot {
  width: 8px; height: 8px; background: var(--c-accent); margin-top: 4px;
}
.timeline__label { color: var(--c-fg-1); font-size: var(--t-sm); margin: 0; font-weight: 500; }
.timeline__detail { color: var(--c-fg-2); font-size: var(--t-2xs); margin: 4px 0 0; }
</style>
