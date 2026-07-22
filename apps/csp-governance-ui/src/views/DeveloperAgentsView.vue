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
        <TermButton variant="primary" @click="openRegisterModal" label="註冊 Agent" />
      </div>
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
          Phase 2 ships <strong>anila-agent</strong> (openai-agents runtime + Claude-Code-style harness) as the official sub-agent template.
          Fork it, add tools, wrap in FastAPI, register. Full walkthrough on the
          <router-link to="/developer/guide" class="guide__link">developer guide page →</router-link>
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
            <th style="width: 90px">分類上限</th>
            <th style="width: 90px">預設分級</th>
            <th style="width: 100px">健康</th>
            <th style="width: 120px">審批狀態</th>
            <th style="width: 90px">加密</th>
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
              <div class="cell-meta">{{ agent.version || '—' }}</div>
            </td>
            <td>
              <span class="cell-meta">{{ agent.classification_ceiling || '—' }}</span>
            </td>
            <td>
              <span class="cell-meta">{{ agent.default_classification_level || '—' }}</span>
            </td>
            <td><TermBadge :variant="healthVariant(agent.health_status)" dot>{{ agent.health_status }}</TermBadge></td>
            <td><TermBadge :variant="approvalVariant(agent.approval_status)" dot>{{ approvalLabel(agent.approval_status) }}</TermBadge></td>
            <td>
              <TermBadge :variant="agent.requires_encryption ? 'danger' : ''">
                {{ agent.requires_encryption ? '強制' : '一般' }}
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
                  <span :title="!agent.trace_test_passed_at ? '須先通過軌跡測試' : ''">
                    <button
                      class="term-action"
                      :disabled="!isApprovable(agent.approval_status, agent.trace_test_passed_at)"
                      @click="handleApprove(agent)"
                    >核准</button>
                  </span>
                  <span class="row-actions__sep">·</span>
                  <button class="term-action term-action--danger" @click="openRejectModal(agent)">駁回</button>
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

    <!-- Register wizard: step 1 = details, step 2 = provision csk- + verify -->
    <TermModal
      :visible="showRegisterModal"
      :title="registerStep === 1 ? '註冊 · Agent（1/2）' : `核發金鑰 · ${registeredAgent?.name || ''}（2/2）`"
      width="640px"
      @close="finishRegister"
    >
      <!-- ── STEP 1 — details ────────────────────────────────────────── -->
      <div v-if="registerStep === 1" class="form-grid">
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
        <TermField label="RAG 知識庫（選填）" hint="綁定此 agent 的 csk- 可搜尋的「單一」知識庫 · 非 RAG agent 留空">
          <select v-model.number="form.collection_id" class="term-select">
            <option :value="null">— 無（非 RAG）—</option>
            <option v-for="c in collections" :key="c.id" :value="c.id">
              {{ c.name }} (#{{ c.id }})
            </option>
          </select>
        </TermField>

        <TermSection title="治理設定 · governance" />
        <div class="form-row-2">
          <TermField label="runtime 型別" :hint="runtimeTypeHint">
            <select v-model="form.runtime_type" class="term-select">
              <option v-for="o in RUNTIME_TYPE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </TermField>
          <TermField label="分類上限" hint="此 agent 可處理的最高分類等級；留白＝無上限">
            <select v-model="form.classification_ceiling" class="term-select">
              <option :value="null">— 無上限 —</option>
              <option v-for="lv in CLASSIFICATION_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
            </select>
          </TermField>
        </div>
        <TermField label="版本" hint="agent 版本字串，例如 1.0.0（選填）">
          <input v-model="form.version" class="term-input" placeholder="1.0.0" />
        </TermField>
        <label class="draft-check">
          <input type="checkbox" v-model="form.draft" />
          <span>
            <span class="draft-check__title">以草稿建立（shadow registration）</span>
            <span class="draft-check__hint">先以「草稿」狀態影子註冊，暫不進入審批佇列；適合遷移既有 agent 時先建檔、稍後再補測試與審查。</span>
          </span>
        </label>

        <TermSection title="起飛前檢查" />
        <ul class="check">
          <li :class="form.name ? 'is-ok' : 'is-pending'">{{ form.name ? '●' : '○' }} agent 名稱已設定</li>
          <li :class="/^https?:\/\//.test(form.endpoint_url) ? 'is-ok' : 'is-pending'">{{ /^https?:\/\//.test(form.endpoint_url) ? '●' : '○' }} endpoint 為 http(s) URL</li>
          <li :class="form.description_for_router.trim().length >= 24 ? 'is-ok' : 'is-pending'">{{ form.description_for_router.trim().length >= 24 ? '●' : '○' }} 說明 ≥ 24 字</li>
          <li :class="form.base_model_id ? 'is-ok' : 'is-pending'">{{ form.base_model_id ? '●' : '○' }} 已選基礎模型</li>
          <li class="is-pending">○ 已實作 <code>GET /health</code> + <code>POST /v1/chat/completions</code>（手動確認）</li>
        </ul>
      </div>

      <!-- ── STEP 2 — provision the single csk- + verify ─────────────── -->
      <div v-else class="form-grid">
        <p class="cell-meta">
          agent <strong>{{ registeredAgent?.name }}</strong>（#{{ registeredAgent?.id }}）已註冊 ·
          <TermBadge :variant="approvalVariant(registeredAgent?.approval_status)" dot>{{ approvalLabel(registeredAgent?.approval_status) }}</TermBadge>
          {{ registeredAgent?.approval_status === 'draft' ? '（草稿，未進入審批佇列）' : '待管理員審查。' }}
        </p>

        <div v-if="!newAgentCsk">
          <p class="cell-meta">
            核發此 agent 的單一 service token（<code>csk-</code>）。它同時驗證
            Router→agent 派送，以及（綁定知識庫時）agent 的 RAG 搜尋 —
            兩者共用一把。
          </p>
          <TermButton
            variant="primary" :loading="issuingNew" :disabled="issuingNew"
            label="核發 service token（csk-）" @click="handleIssueForNew"
          />
        </div>

        <div v-else>
          <div class="secret-banner secret-banner--csk">
            <div class="secret-banner__head">
              <span class="cell-strong">service token（csk-）</span>
              <span class="cell-meta">立即複製 — 不會再顯示</span>
            </div>
            <div class="secret-banner__body">
              <code class="secret-banner__token">{{ newAgentCsk }}</code>
              <TermButton size="sm" variant="ghost" @click="copyToClipboard(newAgentCsk)" label="複製" />
            </div>
          </div>

          <TermSection title="agent .env" />
          <pre class="env-snippet">{{ newAgentEnvSnippet }}</pre>
          <TermButton size="sm" variant="ghost" @click="copyToClipboard(newAgentEnvSnippet)" label="複製 .env" />

          <TermSection title="inbound guard + RAG usage（非模板 agent）" />
          <AgentGuardPanel
            :csk="newAgentCsk"
            :collection-id="registeredAgentCollectionId"
          />

          <TermSection title="驗證連線" />
          <p class="cell-meta">
            把上面的 <code>.env</code> 貼進你的 agent 並啟動，然後測試它是否
            接受該 token（證明 <code>CSP_SERVICE_TOKEN</code> 已正確接上）。
          </p>
          <TermButton
            variant="default" :loading="testing" :disabled="testing"
            label="測試連線" @click="handleTestConnection"
          />
          <div v-if="testResult" class="test-result" :class="testResult.token_accepted ? 'test-result--ok' : 'test-result--bad'">
            {{ testResult.token_accepted ? '✅' : '✗' }} {{ testResult.detail }}
          </div>
        </div>
      </div>

      <template #footer>
        <template v-if="registerStep === 1">
          <TermButton variant="ghost" @click="finishRegister" label="取消" />
          <TermButton variant="primary" :loading="registering" :disabled="registering" :label="registering ? '送出中' : '註冊 →'" @click="handleRegister" />
        </template>
        <TermButton v-else variant="primary" @click="finishRegister" label="完成" />
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
        <div class="form-row-2">
          <TermField label="分類上限" hint="此 agent 可處理的最高分類等級；留白＝無上限">
            <select v-model="editForm.classification_ceiling" class="term-select">
              <option :value="null">— 無上限 —</option>
              <option v-for="lv in CLASSIFICATION_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
            </select>
          </TermField>
          <TermField
            label="預設分級"
            hint="新建工作／對話時的起始分類等級；必須 ≤ 分類上限，與上限不同"
            :error="editClassificationInvariantError"
          >
            <select v-model="editForm.default_classification_level" class="term-select">
              <option v-for="lv in CLASSIFICATION_LEVELS" :key="lv" :value="lv">{{ lv }}</option>
            </select>
          </TermField>
        </div>
        <TermField label="capabilities · json" hint='e.g. {"streaming":true,"vision":false}' :error="editFormError">
          <textarea v-model="editForm.capabilitiesRaw" rows="3" class="term-textarea" style="font-family: var(--font-mono); font-size: var(--t-xs);" />
        </TermField>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeEditModal" label="取消" />
        <TermButton
          variant="primary"
          :disabled="editing || Boolean(editClassificationInvariantError)"
          :loading="editing"
          :label="editing ? '儲存中' : '儲存'"
          @click="handleUpdateAgent"
        />
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
          <div><dt>版本</dt><dd>{{ detailAgent.version || '—' }}</dd></div>
          <div><dt>分類上限</dt><dd>{{ detailAgent.classification_ceiling || '—' }}</dd></div>
          <div>
            <dt title="新建工作／對話時的起始分類等級；必須 ≤ 分類上限">預設分級</dt>
            <dd>{{ detailAgent.default_classification_level || '—' }}</dd>
          </div>
          <div><dt>健康</dt><dd>{{ detailAgent.health_status }}</dd></div>
          <div>
            <dt>審批狀態</dt>
            <dd><TermBadge :variant="approvalVariant(detailAgent.approval_status)" dot>{{ approvalLabel(detailAgent.approval_status) }}</TermBadge></dd>
          </div>
          <div><dt>建立時間</dt><dd class="tnum">{{ formatDate(detailAgent.created_at) }}</dd></div>
          <div><dt>擁有者</dt><dd>{{ ownerDisplay(detailAgent) }}</dd></div>
          <div><dt>基礎模型</dt><dd>{{ detailAgent.base_model_id || '—' }}</dd></div>
          <div>
            <dt>加密</dt>
            <dd>
              <TermBadge :variant="detailAgent.requires_encryption ? 'danger' : ''" dot>
                {{ detailAgent.requires_encryption ? '強制' : '一般' }}
              </TermBadge>
              <button
                v-if="authStore.isAdmin"
                class="term-action"
                style="margin-left: 8px;"
                :disabled="encryptionBusyId === detailAgent.id"
                @click="handleToggleEncryption(detailAgent)"
              >
                {{ encryptionBusyId === detailAgent.id ? '更新中…' : (detailAgent.requires_encryption ? '停用' : '啟用') }}
              </button>
            </dd>
          </div>
        </dl>

        <TermSection title="router 說明" />
        <p class="detail__desc">{{ detailAgent.description_for_router || '—' }}</p>

        <!-- Slice 5b — 軌跡測試（Full Trace 審批關卡）。admin 執行測試 → 逐項
             pass/fail 報告 → 通過後才可核准。 -->
        <template v-if="authStore.isAdmin">
          <TermSection title="軌跡測試 · full trace" />
          <p class="cell-meta">
            正式核准前必須通過軌跡測試。測試會對 agent 發出一次帶追蹤的請求，逐項檢查 span 回報。
          </p>
          <p class="trace-status" :class="detailAgent.trace_test_passed_at ? 'is-ok' : 'is-pending'">
            {{ detailAgent.trace_test_passed_at
                ? `✓ 已於 ${formatDate(detailAgent.trace_test_passed_at)} 通過軌跡測試`
                : '○ 尚未通過軌跡測試' }}
          </p>
          <div class="row-actions" style="margin: 8px 0;">
            <TermButton
              size="xs" variant="default" :loading="traceTesting" :disabled="traceTesting"
              :label="traceTesting ? '測試中' : '執行軌跡測試'" @click="handleTraceTest"
            />
            <template v-if="isPendingReview(detailAgent.approval_status)">
              <span :title="!detailAgent.trace_test_passed_at ? '須先通過軌跡測試' : ''">
                <TermButton
                  size="xs" variant="primary" :disabled="!detailIsApprovable"
                  label="核准" @click="handleApprove(detailAgent)"
                />
              </span>
              <TermButton size="xs" variant="ghost" label="駁回" @click="openRejectModal(detailAgent)" />
            </template>
          </div>
          <ul v-if="traceReport.length" class="trace-report">
            <li v-for="(item, i) in traceReport" :key="i" :class="item.passed ? 'is-ok' : 'is-bad'">
              <span class="trace-report__mark">{{ item.passed ? '✓' : '✗' }}</span>
              <span class="trace-report__name">{{ item.name }}</span>
              <span v-if="item.detail" class="trace-report__detail">{{ item.detail }}</span>
            </li>
          </ul>
        </template>

        <TermSection title="capabilities" />
        <pre v-if="hasCapabilities(detailAgent)" class="detail__pre">{{ prettyJson(detailAgent.capabilities) }}</pre>
        <TermEmpty v-else message="manifest 中未宣告 capabilities" />

        <!-- Sprint 13 PR C1 — quick link to the per-agent runtime
             config editor (tool permissions / workspace caps / guardrails). -->
        <TermSection title="執行設定" />
        <p class="cell-meta">
          工具權限 · 工作區上限 · 護欄 — 透過 agent 上的 30 秒輪詢即時套用。
          {{ detailAgent.runtime_config ? '目前已覆寫。' : '使用編譯內建預設。' }}
        </p>
        <router-link
          :to="{ name: 'AgentRuntimeConfig', params: { id: detailAgent.id } }"
          class="term-action"
          style="display: inline-block; margin-top: 4px;"
        >
          編輯執行設定 →
        </router-link>

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

        <!-- Sprint 8 X / Phase A — service token management ------------ -->
        <!-- Owner-or-admin (canEditAgent): the agent owner may self-issue a
             static csk- and see its one-time plaintext. bootstrap / rotate /
             revoke / credential listing stay admin-only (backend authz), so
             those controls below remain gated on authStore.isAdmin. -->
        <template v-if="canEditAgent(detailAgent)">
          <TermSection title="service token" />

          <!-- One-shot plaintext display: only shown right after a
               successful issue / rotate; clears when the modal closes. -->
          <div v-if="issuedSecret" class="secret-banner" :class="`secret-banner--${issuedSecret.kind}`">
            <div class="secret-banner__head">
              <span class="cell-strong">
                {{ issuedSecret.kind === 'bsk' ? 'bootstrap token（bsk-）' : 'service token（csk-）' }}
              </span>
              <span class="cell-meta">立即複製 — 不會再顯示</span>
            </div>
            <div class="secret-banner__body">
              <code class="secret-banner__token">{{ issuedSecret.value }}</code>
              <TermButton size="sm" variant="ghost" @click="copyToClipboard(issuedSecret.value)" label="複製" />
              <TermButton size="sm" variant="ghost" @click="clearIssuedSecret" label="隱藏" />
            </div>
            <ul v-if="issuedSecret.meta" class="secret-banner__meta">
              <li v-if="issuedSecret.kind === 'bsk'">
                到期 {{ formatDate(issuedSecret.meta.expires_at) }} — agent 必須呼叫
                <code>POST /api/agents/{{ issuedSecret.meta.agent_id }}/bootstrap</code>
                附上此 token + <code>endpoint_url={{ issuedSecret.meta.endpoint_url }}</code>
              </li>
              <li v-if="issuedSecret.kind === 'csk' && issuedSecret.meta.kind">
                {{ issuedSecret.meta.kind }} · credential_id={{ issuedSecret.meta.credential_id }}{{ issuedSecret.meta.label ? ` · label=${issuedSecret.meta.label}` : '' }}
              </li>
            </ul>

            <!-- Phase 0.5 — collapsible "how to use" with per-language
                 snippets pre-filled with this agent's bsk- + ids.
                 bsk- uses this per-language exchange how-to; the csk-
                 path has its own guard block below. -->
            <details
              v-if="issuedSecret.kind === 'bsk' && issuedSecret.meta"
              class="secret-banner__howto"
              open
            >
              <summary class="secret-banner__howto-summary">如何使用此 token →</summary>
              <BootstrapHowToTabs
                :csp-url="cspUrl"
                :agent-id="issuedSecret.meta.agent_id"
                :endpoint-url="issuedSecret.meta.endpoint_url"
                :bsk="issuedSecret.value"
              />
            </details>

            <!-- csk- direct-issue / rotate: non-template agents need the
                 inbound guard (anila_core isn't pip-installable) + (when a
                 collection is bound) the outbound RAG usage. -->
            <details
              v-if="issuedSecret.kind === 'csk' && issuedSecret.meta"
              class="secret-banner__howto"
              open
            >
              <summary class="secret-banner__howto-summary">非模板 agent？如何接上這把 csk- →</summary>
              <AgentGuardPanel
                :csk="issuedSecret.value"
                :collection-id="detailAgentCollectionId"
              />
            </details>
          </div>

          <div class="row-actions" style="margin-bottom: 8px;">
            <template v-if="authStore.isAdmin">
              <button class="term-action" :disabled="credentialBusyId === -1" @click="handleIssueBootstrap">
                {{ credentialBusyId === -1 ? '核發中…' : '核發 bootstrap（bsk-）' }}
              </button>
              <span class="row-actions__sep">·</span>
            </template>
            <button class="term-action" :disabled="credentialBusyId === -2" @click="openIssueStaticModal">
              {{ credentialBusyId === -2 ? '核發中…' : '核發靜態（csk-）' }}
            </button>
            <template v-if="authStore.isAdmin">
              <span class="row-actions__sep">·</span>
              <button class="term-action" @click="refreshDetailCredentials">重新整理</button>
            </template>
          </div>

          <!-- credential listing + rotate/revoke are admin-only (backend authz) -->
          <template v-if="authStore.isAdmin">
          <TermEmpty v-if="!credentialsLoading && detailCredentials.length === 0" message="尚無憑證 — 核發 bootstrap 或靜態 token 以開始" />
          <table v-else class="cred-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>標籤</th>
                <th>狀態</th>
                <th>核發時間</th>
                <th>輪替時間</th>
                <th>寬限期</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in detailCredentials" :key="c.id" :class="{ 'is-revoked': !c.is_active }">
                <td class="tnum">{{ c.id }}</td>
                <td>
                  <span v-if="c.label">{{ c.label }}</span>
                  <span v-else class="cell-meta">—</span>
                  <TermBadge v-if="c.is_legacy" variant="warn" style="margin-left: 6px;">舊版</TermBadge>
                </td>
                <td>
                  <TermBadge :variant="c.is_active ? '' : 'danger'" dot>
                    {{ c.is_active ? '使用中' : '已吊銷' }}
                  </TermBadge>
                  <TermBadge
                    v-if="c.id === dispatchedCredentialId"
                    variant="warn"
                    style="margin-left: 6px;"
                    title="CSP 派送此 agent 時用這把 csk-；fail-closed 守門碼要對應這把"
                  >CSP 派送中</TermBadge>
                </td>
                <td class="cell-meta tnum">{{ formatDate(c.issued_at) }}</td>
                <td class="cell-meta tnum">{{ c.rotated_at ? formatDate(c.rotated_at) : '—' }}</td>
                <td class="cell-meta tnum">
                  <span v-if="c.has_previous_token">至 {{ formatDate(c.previous_expires_at) }}</span>
                  <span v-else>—</span>
                </td>
                <td>
                  <div class="row-actions">
                    <template v-if="c.is_active">
                      <button class="term-action" :disabled="credentialBusyId === c.id" @click="handleRotateCredential(c)">
                        {{ credentialBusyId === c.id ? '…' : '輪替' }}
                      </button>
                      <span class="row-actions__sep">·</span>
                      <button class="term-action term-action--danger" :disabled="credentialBusyId === c.id" @click="handleRevokeCredential(c)">
                        {{ credentialBusyId === c.id ? '…' : '吊銷' }}
                      </button>
                    </template>
                    <span v-else class="cell-meta">—</span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
          </template>
        </template>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeDetailModal" label="關閉" />
      </template>
    </TermModal>

    <!-- Issue static token modal (Phase F Tier 0) -->
    <TermModal :visible="showIssueStaticModal" title="核發靜態 service token" width="440px" @close="showIssueStaticModal = false">
      <p class="cell-meta">
        靜態 csk- 不會自動輪替；建議每 90 天手動 rotate 一次。
        適合無法跑 anila-core bootstrap CLI 的舊版 / 第三方 agent（Phase F Tier 0）。
      </p>
      <TermField label="標籤（選填）" hint="例：vendor-foo / pod-1 / staging">
        <input v-model="staticLabel" class="term-input" placeholder="" maxlength="100" />
      </TermField>
      <template #footer>
        <TermButton variant="ghost" @click="showIssueStaticModal = false" label="取消" />
        <TermButton variant="primary" :loading="credentialBusyId === -2" :disabled="credentialBusyId === -2" label="核發" @click="handleIssueStatic" />
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
  approveAgent, deleteAgent, downloadTemplate, getAgent, listMyAgents,
  registerAgent, rejectAgent, setAgentEncryption, traceTestAgent,
  triggerAgentHealthCheck, updateAgent,
} from '../api/agents'
import {
  APPROVAL_STATUSES, approvalLabel, approvalVariant, isApprovable, isPendingReview,
} from '../utils/approvalStatus'
import {
  issueBootstrapToken,
  issueStaticCredential,
  listAgentCredentials,
  revokeAgentCredential,
  rotateAgentCredential,
  testAgentConnection,
} from '../api/agentCredentials'
import { listCollections } from '../api/ingestionCollections'
import { listModels } from '../api/models'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat, TermSection } from '../components/cli'
import { useDialog } from '../composables/useDialog'
import BootstrapHowToTabs from '../components/agents/BootstrapHowToTabs.vue'
import AgentGuardPanel from '../components/agents/AgentGuardPanel.vue'

// CSP base URL the snippets should reference. Derived from the
// browser origin so the dev's copy-pasted code targets whatever host
// they're actually viewing — `localhost:5173` in dev, prod hostname
// in prod. Override via VITE_CSP_BASE_URL when the API lives on a
// different origin from the SPA.
const cspUrl = import.meta.env?.VITE_CSP_BASE_URL || (typeof window !== 'undefined' ? window.location.origin : '')

const { confirm } = useDialog()
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
const encryptionBusyId = ref(null)
const deletingId = ref(null)
const healthCheckingId = ref(null)
const showEditModal = ref(false)
const editTarget = ref(null)
const editing = ref(false)
const editFormError = ref('')
const editForm = ref({
  endpoint_url: '', description_for_router: '', api_version: '', base_model_id: null,
  capabilitiesRaw: '', classification_ceiling: null, default_classification_level: '無機密',
})
const feedback = ref({ type: 'success', message: '' })

// ── Sprint 8 X / Phase A — agent service-token management ────────────────────
// detailCredentials is loaded on detail-modal open. issuedSecret holds the
// one-shot plaintext returned by issue-bootstrap / issue-static / rotate so
// we can display it once and clear it the moment the modal closes.
const detailCredentials = ref([])
const credentialsLoading = ref(false)
const credentialBusyId = ref(null)
const issuedSecret = ref(null) // { kind: 'bsk'|'csk', value, meta?, ttlExpiresAt? }
const showIssueStaticModal = ref(false)
const staticLabel = ref('')
const filters = ref({ query: '', approval: 'all', health: 'all', sort: 'newest' })
const form = ref({
  name: '', endpoint_url: '', description_for_router: '', api_version: 'v1',
  base_model_id: null, collection_id: null,
  // Slice 5b — 新增治理欄位
  runtime_type: 'openai_compatible_agent', classification_ceiling: null, version: '', draft: false,
})
const formErrors = ref({})

// Slice 5b — 篩選下拉可選的七值審批狀態（含 all）。
const approvalFilterOptions = APPROVAL_STATUSES

// runtime_type 五選一（doc 05 §3）；值為機器 token（保留英文），helper 繁中說明。
const RUNTIME_TYPE_OPTIONS = [
  { value: 'anila_agent', label: 'anila_agent', hint: '官方 anila-agent 樣板（openai-agents runtime）' },
  { value: 'langchain', label: 'langchain', hint: 'LangChain / LangGraph 服務' },
  { value: 'openwebui_pipe_compatible', label: 'openwebui_pipe_compatible', hint: '相容 OpenWebUI pipe 介面的既有 agent' },
  { value: 'openai_compatible_agent', label: 'openai_compatible_agent', hint: 'OpenAI 相容 /v1/chat/completions（預設）' },
  { value: 'custom_http', label: 'custom_http', hint: '自訂 HTTP 介面（需自行對齊契約）' },
]

// 分類上限五級（doc 08）；null = 無上限。由低到高排序。
const CLASSIFICATION_LEVELS = ['無機密', '營業秘密', '機密', '極機密', '絕對機密']

function classificationRank(level) {
  if (!level) return -1
  return CLASSIFICATION_LEVELS.indexOf(level)
}

const editClassificationInvariantError = computed(() => {
  const ceiling = editForm.value.classification_ceiling
  const defaultLevel = editForm.value.default_classification_level
  if (!ceiling || !defaultLevel) return ''
  if (classificationRank(defaultLevel) > classificationRank(ceiling)) {
    return '預設分級不可高於分類上限'
  }
  return ''
})

const runtimeTypeHint = computed(() =>
  RUNTIME_TYPE_OPTIONS.find(o => o.value === form.value.runtime_type)?.hint || '')

// ── Slice 5b — 軌跡測試（Full Trace 審批關卡）────────────────────────────────
const traceTesting = ref(false)
const traceReport = ref([]) // 正規化後的逐項 [{ name, passed, detail }]
// Register wizard: step 1 = details form, step 2 = provision the csk- + verify.
const registerStep = ref(1)
const registeredAgent = ref(null) // { id, name, bound_collection_id }
const newAgentCsk = ref('')       // one-time plaintext csk- for the new agent
const issuingNew = ref(false)
// Bound collection id for the csk- guard panel's outbound-RAG snippet.
// undefined (→ panel hides the RAG block) when the agent has no bound
// collection or the payload doesn't carry it.
const detailAgentCollectionId = computed(() =>
  typeof detailAgent.value?.bound_collection_id === 'number'
    ? detailAgent.value.bound_collection_id
    : undefined
)
const registeredAgentCollectionId = computed(() =>
  typeof registeredAgent.value?.bound_collection_id === 'number'
    ? registeredAgent.value.bound_collection_id
    : undefined
)
// Which active credential CSP actually dispatches as X-CSP-Service-Token:
// the most recently issued-OR-rotated active one — mirrors the backend's
// get_active_plaintext_for_agent ordering (coalesce(rotated_at, issued_at),
// Nit#2). Surfaced so operators with multiple active credentials know which
// csk- a fail-closed guard must match. null when no active credential.
const dispatchedCredentialId = computed(() => {
  let best = null
  let bestTs = -Infinity
  for (const c of detailCredentials.value) {
    if (!c.is_active) continue
    const ts = new Date(c.rotated_at || c.issued_at).getTime()
    if (!Number.isFinite(ts)) continue
    // Tie-break by id (mirrors backend `, id DESC`) so badge == dispatch.
    if (ts > bestTs || (ts === bestTs && c.id > best)) {
      bestTs = ts
      best = c.id
    }
  }
  return best
})
const collections = ref([])       // owner's collections, for the optional RAG bind
const testResult = ref(null)      // { reachable, token_accepted, detail }
const testing = ref(false)
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
function hasCapabilities(agent) {
  const c = agent?.capabilities
  if (!c) return false
  if (typeof c !== 'object') return true
  return Object.keys(c).length > 0
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
    base_model_id: null, collection_id: null,
    runtime_type: 'openai_compatible_agent', classification_ceiling: null, version: '', draft: false,
  }
  formErrors.value = {}
  registerStep.value = 1
  registeredAgent.value = null
  newAgentCsk.value = ''
  testResult.value = null
}

function validateForm() {
  const errors = {}
  if (!form.value.name.trim()) errors.name = 'agent name required'
  if (!/^https?:\/\//.test(form.value.endpoint_url.trim())) errors.endpoint_url = 'must be http or https url'
  if (form.value.description_for_router.trim().length < 24) errors.description_for_router = 'min 24 chars'
  if (!form.value.base_model_id) errors.base_model_id = 'base model required for usage attribution'
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
  try { const { data } = await getAgent(agent.id); detailAgent.value = data }
  catch { detailAgent.value = agent }
  // 帶出上次的軌跡測試報告（若後端已存 trace_test_report）。
  traceReport.value = normalizeTraceReport(detailAgent.value?.trace_test_report)
  showDetailModal.value = true
  // Lazy-load credentials only when admin opens the modal — avoids
  // hitting the endpoint for non-admin viewers.
  if (authStore.isAdmin) await refreshDetailCredentials()
}

// 是否可核准（狀態待審查 + 已通過軌跡測試）。row action 與 detail 共用。
const detailIsApprovable = computed(() =>
  isApprovable(detailAgent.value?.approval_status, detailAgent.value?.trace_test_passed_at))

// 後端 trace_test_report 形狀未定，防禦性正規化為 [{ name, passed, detail }]。
// 兼容陣列或 { items|checks|results: [...] }；每項的 pass 旗標容忍多種鍵名。
function normalizeTraceReport(report) {
  if (!report) return []
  const items = Array.isArray(report)
    ? report
    : (report.items || report.checks || report.results || [])
  if (!Array.isArray(items)) return []
  return items.map((it, i) => ({
    name: it.name || it.check || it.label || `檢查項目 ${i + 1}`,
    passed: it.passed ?? it.pass ?? (it.status === 'pass' || it.status === 'ok'),
    detail: it.detail || it.message || it.reason || '',
  }))
}

async function handleTraceTest() {
  if (!detailAgent.value || traceTesting.value) return
  traceTesting.value = true
  try {
    const { data } = await traceTestAgent(detailAgent.value.id)
    const report = data?.trace_test_report || data?.report || data
    traceReport.value = normalizeTraceReport(report)
    const passedAt = data?.trace_test_passed_at || report?.passed_at || null
    // 同步 detail 與列表列，讓 approve 閘門即時解鎖。
    detailAgent.value = { ...detailAgent.value, trace_test_report: report, trace_test_passed_at: passedAt }
    const idx = agents.value.findIndex(a => a.id === detailAgent.value.id)
    if (idx >= 0) agents.value[idx] = { ...agents.value[idx], trace_test_passed_at: passedAt }
    const allPass = traceReport.value.length > 0 && traceReport.value.every(r => r.passed)
    setFeedback(allPass ? 'success' : 'error',
      allPass ? '軌跡測試通過' : '軌跡測試完成，但有項目未通過，請檢視報告')
  } catch (e) {
    // 4xx（含 409）→ 繁中錯誤 toast；若後端仍附帶報告則一併呈現。
    if (e.response?.data?.trace_test_report || e.response?.data?.report) {
      traceReport.value = normalizeTraceReport(e.response.data.trace_test_report || e.response.data.report)
    }
    setFeedback('error', e.response?.data?.detail || '軌跡測試失敗，請稍後再試')
  } finally {
    traceTesting.value = false
  }
}

async function refreshDetailCredentials() {
  if (!detailAgent.value) return
  credentialsLoading.value = true
  try {
    detailCredentials.value = await listAgentCredentials(detailAgent.value.id)
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || '載入憑證失敗')
    detailCredentials.value = []
  } finally {
    credentialsLoading.value = false
  }
}

function clearIssuedSecret() {
  issuedSecret.value = null
}

function closeDetailModal() {
  showDetailModal.value = false
  detailAgent.value = null
  detailCredentials.value = []
  traceReport.value = []
  clearIssuedSecret()
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text)
    setFeedback('success', '已複製到剪貼簿')
  } catch {
    setFeedback('error', 'clipboard write failed — copy manually')
  }
}

async function handleIssueBootstrap() {
  if (!detailAgent.value) return
  if (!(await confirm({ message: `為「${detailAgent.value.name}」核發新的 bootstrap token？\n\n舊 bootstrap（若存在）會立即失效。`, confirmText: '核發', danger: true }))) return
  credentialBusyId.value = -1
  try {
    const data = await issueBootstrapToken(detailAgent.value.id)
    issuedSecret.value = {
      kind: 'bsk',
      value: data.bootstrap_token,
      meta: {
        agent_name: data.agent_name,
        agent_id: data.agent_id,
        endpoint_url: data.endpoint_url,
        expires_at: data.expires_at,
      },
    }
    setFeedback('success', 'bootstrap token 已核發 — 立即複製，不會再顯示')
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || '核發 bootstrap 失敗')
  } finally {
    credentialBusyId.value = null
  }
}

function openIssueStaticModal() {
  staticLabel.value = ''
  showIssueStaticModal.value = true
}

async function handleIssueStatic() {
  if (!detailAgent.value) return
  credentialBusyId.value = -2
  try {
    const data = await issueStaticCredential(detailAgent.value.id, staticLabel.value || null)
    issuedSecret.value = {
      kind: 'csk',
      value: data.service_token,
      meta: {
        credential_id: data.credential_id,
        label: data.label,
        issued_at: data.issued_at,
        kind: 'static (no auto-rotate)',
      },
    }
    setFeedback('success', 'service token 已核發 — 立即複製，不會再顯示')
    showIssueStaticModal.value = false
    // Listing credentials is admin-only (backend authz); an owner has already
    // got the one-time plaintext from the banner above, so skip the refresh.
    if (authStore.isAdmin) await refreshDetailCredentials()
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || '核發靜態 token 失敗')
  } finally {
    credentialBusyId.value = null
  }
}

async function handleRotateCredential(credential) {
  if (!detailAgent.value) return
  if (!(await confirm({ message: `輪替 credential id=${credential.id}？舊 token 仍可用 24h（grace window）。`, confirmText: '輪替' }))) return
  credentialBusyId.value = credential.id
  try {
    const data = await rotateAgentCredential(detailAgent.value.id, credential.id)
    issuedSecret.value = {
      kind: 'csk',
      value: data.service_token,
      meta: {
        credential_id: data.credential_id,
        label: credential.label,
        issued_at: data.issued_at,
        kind: 'rotated (previous valid 24h)',
      },
    }
    setFeedback('success', '憑證已輪替 — 立即複製新 token')
    await refreshDetailCredentials()
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || '輪替失敗')
  } finally {
    credentialBusyId.value = null
  }
}

async function handleRevokeCredential(credential) {
  if (!detailAgent.value) return
  if (!(await confirm({ message: `立即吊銷 credential id=${credential.id}？無 grace window。`, confirmText: '吊銷', danger: true }))) return
  credentialBusyId.value = credential.id
  try {
    await revokeAgentCredential(detailAgent.value.id, credential.id)
    setFeedback('success', `憑證 id=${credential.id} 已吊銷`)
    await refreshDetailCredentials()
  } catch (e) {
    setFeedback('error', e.response?.data?.detail || '吊銷失敗')
  } finally {
    credentialBusyId.value = null
  }
}

function openRejectModal(agent) { rejectTarget.value = agent; rejectReason.value = '' }
function closeRejectModal() { rejectTarget.value = null; rejectReason.value = '' }

async function handleRegister() {
  if (!validateForm()) return
  registering.value = true
  try {
    const resp = await registerAgent({
      ...form.value,
      name: form.value.name.trim(),
      endpoint_url: form.value.endpoint_url.trim(),
      description_for_router: form.value.description_for_router.trim(),
      collection_id: form.value.collection_id || null,
      // Slice 5b — 治理欄位；空值送 null（無上限 / 未填版本），draft 對應 5a 影子註冊參數。
      runtime_type: form.value.runtime_type || 'openai_compatible_agent',
      classification_ceiling: form.value.classification_ceiling || null,
      version: form.value.version.trim() || null,
      draft: form.value.draft,
    })
    // Advance to step 2 (provision key) instead of closing — one onboarding
    // flow: register → issue csk- → paste into .env → verify (S-Q2/Q3).
    registeredAgent.value = resp.data
    registerStep.value = 2
    setFeedback('success', 'agent 已註冊 · 待管理員審查 — 現在核發金鑰')
    await fetchAgents()
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'register failed') }
  finally { registering.value = false }
}

async function handleIssueForNew() {
  if (!registeredAgent.value) return
  issuingNew.value = true
  try {
    const data = await issueStaticCredential(registeredAgent.value.id, null)
    newAgentCsk.value = data.service_token
    setFeedback('success', 'service token 已核發 — 立即複製，不會再顯示')
  } catch (e) { setFeedback('error', e.response?.data?.detail || '核發 token 失敗') }
  finally { issuingNew.value = false }
}

async function handleTestConnection() {
  if (!registeredAgent.value) return
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await testAgentConnection(registeredAgent.value.id)
  } catch (e) {
    testResult.value = {
      reachable: false, token_accepted: null,
      detail: e.response?.data?.detail || 'test failed',
    }
  } finally { testing.value = false }
}

function finishRegister() {
  showRegisterModal.value = false
}

// env snippet pre-filled for the new agent's .env (S-Q1 one-key model).
// CSP_BASE_URL: the browser origin (e.g. localhost) is the ADMIN's view, not
// necessarily where the agent host can reach CSP — the agent usually runs on a
// different machine. So when the origin is loopback (or unknown) we emit a
// placeholder + comment instead of a misleading localhost value.
const newAgentEnvSnippet = computed(() => {
  const a = registeredAgent.value
  if (!a) return ''
  const origin = cspUrl || ''
  const isLoopback = !origin || /localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0/.test(origin)
  const lines = []
  if (isLoopback) {
    lines.push('# CSP_BASE_URL: set to the CSP host reachable FROM the agent machine')
    lines.push('# (the agent rarely shares a host with CSP — do NOT use localhost)')
    lines.push('CSP_BASE_URL=https://<csp-host-reachable-from-agent>')
  } else {
    lines.push(`CSP_BASE_URL=${origin}`)
  }
  lines.push(`ANILA_AGENT_NAME=${a.name}`)
  lines.push(`CSP_SERVICE_TOKEN=${newAgentCsk.value || '<paste the csk- shown above>'}`)
  if (a.bound_collection_id) lines.push(`ANILA_COLLECTION_ID=${a.bound_collection_id}`)
  return lines.join('\n')
})

function canEditAgent(agent) {
  if (!agent) return false
  if (authStore.isAdmin) return true
  return agent.owner_user_id === authStore.user?.id
}
function openEditModal(agent) {
  editTarget.value = agent
  editForm.value = {
    endpoint_url: agent.endpoint_url || '',
    description_for_router: agent.description_for_router || '',
    api_version: agent.api_version || 'v1',
    base_model_id: agent.base_model_id ?? null,
    capabilitiesRaw: agent.capabilities && Object.keys(agent.capabilities).length
      ? JSON.stringify(agent.capabilities, null, 2) : '',
    classification_ceiling: agent.classification_ceiling || null,
    default_classification_level: agent.default_classification_level || '無機密',
  }
  editFormError.value = ''
  showEditModal.value = true
}
function closeEditModal() { showEditModal.value = false; editTarget.value = null; editFormError.value = '' }

async function handleUpdateAgent() {
  if (!editTarget.value || editing.value) return
  if (editClassificationInvariantError.value) {
    editFormError.value = editClassificationInvariantError.value
    return
  }
  if (!editForm.value.base_model_id) { editFormError.value = 'base model required'; return }
  let capabilities = null
  const raw = (editForm.value.capabilitiesRaw || '').trim()
  if (raw) {
    try {
      capabilities = JSON.parse(raw)
      if (typeof capabilities !== 'object' || Array.isArray(capabilities)) throw new Error('capabilities must be an object')
    } catch (err) { editFormError.value = `capabilities json error: ${err.message}`; return }
  }
  const patch = {
    endpoint_url: editForm.value.endpoint_url.trim() || null,
    description_for_router: (editForm.value.description_for_router || '').trim() || null,
    api_version: (editForm.value.api_version || '').trim() || null,
    base_model_id: editForm.value.base_model_id,
    capabilities,
    classification_ceiling: editForm.value.classification_ceiling || null,
    default_classification_level: editForm.value.default_classification_level || '無機密',
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
  catch (e) { setFeedback('error', e.response?.data?.detail || '核准失敗，請確認已通過軌跡測試') }
}

// 核准／駁回後把最新狀態同步回開啟中的 detail modal（若操作對象就是它）。
function syncDetailFromList(id) {
  if (!detailAgent.value || detailAgent.value.id !== id) return
  const fresh = agents.value.find(a => a.id === id)
  if (fresh) detailAgent.value = { ...detailAgent.value, ...fresh }
}

async function handleToggleEncryption(agent) {
  if (!agent || encryptionBusyId.value === agent.id) return
  const next = !agent.requires_encryption
  if (next && !(await confirm({ message: `為「${agent.name}」啟用強制加密？經過它的所有對話都鎖為加密模式 — 每個對話不可逆。`, confirmText: '啟用', danger: true }))) return
  encryptionBusyId.value = agent.id
  try {
    const { data } = await setAgentEncryption(agent.id, next)
    const applied = Boolean(data?.requires_encryption ?? next)
    const idx = agents.value.findIndex(a => a.id === agent.id)
    if (idx !== -1) agents.value[idx] = { ...agents.value[idx], requires_encryption: applied }
    if (detailAgent.value && detailAgent.value.id === agent.id) detailAgent.value = { ...detailAgent.value, requires_encryption: applied }
    setFeedback('success', `已為「${agent.name}」${applied ? '啟用' : '停用'}加密`)
  } catch (e) { setFeedback('error', e.response?.data?.detail || 'encryption update failed') }
  finally { encryptionBusyId.value = null }
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
    link.download = 'anila-core-template.zip'
    link.click()
    URL.revokeObjectURL(url)
    setFeedback('success', '樣板已下載')
  } catch (e) { setFeedback('error', e.response?.data?.detail || '下載失敗') }
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

/* Sprint 8 X / Phase A — service-token banner + table */
.secret-banner {
  border: 1px solid var(--c-accent);
  background: var(--c-surface-2);
  padding: 8px 10px;
  margin: 8px 0 12px;
  font-size: var(--t-2xs);
}
.secret-banner--bsk { border-color: var(--c-warn, #c08a2c); }
.secret-banner__head {
  display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px;
}
.secret-banner__body { display: flex; align-items: center; gap: 8px; }
.secret-banner__token {
  flex: 1;
  font-family: var(--font-mono); font-size: var(--t-sm);
  background: var(--c-surface-1); color: var(--c-fg-1);
  border: var(--border-w) solid var(--c-border-strong);
  padding: 4px 6px;
  word-break: break-all; user-select: all;
}
.secret-banner__meta { margin: 6px 0 0; padding-left: 1.2em; color: var(--c-fg-2); }
.secret-banner__meta li { line-height: 1.5; }
.secret-banner__meta code {
  background: var(--c-surface-1); color: var(--c-fg-1);
  border: var(--border-w) solid var(--c-border);
  padding: 1px 4px; font-size: var(--t-3xs);
}

.secret-banner__howto { margin-top: 12px; }
.secret-banner__howto-summary {
  cursor: pointer;
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 4px 0;
  user-select: none;
}
.secret-banner__howto-summary:hover { color: var(--c-fg-1); }
.secret-banner__howto[open] .secret-banner__howto-summary { margin-bottom: 8px; }

.cred-table { width: 100%; border-collapse: collapse; font-size: var(--t-2xs); }
.cred-table th {
  text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--c-divider);
  font-weight: 500; color: var(--c-fg-2); text-transform: uppercase;
  font-size: var(--t-3xs); letter-spacing: 0.04em;
}
.cred-table td { padding: 6px; border-bottom: 1px solid var(--c-divider); vertical-align: middle; }
.cred-table tr.is-revoked td { opacity: 0.5; }

.env-snippet {
  background: var(--c-surface-2);
  border: 1px solid var(--c-divider);
  border-radius: var(--r-sharp);
  padding: 8px 10px;
  margin: 6px 0;
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
  user-select: all;
  color: var(--c-fg-1);
}
.test-result {
  margin-top: 8px;
  padding: 6px 10px;
  border-radius: var(--r-sharp);
  border: 1px solid var(--c-divider);
  font-size: var(--t-2xs);
  line-height: 1.5;
}
.test-result--ok { border-color: var(--c-ok); color: var(--c-ok); }
.test-result--bad { border-color: var(--c-danger); color: var(--c-danger); }

/* Slice 5b — shadow-registration checkbox */
.draft-check {
  display: flex; gap: var(--gap-2); align-items: flex-start;
  font-size: var(--t-2xs); color: var(--c-fg-2); cursor: pointer;
  border: var(--border-w) solid var(--c-border);
  padding: var(--gap-2) var(--gap-3); background: var(--c-bg);
}
.draft-check input { margin-top: 2px; flex-shrink: 0; }
.draft-check__title { display: block; color: var(--c-fg-1); font-weight: 500; }
.draft-check__hint { display: block; color: var(--c-fg-3); margin-top: 2px; line-height: 1.5; }

/* Slice 5b — trace-test status line + per-item pass/fail report */
.trace-status { font-size: var(--t-xs); margin: 4px 0 0; }
.trace-status.is-ok { color: var(--c-ok); }
.trace-status.is-pending { color: var(--c-fg-3); }
.trace-report { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-direction: column; gap: 4px; }
.trace-report li {
  display: grid; grid-template-columns: 16px auto 1fr; gap: 6px; align-items: baseline;
  font-size: var(--t-2xs); padding: 4px 6px; border: var(--border-w) solid var(--c-border);
}
.trace-report li.is-ok { color: var(--c-fg-1); }
.trace-report li.is-bad { color: var(--c-danger); border-color: var(--c-danger); background: var(--c-danger-soft); }
.trace-report__mark { font-weight: 700; }
.trace-report__detail { color: var(--c-fg-3); }
</style>
