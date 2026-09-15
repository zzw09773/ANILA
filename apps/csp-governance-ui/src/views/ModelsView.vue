<template>
  <div class="page">
    <PageHead title="模型" subtitle="已開放對話選單的模型，在「對話選單」欄可設定可使用對象。">
      <template #actions>
        <TermButton v-if="authStore.isAdmin" variant="ghost" @click="openImportModal" label="整批帶入" />
        <TermButton
          v-if="canSetEndpointAddress"
          variant="primary"
          @click="openCreateModal"
          label="註冊模型"
        />
      </template>
    </PageHead>

    <!-- P4.6b — 擁有者指派可設定／看見端點位址的開發者或管理員 -->
    <TermBox
      v-if="authStore.isOwner"
      title="端點位址設定授權"
      hint="僅擁有者與下列獲授權者可登錄／變更／看見模型端點位址"
    >
      <div class="author-grant">
        <div class="author-grant__form">
          <TermField label="指派對象" hint="從開發者或管理員帳號中選擇；撤銷立即生效">
            <div class="author-grant__row">
              <select v-model="grantUserId" class="term-select">
                <option :value="null">— 請選擇 —</option>
                <option
                  v-for="u in grantableAuthors"
                  :key="u.id"
                  :value="u.id"
                >
                  {{ u.username }}（{{ roleLabel(u.role) }}）
                </option>
              </select>
              <TermButton
                variant="primary"
                :disabled="!grantUserId || granting"
                :label="granting ? '指派中…' : '授予'"
                @click="handleGrantAuthor"
              />
            </div>
          </TermField>
        </div>
        <table v-if="endpointAuthors.length" class="term-table author-grant__table">
          <thead>
            <tr>
              <th>開發者</th>
              <th style="width: 40%">授予時間</th>
              <th style="width: 100px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="g in endpointAuthors" :key="g.id">
              <td class="cell-strong">{{ g.username || `user#${g.user_id}` }}</td>
              <td class="cell-meta">{{ formatDate(g.granted_at) }}</td>
              <td>
                <button
                  class="term-action term-action--danger"
                  :disabled="revokingId === g.id"
                  @click="handleRevokeAuthor(g)"
                >
                  {{ revokingId === g.id ? '撤銷中…' : '撤銷' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <TermEmpty v-else message="尚未指派任何開發者 · 目前僅擁有者可設定端點位址" />
      </div>
    </TermBox>

    <div class="kpi-row">
      <TermStat label="模型 · 總數" :value="modelsStore.models.length" />
      <TermStat label="健康" :value="healthyCount" tone="accent" />
      <TermStat label="降級" :value="degradedCount" :tone="degradedCount ? 'warn' : 'default'" />
      <TermStat label="異常" :value="unhealthyCount" :tone="unhealthyCount ? 'danger' : 'default'" />
    </div>

    <TermBox :title="`已註冊 · ${modelsStore.models.length}`" hint="每 60 秒健康檢查" pad="none" flush>
      <table class="term-table">
        <thead>
          <tr>
            <th style="width: 96px">健康</th>
            <th>名稱</th>
            <th style="width: 148px">支援等級</th>
            <th style="width: 100px">類型</th>
            <th style="width: 92px">分類上限</th>
            <th>端點</th>
            <th style="width: 80px">API</th>
            <th style="width: 80px">啟用</th>
            <th style="width: 148px">對話選單</th>
            <th v-if="authStore.isAdmin || canSetEndpointAddress" style="width: 26%">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="model in modelsStore.models" :key="model.id">
            <td>
              <TermBadge :variant="healthVariant(model.health_status)" dot>{{ healthLabel(model.health_status) }}</TermBadge>
              <div v-if="testResults[model.id]?.latencyLabel" class="cell-meta cell-latency">{{ testResults[model.id].latencyLabel }}</div>
            </td>
            <td>
              <div class="cell-strong">
                <span
                  v-if="model.is_internal"
                  class="internal-lock"
                  title="lives on anila-models-net internal docker network — no host port exposure"
                >🔒</span>
                {{ model.display_name }}
              </div>
              <div class="cell-meta">{{ model.name }}</div>
              <div v-if="model.base_model_name" class="cell-base">↳ base: {{ model.base_model_name }}</div>
              <div class="cell-caps">
                <TermBadge v-if="model.protocol" variant="" class="cap-chip">{{ protocolLabel(model.protocol) }}</TermBadge>
                <TermBadge v-for="cap in capabilityChips(model)" :key="cap" variant="info" class="cap-chip">{{ cap }}</TermBadge>
                <!-- triton_grpc 不走金鑰:顯示「使用全域金鑰」會讓人以為
                     gRPC 呼叫帶了全域 Bearer,實際上一個 byte 都沒帶。 -->
                <span
                  v-if="model.protocol !== 'triton_grpc'"
                  class="cap-key"
                  :class="model.has_api_key ? 'cap-key--set' : 'cap-key--global'"
                >
                  {{ model.has_api_key ? '已設定模型金鑰' : '使用全域金鑰' }}
                </span>
                <span v-else class="cap-key cap-key--global">不使用金鑰</span>
                <span
                  v-if="thinkingEffortChip(model.thinking_effort)"
                  class="cap-key think-chip"
                >{{ thinkingEffortChip(model.thinking_effort) }}</span>
              </div>
            </td>
            <td>
              <ThinkingLevelsDisplay :levels="model.thinking_levels_supported" />
            </td>
            <td><TermBadge :tone="model.model_type">{{ model.model_type }}</TermBadge></td>
            <td>
              <span v-if="classificationCeilingLabel(model.classification_ceiling) === '無上限'" class="cell-meta">無上限</span>
              <TermBadge v-else variant="accent">{{ classificationCeilingLabel(model.classification_ceiling) }}</TermBadge>
            </td>
            <td>
              <span
                v-if="model.endpoint_url === ENDPOINT_INTERNAL"
                class="cell-meta cell-meta--internal"
                title="endpoint lives on anila-models-net (cross-stack docker DNS) — owner can see the URL"
              >🔒 internal</span>
              <span
                v-else-if="model.endpoint_url === ENDPOINT_REDACTED"
                class="cell-meta"
                title="endpoint URL is owner-only (deployment topology)"
              >🔒 owner-only</span>
              <code v-else class="cell-url" :title="model.endpoint_url">{{ model.endpoint_url }}</code>
            </td>
            <td class="cell-meta">{{ model.api_version }}</td>
            <td>
              <TermBadge :variant="model.is_active ? 'ok' : 'danger'" dot>
                {{ model.is_active ? '開' : '關' }}
              </TermBadge>
            </td>
            <td>
              <span v-if="model.name === 'anila-router'" class="primary-pill" title="平台聊天入口，不是可選基礎模型">
                平台入口
              </span>
              <span v-if="model.router_enabled && model.name !== 'anila-router'" class="primary-pill" title="可用於 Router 對話模型">
                Router
              </span>
              <span v-if="model.is_router_primary" class="primary-pill" title="全院 Router 預設基礎模型">
                ★ 全院預設
              </span>
              <span
                v-if="model.is_image_primary"
                class="primary-pill"
                title="flux2-dev-agent / anila-studio 以此為主圖像模型"
              >
                ★ 主圖像
              </span>
              <span
                v-if="model.is_asr_primary"
                class="primary-pill"
                title="asr-gateway 以此為主語音辨識 decoder"
              >
                ★ 主語音
              </span>
              <span
                v-if="model.is_slides_primary && model.name !== 'anila-router'"
                class="primary-pill"
                title="anila-studio 以此模型產生簡報並做視覺檢查"
              >
                ★ 主簡報
              </span>
              <span
                v-if="model.is_platform_embedding"
                class="primary-pill primary-pill--embed"
                :title="platformEmbedTitle(model)"
              >
                ★ 主 embedding
              </span>
              <span v-if="!model.is_router_primary && !model.is_image_primary && !model.is_asr_primary && !model.is_slides_primary && !model.is_platform_embedding" class="cell-meta">—</span>
              <div v-if="canEditAudience(model)" class="audience-link">
                <button type="button" class="term-action" @click="openEditModal(model, { focusGrants: true })">可使用對象</button>
              </div>
            </td>
            <td v-if="authStore.isAdmin || canSetEndpointAddress">
              <RowActions>
                <button class="term-action" @click="openEditModal(model)">編輯</button>
                <template v-if="authStore.isAdmin">
                  <button
                    class="term-action"
                    :disabled="testingId === model.id"
                    title="主動探測此端點連線並回報五態健康與延遲"
                    @click="handleTest(model)"
                  >{{ testingId === model.id ? '測試中…' : '測試連線' }}</button>
                </template>
                <button
                  v-if="authStore.isAdmin && model.is_active"
                  class="term-action"
                  @click="handleDeactivate(model.id)"
                >停用</button>
                <button
                  v-else-if="authStore.isAdmin && !model.is_active"
                  class="term-action"
                  @click="handleActivate(model.id)"
                >啟用</button>
                <template #more v-if="hasMoreActions(model)">
                  <button
                    v-if="model.model_type === 'llm' && model.name !== 'anila-router' && !model.is_router_primary"
                    class="term-action"
                    :disabled="!model.is_active || settingPrimaryId === model.id"
                    @click="handleSetPrimary(model.id)"
                  >
                    {{ settingPrimaryId === model.id ? '設定中…' : '設為全院預設' }}
                  </button>
                  <button
                    v-if="model.is_router_primary"
                    class="term-action"
                    :disabled="settingPrimaryId === model.id"
                    @click="handleUnsetPrimary(model.id)"
                  >
                    取消全院預設
                  </button>
                  <button
                    v-if="model.model_type === 'llm' && model.name !== 'anila-router' && !model.is_slides_primary"
                    class="term-action"
                    :disabled="!model.is_active || settingSlidesPrimaryId === model.id"
                    title="簡報製作（Studio）用這顆模型寫內容與做視覺檢查；建議選不思考（nothink）的版本"
                    @click="handleSetSlidesPrimary(model.id)"
                  >
                    {{ settingSlidesPrimaryId === model.id ? '設定中…' : '設為主簡報' }}
                  </button>
                  <button
                    v-if="model.is_slides_primary && model.name !== 'anila-router'"
                    class="term-action"
                    :disabled="settingSlidesPrimaryId === model.id"
                    @click="handleUnsetSlidesPrimary(model.id)"
                  >
                    取消主簡報
                  </button>
                  <button
                    v-if="model.model_type === 'image' && !model.is_image_primary"
                    class="term-action"
                    :disabled="!model.is_active || settingImagePrimaryId === model.id"
                    @click="handleSetImagePrimary(model.id)"
                  >
                    {{ settingImagePrimaryId === model.id ? '設定中…' : '設為主圖像模型' }}
                  </button>
                  <button
                    v-if="model.is_image_primary"
                    class="term-action"
                    :disabled="settingImagePrimaryId === model.id"
                    @click="handleUnsetImagePrimary(model.id)"
                  >
                    取消主圖像
                  </button>
                  <button
                    v-if="model.model_type === 'asr' && !model.is_asr_primary"
                    class="term-action"
                    :disabled="!model.is_active || settingAsrPrimaryId === model.id"
                    @click="handleSetAsrPrimary(model.id)"
                  >
                    {{ settingAsrPrimaryId === model.id ? '設定中…' : '設為主語音辨識' }}
                  </button>
                  <button
                    v-if="model.is_asr_primary"
                    class="term-action"
                    :disabled="settingAsrPrimaryId === model.id"
                    @click="handleUnsetAsrPrimary(model.id)"
                  >
                    取消主語音
                  </button>
                  <button
                    v-if="model.model_type === 'embedding' && !model.is_platform_embedding"
                    class="term-action"
                    :disabled="!model.is_active || settingEmbedId === model.id"
                    @click="handleSetPlatformEmbed(model.id)"
                  >
                    {{ settingEmbedId === model.id ? '設定中…' : '設為主 embedding' }}
                  </button>
                  <button
                    v-if="model.is_platform_embedding"
                    class="term-action"
                    :disabled="settingEmbedId === model.id"
                    @click="handleUnsetPlatformEmbed(model.id)"
                  >
                    取消主 embedding
                  </button>
                  <button
                    v-if="authStore.isOwner"
                    class="term-action term-action--danger"
                    :disabled="purgingId === model.id"
                    :title="'hard-delete this row · irreversible · owner-only'"
                    @click="handlePurge(model)"
                  >
                    {{ purgingId === model.id ? '清除中…' : '刪除模型登錄' }}
                  </button>
                </template>
              </RowActions>
            </td>
          </tr>
          <tr v-if="modelsStore.models.length === 0">
            <td :colspan="tableColspan"><TermEmpty message="尚未註冊模型 · 註冊後即可啟用 /v1/* 代理" /></td>
          </tr>
        </tbody>
      </table>
    </TermBox>

    <TermModal :visible="showModal" :title="editingId ? '編輯 · 模型' : '註冊 · 模型'" width="600px" @close="showModal = false">
      <div class="form-grid">
        <TermField label="模型 ID" hint="不可變更 · 用於 API 請求 · 例：llama3-70b">
          <input v-model="form.name" :disabled="!!editingId" class="term-input" placeholder="llama3-70b" />
        </TermField>
        <TermField label="顯示名稱">
          <input
            v-model="form.display_name"
            class="term-input"
            placeholder="Llama 3 70B Instruct"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <div class="form-row-2">
          <TermField label="類型">
            <select v-model="form.model_type" class="term-select" :disabled="addressOnlyEditor">
              <option value="llm">llm</option>
              <option value="vlm">vlm</option>
              <option value="embedding">embedding</option>
              <option value="agent">agent</option>
              <option value="image">image</option>
              <option value="asr">asr</option>
            </select>
          </TermField>
          <TermField
            label="URL 路徑前綴"
            hint="只改上游路徑的 /v1 或 /v2，不是通訊協定；body 與回應解析相同。Triton gRPC 模型可忽略此欄。"
          >
            <select v-model="form.api_version" class="term-select" :disabled="addressOnlyEditor">
              <option value="v1">v1（路徑前綴）</option>
              <option value="v2">v2（路徑前綴）</option>
            </select>
          </TermField>
        </div>
        <TermField
          label="端點 URL"
          :hint="endpointUrlHint"
        >
          <input
            v-model="form.endpoint_url"
            class="term-input"
            :disabled="endpointFieldLocked"
            :placeholder="endpointUrlPlaceholder"
          />
        </TermField>
        <p v-if="form.model_type === 'asr'" class="field-note">
          asr-gateway 會呼叫 <code>{此位址}/transcribe</code>，請填 decoder 根位址（例如
          <code>http://asr-decoder:9000</code>），不要加 <code>/v1</code>（模型登錄慣例的
          <code>/v1</code> 在這裡會變成 404）。共享密鑰 <code>ASR_DECODER_TOKEN</code> 不進本登錄；
          換到新的 GPU 主機時，該主機必須以相同 token 部署，否則每句都會 401。
        </p>
        <TermField
          label="內部"
          hint="位於 anila-models-net（跨 stack docker DNS）— 不對外開 host port，URL 僅 owner 可見"
        >
          <label class="internal-checkbox">
            <input
              v-model="form.is_internal"
              type="checkbox"
              :disabled="endpointFieldLocked || addressOnlyEditor"
            />
            <span>{{ form.is_internal ? '內部 · 僅平台 stack 內可連' : '外部 · 內網 LAN 或公開端點' }}</span>
          </label>
        </TermField>
        <div class="form-row-2">
          <TermField
            label="協定 · protocol"
            hint="端點所講的 wire protocol（與上方 URL 路徑前綴無關）"
          >
            <select v-model="form.protocol" class="term-select" :disabled="addressOnlyEditor">
              <option v-for="p in PROTOCOL_OPTIONS" :key="p.value" :value="p.value">{{ p.label }}</option>
            </select>
          </TermField>
          <TermField label="分類上限" hint="可承接的最高分類；留空＝無上限">
            <select
              v-model="form.classification_ceiling"
              class="term-select"
              :disabled="addressOnlyEditor"
            >
              <option :value="null">— 無上限 —</option>
              <option v-for="lvl in CLASSIFICATION_LEVELS" :key="lvl" :value="lvl">{{ lvl }}</option>
            </select>
          </TermField>
        </div>
        <p v-if="form.protocol === 'triton_grpc'" class="field-note">
          Triton/KServe gRPC：平台會依呼叫端決定 query／documents 輸入張量（搜尋＝query、匯入＝documents）。
          模型名稱須與 Triton 上的 model name 一致（例如 <code>nv-embed-v2</code>）。
          本協定不送金鑰（gRPC 通道不帶 call credentials），故不顯示金鑰欄位。
        </p>
        <!--
          金鑰欄位只在會真的送出金鑰的協定下出現。triton_grpc 路徑從不呼叫
          resolve_model_gateway_key / _apply_gateway_auth,client.py 也沒有掛
          call credentials 或 metadata —— 留著這個欄位就是「打了字、跳成功、
          什麼也沒送出去」的假控制項(docs/FAKE-CONTROLS.md)。
        -->
        <p v-if="form.name === 'anila-router'" class="field-note">
          這是平台入口「ANILA 自動選助手」，不必另設金鑰；Router 轉送呼叫者 JWT／CSP sk-。
        </p>
        <TermField
          v-if="form.protocol !== 'triton_grpc' && form.name !== 'anila-router'"
          label="模型金鑰 · api key"
          optional
          hint="僅寫入,不會回顯;留空=沿用現值或全域金鑰"
        >
          <input
            v-model="form.api_key"
            type="password"
            autocomplete="new-password"
            class="term-input"
            placeholder="Bearer 金鑰(留空＝沿用現值/全域)"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <TermField v-if="form.name !== 'anila-router' && (form.model_type === 'llm' || form.model_type === 'vlm')" label="可用於 Router">
          <label class="term-check"><input type="checkbox" v-model="form.router_enabled" :disabled="addressOnlyEditor" /> 開放給對話模型選單</label>
        </TermField>
        <div id="model-grant-editor" v-if="form.name !== 'anila-router' && (form.model_type === 'llm' || form.model_type === 'vlm') && form.router_enabled" class="grant-editor">
          <p class="field-note">可使用對象（全院／部門／群組／個人）。儲存模型時一併寫入。</p>
          <div v-for="(g, idx) in routerGrants" :key="idx" class="grant-row">
            <select v-model="g.scope_type" class="term-select" :disabled="addressOnlyEditor">
              <option value="all">全院</option>
              <option value="department">部門</option>
              <option value="group">群組</option>
              <option value="user">個人</option>
            </select>
            <select v-if="g.scope_type === 'department'" v-model.number="g.department_id" class="term-select" :disabled="addressOnlyEditor">
              <option :value="null">選擇部門</option>
              <option v-for="d in departmentChoices" :key="d.id" :value="d.id">{{ d.label }}</option>
            </select>
            <label v-if="g.scope_type === 'department'" class="term-check"><input type="checkbox" v-model="g.include_descendants" :disabled="addressOnlyEditor" /> 含子部門</label>
            <select v-if="g.scope_type === 'group'" v-model.number="g.group_id" class="term-select" :disabled="addressOnlyEditor">
              <option :value="null">選擇群組</option>
              <option v-for="grp in accessGroups" :key="grp.id" :value="grp.id">{{ grp.name }}</option>
            </select>
            <div v-if="g.scope_type === 'user'" class="grant-user">
              <span v-if="g.username || g.user_id" class="cell-strong">{{ g.username || ('#' + g.user_id) }}</span>
              <UserSearchField :disabled="addressOnlyEditor" placeholder="搜尋帳號後點選" @select="u => pickGrantUser(g, u)" />
            </div>
            <button type="button" class="term-action" :disabled="addressOnlyEditor" @click="routerGrants.splice(idx,1)">移除</button>
          </div>
          <button type="button" class="term-action" :disabled="addressOnlyEditor" @click="addRouterGrant">新增對象</button>
        </div>
        <TermField label="描述" optional>
          <textarea
            v-model="form.description"
            rows="2"
            class="term-textarea"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <TermField label="context window" optional hint="tokens">
          <input
            v-model.number="form.context_window"
            type="number"
            class="term-input"
            placeholder="128000"
            :disabled="addressOnlyEditor"
          />
        </TermField>
        <TermField v-if="form.model_type === 'agent'" label="基礎模型" hint="用於用量歸屬">
          <select v-model="form.base_model_id" class="term-select" :disabled="addressOnlyEditor">
            <option :value="null">— 獨立 —</option>
            <option v-for="m in baseModelOptions" :key="m.id" :value="m.id">
              {{ m.display_name }} ({{ m.model_type }})
            </option>
          </select>
        </TermField>
        <div
          v-if="(form.model_type === 'llm' || form.model_type === 'vlm') && !addressOnlyEditor"
          class="form-section"
        >
          <p class="form-section__title">推理設定</p>
          <p class="field-note">
            思考模型請搭配較高溫度（約 0.6／0.95／1.5），避免低溫造成重複迴圈。
            留空＝沿用上游預設。
          </p>
          <TermField
            label="thinking_effort"
            hint="原字串送上游 reasoning_effort；NONE＝enable_thinking=false。各後端支援的等級不同（院內 Qwen vLLM：low／medium／xhigh，預設 xhigh，不收 high／max；gemma 一律忽略）。儲存時會向模型探測一次，被拒絕的等級存不進去。"
          >
            <select v-model="form.thinking_effort" class="term-select">
              <option
                v-for="opt in THINKING_EFFORT_OPTIONS"
                :key="opt.value"
                :value="opt.value"
              >{{ thinkingEffortOptionLabel(opt.value, form.thinking_levels_supported) }}</option>
            </select>
          </TermField>
          <TermField
            label="支援等級"
            hint="新增、整批帶入或改端點時會自動探測。端點升級後可手動重跑。"
          >
            <div class="thinking-levels-row">
              <ThinkingLevelsDisplay :levels="form.thinking_levels_supported" />
              <TermButton
                v-if="editingId && authStore.isAdmin"
                variant="ghost"
                size="xs"
                :disabled="probingThinking"
                :label="probingThinking ? '探測中…' : '重新探測'"
                @click="handleProbeThinking"
              />
            </div>
          </TermField>
          <TermField
            label="允許使用者自選思考程度"
            hint="關閉後，ANILA 對話中的思考選單對此模型鎖定，改用上方的預設思考程度。"
          >
            <label class="term-check">
              <input type="checkbox" v-model="form.thinking_user_selectable" />
              允許使用者自選思考程度
            </label>
          </TermField>
          <div class="form-row-2">
            <TermField label="temperature" optional hint="0–2 · 留空＝上游預設">
              <input
                :value="form.temperature ?? ''"
                type="number"
                step="0.05"
                min="0"
                max="2"
                class="term-input"
                placeholder="上游預設"
                @input="form.temperature = parseOptionalNumber($event.target.value)"
              />
            </TermField>
            <TermField label="top_p" optional hint="0–1 · 留空＝上游預設">
              <input
                :value="form.top_p ?? ''"
                type="number"
                step="0.05"
                min="0"
                max="1"
                class="term-input"
                placeholder="上游預設"
                @input="form.top_p = parseOptionalNumber($event.target.value)"
              />
            </TermField>
          </div>
          <div class="form-row-2">
            <TermField label="presence_penalty" optional hint="-2–2 · 留空＝上游預設">
              <input
                :value="form.presence_penalty ?? ''"
                type="number"
                step="0.1"
                min="-2"
                max="2"
                class="term-input"
                placeholder="上游預設"
                @input="form.presence_penalty = parseOptionalNumber($event.target.value)"
              />
            </TermField>
            <TermField label="max_tokens" optional hint="大於 0 · 留空＝上游預設">
              <input
                :value="form.max_tokens ?? ''"
                type="number"
                step="1"
                min="1"
                class="term-input"
                placeholder="上游預設"
                @input="form.max_tokens = parseOptionalInteger($event.target.value)"
              />
            </TermField>
          </div>
        </div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="showModal = false" label="取消" />
        <TermButton
          variant="primary"
          :disabled="!form.name || !form.display_name || (!endpointFieldLocked && !form.endpoint_url)"
          :label="editingId ? '更新' : '註冊'"
          @click="handleSubmit"
        />
      </template>
    </TermModal>

    <!-- P4.6 — 整批帶入：選已註冊端點 → 拉上游 /v1/models → 回報計數 -->
    <TermModal
      :visible="showImportModal"
      title="整批帶入 · 模型"
      width="560px"
      @close="closeImportModal"
    >
      <div class="form-grid">
        <p class="import-hint">
          從已註冊端點拉取上游 <code>/models</code> 清單並寫入登錄表。
          已存在的名稱不會覆寫管理員設定；格式錯誤的項目會略過並附原因。
          新帶入列只繼承端點層級欄位（含分類上限），context window 與能力旗標維持保守預設，並維持停用待檢視後啟用。
          相同端點會合併為一個選項（位址本身仍僅擁有者可見）。
        </p>
        <TermField
          label="來源端點"
          hint="相同端點位址會合併為一個選項"
        >
          <select v-model="importSourceId" class="term-select">
            <option :value="null">— 請選擇 —</option>
            <option
              v-for="opt in importEndpointOptions"
              :key="opt.sourceModelId"
              :value="opt.sourceModelId"
            >
              {{ opt.label }}
            </option>
          </select>
        </TermField>
        <div v-if="importResult" class="import-result">
          <p class="import-result__summary">
            新增 {{ importResult.created }} ·
            已存在 {{ importResult.already_existed }} ·
            略過 {{ importResult.skipped }}
            <template v-if="importResult.truncated"> · 尚有 {{ importResult.truncated }} 筆未帶入（可再次執行以繼續）</template>
            <template v-if="importResult.missing_from_listing?.length">
              · 上游未列出 {{ importResult.missing_from_listing.length }}
            </template>
          </p>
          <ul v-if="importResult.created_entries?.length" class="import-result__list">
            <li v-for="e in importResult.created_entries" :key="'c-' + e.name">
              新增 · {{ e.name }}
              <span v-if="e.guessed_fields?.length" class="cell-meta">
                — 待確認: {{ e.guessed_fields.join(', ') }}
              </span>
            </li>
          </ul>
          <ul v-else-if="importResult.created_names?.length" class="import-result__list">
            <li v-for="n in importResult.created_names" :key="'c-' + n">新增 · {{ n }}</li>
          </ul>
          <ul v-if="importResult.unchanged?.length" class="import-result__list">
            <li v-for="u in importResult.unchanged" :key="'u-' + u.name">
              未變更 · {{ u.name }}
              <span class="cell-meta"> — {{ u.reason }}</span>
            </li>
          </ul>
          <ul v-if="importResult.skipped_entries?.length" class="import-result__list import-result__list--skip">
            <li v-for="(s, i) in importResult.skipped_entries" :key="'s-' + i">
              略過 · {{ s.name || '（無名稱）' }}
              <span class="cell-meta"> — {{ s.reason }}</span>
            </li>
          </ul>
          <ul v-if="importResult.missing_from_listing?.length" class="import-result__list import-result__list--skip">
            <li v-for="m in importResult.missing_from_listing" :key="'m-' + m.name">
              上游未列出 · {{ m.name }}
              <span class="cell-meta"> — {{ m.reason }}</span>
            </li>
          </ul>
        </div>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="closeImportModal" label="關閉" />
        <TermButton
          v-if="importResult?.created_names?.length"
          variant="ghost"
          :disabled="activatingCreated"
          :label="activatingCreated ? '啟用中…' : `啟用本次新增（${importResult.created_names.length}）`"
          @click="handleActivateCreated"
        />
        <TermButton
          variant="primary"
          :disabled="!importSourceId || importing"
          :label="importing ? '帶入中…' : '開始帶入'"
          @click="handleImport"
        />
      </template>
    </TermModal>

    <!-- Phase 2 — typed 400 confirm modal. Shows when backend rejects a
         register/update because the hostname isn't in trusted_hosts AND
         the failure is fixable (single-label / internal-zone, NOT
         loopback/metadata). Owner can one-click promote + retry. -->
    <TermModal
      :visible="!!untrustedHostPrompt"
      title="hostname 未在受信任清單"
      width="540px"
      @close="cancelTrustPrompt"
    >
      <div v-if="untrustedHostPrompt" class="trust-prompt">
        <p class="trust-prompt__hint">{{ untrustedHostPrompt.hint }}</p>
        <p class="trust-prompt__cta">
          要把 <code>{{ untrustedHostPrompt.host }}</code> 加進 trusted_hosts 並重試嗎?
          <br>
          <span class="cell-meta">會寫一筆 audit log,事後可在 /trusted-hosts 移除。</span>
        </p>
      </div>
      <template #footer>
        <TermButton variant="ghost" @click="cancelTrustPrompt" label="取消" />
        <TermButton
          variant="primary"
          :label="`加入 ${untrustedHostPrompt?.host || ''} 並重試`"
          @click="confirmTrustAndRetry"
        />
      </template>
    </TermModal>
  </div>
</template>

<script setup>
import { roleLabel } from '../utils/roleLabel'
import { ref, computed, onMounted, nextTick } from 'vue'
import { useModelsStore } from '../stores/models'
import { useAuthStore } from '../stores/auth'
import {
  getMyEndpointAuthorStatus,
  listEndpointAuthors,
  grantEndpointAuthor,
  revokeEndpointAuthor,
  listRouterGrants,
  replaceRouterGrants,
  listModelAccessGroups,
} from '../api/models'
import { listUsers } from '../api/users'
import { listDepartments } from '../api/departments'
import { departmentOptions } from '../utils/departmentTree'
import { extractError, getRawDetail } from '../api/errors'
import { grantsLoadResult, canReplaceRouterGrants } from '../utils/routerGrantsLoad.js'
import { TermBox, TermButton, TermField, TermBadge, TermEmpty, TermModal, TermStat, PageHead, RowActions, UserSearchField } from '../components/cli'
import ThinkingLevelsDisplay from '../components/ThinkingLevelsDisplay.vue'
import { useDialog } from '../composables/useDialog'
import { healthLabel, healthVariant, normalizeHealth } from '../utils/healthStatus'
import { designationConfirm, designationToast } from '../utils/platformEmbedding'
import { formatDate } from '../utils/formatDate'
import {
  THINKING_EFFORT_OPTIONS,
  thinkingEffortOptionLabel,
  thinkingUserSelectableFromModel,
  withThinkingWriteFields,
} from '../utils/thinkingLevels'

const { confirm, toast } = useDialog()
const modelsStore = useModelsStore()
const authStore = useAuthStore()
const showModal = ref(false)
const editingId = ref(null)
const purgingId = ref(null)
const settingPrimaryId = ref(null)
const settingImagePrimaryId = ref(null)
const settingSlidesPrimaryId = ref(null)
const settingAsrPrimaryId = ref(null)
const settingEmbedId = ref(null)
// P4.6 — 整批帶入 modal 狀態
const showImportModal = ref(false)
const importSourceId = ref(null)
const importing = ref(false)
const importResult = ref(null)
const activatingCreated = ref(false)
// P4.6b — 端點位址設定授權
const canSetEndpointAddress = ref(false)
const endpointAuthors = ref([])
const authorCandidateUsers = ref([])
const grantUserId = ref(null)
const granting = ref(false)
const revokingId = ref(null)
// Slice 6b — 每列一個「測試連線」狀態：testingId 顯示 spinner；
// testResults[id] 快取最近一次探測的延遲標籤（五態 badge 由 refetch 後的
// health_status 反映）。
const testingId = ref(null)
const testResults = ref({})
const probingThinking = ref(false)

// doc 04 §2 protocol 列舉。openai_compatible = HTTP OpenAI shape；
// triton_grpc = Triton/KServe gRPC（端點填 grpc://host:port）。custom_adapter 已退場。
const PROTOCOL_OPTIONS = [
  { value: 'openai_compatible', label: 'OpenAI 相容（HTTP）' },
  {
    value: 'triton_grpc',
    label: 'Triton / KServe gRPC',
  },
]
const PROTOCOL_LABELS = Object.fromEntries(PROTOCOL_OPTIONS.map(p => [p.value, p.label]))

// SYSTEM-MAP §8 四級分類（無機密 < 營業秘密 < 密 < 機密）。
const CLASSIFICATION_LEVELS = ['無機密', '營業秘密', '密', '機密']

// supports_* → 能力晶片繁中標籤。缺欄位（6a 未落地）時該晶片不顯示。
const CAPABILITY_LABELS = {
  supports_streaming: '串流',
  supports_json_schema: '結構化輸出',
  supports_tools: '工具呼叫',
}

function protocolLabel(p) {
  if (!p) return '—'
  return PROTOCOL_LABELS[p] || p
}
function classificationCeilingLabel(c) {
  return c || '無上限'
}
const THINKING_EFFORT_LABELS = {
  none: 'NONE', off: 'NONE', low: 'low', medium: 'medium', high: 'high', xhigh: 'xhigh', max: 'max',
}
function thinkingEffortChip(value) {
  if (!value || value === 'default') return null
  const label = THINKING_EFFORT_LABELS[value] || value
  return `thinking: ${label}`
}
function normalizeThinkingEffort(value) {
  if (!value || value === 'default') return 'none'
  if (value === 'off') return 'none'
  return value
}
function parseOptionalNumber(raw) {
  if (raw === '' || raw == null) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}
function parseOptionalInteger(raw) {
  const n = parseOptionalNumber(raw)
  if (n == null) return null
  const i = Math.trunc(n)
  return i > 0 ? i : null
}
function capabilityChips(model) {
  return Object.entries(CAPABILITY_LABELS)
    .filter(([key]) => model[key])
    .map(([, label]) => label)
}

const defaultForm = () => ({
  name: '', display_name: '', model_type: 'llm', endpoint_url: '',
  api_version: 'v1', description: '', context_window: null, base_model_id: null,
  // Default true matches backend ModelCreate schema — new registrations are
  // expected to land on the anila-models-net cross-stack docker network.
  // Admin can untick for an external on-prem LAN endpoint.
  is_internal: true,
  // Slice 6b — model gateway governance。protocol 預設 openai_compatible;
  // classification_ceiling null = 無上限;api_key 為 write-only（留空不覆蓋）。
  protocol: 'openai_compatible', classification_ceiling: null, api_key: '',
  router_enabled: false,
  thinking_effort: 'none', thinking_levels_supported: null,
  thinking_user_selectable: true,
  temperature: null, top_p: null,
  presence_penalty: null, max_tokens: null,
})
const form = ref(defaultForm())
const routerGrants = ref([])
const grantsLoadState = ref("ready")
const departments = ref([])
const accessGroups = ref([])
const departmentChoices = computed(() => departmentOptions(departments.value))
function serializeRouterGrants() {
  return routerGrants.value.map((g) => ({
    scope_type: g.scope_type,
    department_id: g.scope_type === "department" ? Number(g.department_id) || null : null,
    group_id: g.scope_type === "group" ? Number(g.group_id) || null : null,
    user_id: g.scope_type === "user" ? Number(g.user_id) || null : null,
    include_descendants: !!g.include_descendants,
    expires_at: g.expires_at || null,
  })).filter((g) => g.scope_type === "all" || g.department_id || g.group_id || g.user_id)
}
function addRouterGrant() { routerGrants.value.push({ scope_type: "all", department_id: null, group_id: null, user_id: null, include_descendants: false, expires_at: null }) }

async function loadAudienceOptions() {
  try {
    const [deptRes, groupRes] = await Promise.all([listDepartments(), listModelAccessGroups()])
    departments.value = deptRes.data || []
    accessGroups.value = groupRes.data || []
  } catch {
    departments.value = []
    accessGroups.value = []
  }
}

function pickGrantUser(g, user) {
  g.user_id = user.id
  g.username = user.username
}

const baseModelOptions = computed(() =>
  modelsStore.models.filter(m =>
    m.model_type !== 'agent' && m.is_active && m.id !== editingId.value
  )
)

// Sentinels returned by backend when endpoint_url is redacted from viewers
// who may not see addresses. Keep in sync with endpoint_author_service.
//   <owner-only>  — generic redaction (external endpoint)
//   <internal>    — additional hint: row lives on anila-models-net
const ENDPOINT_REDACTED = '<owner-only>'
const ENDPOINT_INTERNAL = '<internal>'

// Group import sources by visible endpoint_url. Designated viewers / owner
// see the real address so same-host rows collapse to one option. Undesignated
// viewers see a sentinel and fall back to per-row id: keys (they have no
// business grouping by a property they may not see — SYSTEM-MAP §6).
const importEndpointOptions = computed(() => {
  const seen = new Set()
  const opts = []
  for (const m of modelsStore.models) {
    const isRedacted =
      m.endpoint_url === ENDPOINT_REDACTED || m.endpoint_url === ENDPOINT_INTERNAL
    const key = isRedacted ? `id:${m.id}` : (m.endpoint_url || `id:${m.id}`)
    if (seen.has(key)) continue
    seen.add(key)
    const urlLabel = isRedacted
      ? `${m.display_name}（${m.endpoint_url}）`
      : m.endpoint_url
    opts.push({
      sourceModelId: m.id,
      label: `${urlLabel} · 代表列 ${m.name}`,
    })
  }
  return opts
})

const grantableAuthors = computed(() => {
  const activeIds = new Set(endpointAuthors.value.map(g => g.user_id))
  return authorCandidateUsers.value.filter(u => !activeIds.has(u.id))
})

async function loadEndpointAuthorState() {
  try {
    const { data } = await getMyEndpointAuthorStatus()
    canSetEndpointAddress.value = !!data?.can_set_endpoint_address
  } catch {
    canSetEndpointAddress.value = !!authStore.isOwner
  }
  if (!authStore.isOwner) return
  try {
    const [{ data: grants }, { data: users }] = await Promise.all([
      listEndpointAuthors(),
      listUsers(),
    ])
    endpointAuthors.value = Array.isArray(grants) ? grants : []
    authorCandidateUsers.value = (Array.isArray(users) ? users : []).filter(
      u => (u.role === 'developer' || u.role === 'admin') && u.is_active !== false,
    )
  } catch (e) {
    toast(extractError(e, '無法載入端點位址授權清單'), { tone: 'error' })
  }
}

async function handleGrantAuthor() {
  if (!grantUserId.value || granting.value) return
  granting.value = true
  try {
    await grantEndpointAuthor(grantUserId.value)
    grantUserId.value = null
    toast('已授予端點位址設定權限', { tone: 'success' })
    await loadEndpointAuthorState()
  } catch (e) {
    toast(extractError(e, '授予失敗'), { tone: 'error' })
  } finally {
    granting.value = false
  }
}

async function handleRevokeAuthor(grant) {
  if (!grant || revokingId.value === grant.id) return
  if (!(await confirm({
    message: `撤銷「${grant.username || grant.user_id}」的端點位址設定權限？立即生效。`,
    confirmText: '撤銷',
    danger: true,
  }))) return
  revokingId.value = grant.id
  try {
    await revokeEndpointAuthor(grant.id)
    toast('已撤銷端點位址設定權限', { tone: 'success' })
    await loadEndpointAuthorState()
  } catch (e) {
    toast(extractError(e, '撤銷失敗'), { tone: 'error' })
  } finally {
    revokingId.value = null
  }
}

// KPI 以正規化五態計數，兼容舊值（online/connecting/offline）與新值。
const healthyCount = computed(() => modelsStore.models.filter(m => normalizeHealth(m.health_status) === 'healthy').length)
const degradedCount = computed(() => modelsStore.models.filter(m => normalizeHealth(m.health_status) === 'degraded').length)
const unhealthyCount = computed(() => modelsStore.models.filter(m => normalizeHealth(m.health_status) === 'unhealthy').length)
const tableColspan = computed(() => (authStore.isAdmin || canSetEndpointAddress.value) ? 10 : 9)

onMounted(() => {
  modelsStore.fetchModels()
  loadEndpointAuthorState()
  loadAudienceOptions()
})

function openCreateModal() { editingId.value = null; form.value = defaultForm(); routerGrants.value = []; grantsLoadState.value = "ready"; showModal.value = true }
function openImportModal() {
  importSourceId.value = null
  importResult.value = null
  showImportModal.value = true
}
function closeImportModal() {
  showImportModal.value = false
  importing.value = false
}
async function handleImport() {
  if (!importSourceId.value || importing.value) return
  const source = modelsStore.models.find(m => m.id === importSourceId.value)
  const label = source
    ? (source.endpoint_url === ENDPOINT_REDACTED || source.endpoint_url === ENDPOINT_INTERNAL
      ? source.display_name
      : source.endpoint_url)
    : String(importSourceId.value)
  if (!(await confirm({
    message: `自「${label}」整批帶入上游模型清單？已存在的名稱會保留本機設定，不會覆寫。`,
    confirmText: '開始帶入',
  }))) return
  importing.value = true
  importResult.value = null
  try {
    const data = await modelsStore.importFromEndpoint(importSourceId.value)
    importResult.value = data
    const trunc = data.truncated
      ? ` · 尚有 ${data.truncated} 筆未帶入，可再次執行以繼續`
      : ''
    toast(
      `整批帶入完成 · 新增 ${data.created} · 已存在 ${data.already_existed} · 略過 ${data.skipped}${trunc}`,
      { tone: (data.skipped || data.truncated) ? 'warn' : 'success' },
    )
  } catch (e) {
    toast(extractError(e, '整批帶入失敗'), { tone: 'error' })
  } finally {
    importing.value = false
  }
}

async function handleActivateCreated() {
  if (!importSourceId.value || !importResult.value?.created_names?.length || activatingCreated.value) return
  const n = importResult.value.created_names.length
  if (!(await confirm({
    message: `啟用本次新增的 ${n} 個模型？啟用後即可被路由選用；請確認分類上限與能力欄位。`,
    confirmText: '啟用',
  }))) return
  activatingCreated.value = true
  try {
    const data = await modelsStore.activateCreated(
      importSourceId.value,
      importResult.value.created_names,
    )
    toast(
      `已啟用 ${data.activated} 個模型` +
        (data.already_active ? ` · 原本已啟用 ${data.already_active}` : '') +
        (data.wrong_endpoint || data.not_found
          ? ` · 未處理 ${(data.wrong_endpoint || 0) + (data.not_found || 0)}`
          : ''),
      { tone: 'success' },
    )
  } catch (e) {
    toast(extractError(e, '整批啟用失敗'), { tone: 'error' })
  } finally {
    activatingCreated.value = false
  }
}
async function openEditModal(model, opts = {}) {
  editingId.value = model.id
  // Drop the sentinel before populating the form — otherwise saving
  // would PUT the literal "<owner-only>" string back to backend and
  // corrupt the registered endpoint. Non-owner admins see a placeholder
  // hint instead and the field is disabled.
  // Both sentinels (<owner-only> / <internal>) must be stripped before
  // populating the form — otherwise saving would PUT the literal string
  // back. Non-owner admins see a placeholder + disabled field.
  const isRedacted =
    model.endpoint_url === ENDPOINT_REDACTED ||
    model.endpoint_url === ENDPOINT_INTERNAL
  const endpointUrl = isRedacted ? '' : model.endpoint_url
  form.value = {
    name: model.name, display_name: model.display_name,
    model_type: model.model_type, endpoint_url: endpointUrl,
    api_version: model.api_version, description: model.description || '',
    context_window: model.context_window, base_model_id: model.base_model_id || null,
    is_internal: !!model.is_internal,
    // 防禦性：6a 未落地時欄位可能為 undefined，給合理預設。api_key 為 write-only,
    // 永不從後端回顯（後端也不回傳明文金鑰），故一律留空。
    protocol: model.protocol || 'openai_compatible',
    classification_ceiling: model.classification_ceiling ?? null,
    api_key: '',
    router_enabled: !!model.router_enabled,
    thinking_effort: normalizeThinkingEffort(model.thinking_effort),
    thinking_levels_supported: Array.isArray(model.thinking_levels_supported)
      ? model.thinking_levels_supported
      : null,
    thinking_user_selectable: thinkingUserSelectableFromModel(model),
    temperature: model.temperature ?? null,
    top_p: model.top_p ?? null,
    presence_penalty: model.presence_penalty ?? null,
    max_tokens: model.max_tokens ?? null,
  }
  routerGrants.value = []
  grantsLoadState.value = 'pending'
  try {
    const { data } = await listRouterGrants(model.id)
    const loaded = grantsLoadResult(true, data)
    grantsLoadState.value = loaded.state
    routerGrants.value = loaded.grants.map((g) => ({
      scope_type: g.scope_type,
      department_id: g.department_id,
      group_id: g.group_id,
      user_id: g.user_id,
      include_descendants: !!g.include_descendants,
      expires_at: g.expires_at || null,
      username: g.username || '',
    }))
  } catch (e) {
    const loaded = grantsLoadResult(false, [])
    grantsLoadState.value = loaded.state
    toast(extractError(e, '授權清單載入失敗，儲存時不會覆蓋授權'), { tone: 'error' })
  }
  showModal.value = true
  if (opts.focusGrants) {
    await nextTick()
    document.getElementById("model-grant-editor")?.scrollIntoView({ block: "center" })
  }
}

function canEditAudience(model) {
  return authStore.isAdmin && model.router_enabled && model.name !== "anila-router" && (model.model_type === "llm" || model.model_type === "vlm")
}

function hasMoreActions(model) {
  if (!authStore.isAdmin) return false
  if (model.name === "anila-router") return authStore.isOwner
  return true
}

// P4.6b: 新建一律需可設定位址；編輯時無權者不得送出／改寫 endpoint_url。
const endpointFieldLocked = computed(() => !canSetEndpointAddress.value)
// 獲授權開發者僅能改位址；管理員／擁有者維持完整更新表單。
const addressOnlyEditor = computed(
  () => !!editingId.value && canSetEndpointAddress.value && !authStore.isAdmin,
)
const endpointUrlHint = computed(() => {
  if (endpointFieldLocked.value) return '🔒 僅擁有者與獲授權開發者可變更端點位址'
  if (form.value.protocol === 'triton_grpc') {
    return 'Triton gRPC：填 grpc://host:port 或 grpcs://host:port（不要加 /v1 路徑）；cleartext grpc 需 ANILA_ALLOW_GRPC_ENDPOINT=1'
  }
  if (form.value.model_type === 'asr') {
    return 'decoder 根位址（呼叫 {base}/transcribe）；勿加 /v1'
  }
  if (addressOnlyEditor.value) return '獲授權開發者僅可變更端點位址'
  return '登錄／變更端點位址需擁有者或獲授權開發者身分'
})
const endpointUrlPlaceholder = computed(() => {
  if (endpointFieldLocked.value) return '— 無權設定位址 —'
  if (form.value.protocol === 'triton_grpc') return 'grpc://172.16.120.35:9001'
  if (form.value.model_type === 'asr') return 'http://asr-decoder:9000'
  return 'http://gemma4:8000/v1'
})

// Phase 2 模型 stack 解耦 — SSRF guard 對 single-label / internal-zone
// hostname 回 typed 400 (detail 是 dict 不是 string)。前端在 catch 偵測
// 到 code === "untrusted_host" 時跳 confirm modal,owner 一鍵把該
// hostname 加進 trusted_hosts 後重試 — 不必離開「register model」流程
// 去切到另一個分頁手動操作。
const untrustedHostPrompt = ref(null)   // { host, message, hint, retryPayload, retryMode }

// 由 form 組出送出 payload — register / update / trust-retry 三處共用，
// 避免治理欄位（api_key write-only、base_model_id、locked endpoint）漏處理。
function buildModelPayload() {
  // Designated non-admin authors: address only (matches backend gate).
  if (addressOnlyEditor.value) {
    return { endpoint_url: form.value.endpoint_url }
  }
  const payload = withThinkingWriteFields({ ...form.value }, form.value)
  if (payload.model_type !== 'agent') payload.base_model_id = null
  // Don't ship endpoint_url back when the field was locked (admin editing a
  // row whose URL they couldn't see). Backend would accept the empty string
  // and overwrite the real endpoint with junk.
  if (endpointFieldLocked.value) delete payload.endpoint_url
  // api_key 為 write-only：留空 = 沿用現值或全域金鑰,絕不送空字串把既有金鑰清掉。
  if (!payload.api_key) delete payload.api_key
  // triton_grpc 從不送金鑰。欄位在該協定下不顯示,但使用者可能先在
  // openai_compatible 下打了字再切協定 —— 值還留在 form 裡。不丟掉的話
  // 就是「存了一把永遠不會被用到的金鑰」,比不顯示欄位更誤導。
  if (payload.protocol === 'triton_grpc') delete payload.api_key
  for (const key of ['thinking_effort', 'temperature', 'top_p', 'presence_penalty', 'max_tokens']) {
    if (payload[key] === '' || payload[key] === undefined || Number.isNaN(payload[key])) {
      payload[key] = null
    }
  }
  return payload
}

// 儲存時後端會向端點探測一次 thinking_effort。被拒絕的等級是 422（走既有
// 錯誤顯示路徑），這裡只處理「探不到」：等級照存，但沒人驗證過。
function noticeThinkingProbe(saved) {
  if (saved?.thinking_probe?.status !== 'unreachable') return
  const why = saved.thinking_probe.detail ? `（${saved.thinking_probe.detail}）` : ''
  toast(`模型目前連不上，thinking_effort 未經探測${why}`, { tone: 'warn' })
}

function applyThinkingProbeResult(saved) {
  if (!saved) return
  form.value.thinking_levels_supported = Array.isArray(saved.thinking_levels_supported)
    ? saved.thinking_levels_supported
    : null
  if (typeof saved.thinking_user_selectable === 'boolean') {
    form.value.thinking_user_selectable = saved.thinking_user_selectable
  }
}

async function handleProbeThinking() {
  if (!editingId.value || probingThinking.value) return
  probingThinking.value = true
  try {
    const data = await modelsStore.probeThinking(editingId.value)
    applyThinkingProbeResult(data)
    if (data?.thinking_levels_supported == null) {
      toast('已重跑探測，此端點仍無法取得支援等級', { tone: 'warn' })
    } else {
      toast('已更新支援等級', { tone: 'success' })
    }
  } catch (e) {
    toast(extractError(e, '重新探測失敗'), { tone: 'error' })
  } finally {
    probingThinking.value = false
  }
}

async function handleSubmit() {
  try {
    const payload = buildModelPayload()
    if (editingId.value) {
      const { name, ...updateData } = payload
      noticeThinkingProbe(await modelsStore.update(editingId.value, updateData))
      if (updateData.router_enabled) {
        if (!canReplaceRouterGrants(grantsLoadState.value)) {
          toast('授權清單未成功載入，已保存模型但未變更授權', { tone: 'warn' })
        } else {
          await replaceRouterGrants(editingId.value, serializeRouterGrants())
        }
      }
    } else {
      const created = await modelsStore.create(payload)
      noticeThinkingProbe(created)
      if (payload.router_enabled && created && created.id) await replaceRouterGrants(created.id, serializeRouterGrants())
    }
    showModal.value = false
  } catch (e) {
    const detail = getRawDetail(e)
    // Typed 400 with code "untrusted_host" → show confirm modal so the
    // owner can promote the host to trusted_hosts and retry without
    // leaving this page. Plain-string detail (loopback / metadata /
    // scheme failures) falls through to the existing alert path —
    // those aren't fixable by adding to trust list.
    if (
      detail &&
      typeof detail === 'object' &&
      detail.code === 'untrusted_host' &&
      detail.host &&
      authStore.isOwner
    ) {
      const payload = buildModelPayload()
      untrustedHostPrompt.value = {
        host: detail.host,
        message: detail.message || '',
        hint: detail.hint || '',
        retryPayload: payload,
        retryMode: editingId.value ? 'update' : 'create',
        retryId: editingId.value,
      }
      return
    }
    const msg = typeof detail === 'string'
      ? detail
      : (detail?.message || '操作失敗')
    toast(msg, { tone: 'error' })
  }
}

async function confirmTrustAndRetry() {
  const prompt = untrustedHostPrompt.value
  if (!prompt) return
  try {
    // Lazy import so loading ModelsView for non-owner viewers doesn't
    // even pull the trusted-hosts API client.
    const { createTrustedHost } = await import('../api/trustedHosts')
    await createTrustedHost({
      host: prompt.host,
      note: `auto-added via /models register on ${new Date().toISOString()}`,
    })
    // Retry the original submit. cache TTL is 30s but the service
    // invalidates on mutation, so the retry should see the new host
    // immediately on the same CSP worker. Cross-worker eventual
    // consistency: at worst the admin sees the same error again and
    // can retry once more.
    const { retryPayload, retryMode, retryId } = prompt
    if (retryMode === 'update') {
      const { name, ...updateData } = retryPayload
      noticeThinkingProbe(await modelsStore.update(retryId, updateData))
    } else {
      noticeThinkingProbe(await modelsStore.create(retryPayload))
    }
    untrustedHostPrompt.value = null
    showModal.value = false
  } catch (e) {
    toast(extractError(e, '重試失敗'), { tone: 'error' })
  }
}

function cancelTrustPrompt() {
  untrustedHostPrompt.value = null
}

// Slice 6b — 主動探測連線。POST /test → 五態 + 延遲。防禦性讀取欄位
// （health_status / status、latency_ms / latencyMs），並依五態決定 toast 語氣。
async function handleTest(model) {
  if (testingId.value === model.id) return
  testingId.value = model.id
  try {
    const result = await modelsStore.test(model.id)
    const status = result?.health_status ?? result?.status
    const latency = result?.latency_ms ?? result?.latencyMs ?? null
    testResults.value = {
      ...testResults.value,
      [model.id]: { latencyLabel: latency != null ? `延遲 ${latency} ms` : '' },
    }
    const ok = normalizeHealth(status) === 'healthy'
    const latencyTxt = latency != null ? `（${latency} ms）` : ''
    toast(`測試連線 → ${healthLabel(status)}${latencyTxt}`, { tone: ok ? 'success' : 'error' })
  } catch (e) {
    toast(extractError(e, '測試連線失敗'), { tone: 'error' })
  } finally {
    testingId.value = null
  }
}

async function handleSetPrimary(id) {
  settingPrimaryId.value = id
  try { await modelsStore.setPrimary(id) }
  catch (e) { toast(extractError(e, '設定主要失敗'), { tone: 'error' }) }
  finally { settingPrimaryId.value = null }
}
async function handleUnsetPrimary(id) {
  if (!(await confirm({ message: '取消全院預設？在你指定新的主要模型前，ANILA Router 將沒有主要 LLM。', confirmText: '取消全院預設', danger: true }))) return
  settingPrimaryId.value = id
  try { await modelsStore.unsetPrimary(id) }
  catch (e) { toast(extractError(e, '取消全院預設失敗'), { tone: 'error' }) }
  finally { settingPrimaryId.value = null }
}
async function handleSetImagePrimary(id) {
  settingImagePrimaryId.value = id
  try { await modelsStore.setImagePrimary(id) }
  catch (e) { toast(extractError(e, '設定主圖像模型失敗'), { tone: 'error' }) }
  finally { settingImagePrimaryId.value = null }
}
async function handleSetSlidesPrimary(id) {
  settingSlidesPrimaryId.value = id
  try { await modelsStore.setSlidesPrimary(id) }
  catch (e) { toast(extractError(e, '設定主簡報模型失敗'), { tone: 'error' }) }
  finally { settingSlidesPrimaryId.value = null }
}
async function handleUnsetSlidesPrimary(id) {
  settingSlidesPrimaryId.value = id
  try { await modelsStore.unsetSlidesPrimary(id) }
  catch (e) { toast(extractError(e, '取消主簡報模型失敗'), { tone: 'error' }) }
  finally { settingSlidesPrimaryId.value = null }
}
async function handleUnsetImagePrimary(id) {
  if (!(await confirm({ message: '取消主圖像模型？在你指定新的主圖像模型前，flux2-dev-agent / anila-studio 將 fallback 使用環境變數設定的端點。', confirmText: '取消主圖像', danger: true }))) return
  settingImagePrimaryId.value = id
  try { await modelsStore.unsetImagePrimary(id) }
  catch (e) { toast(extractError(e, '取消主圖像模型失敗'), { tone: 'error' }) }
  finally { settingImagePrimaryId.value = null }
}
async function handleSetAsrPrimary(id) {
  settingAsrPrimaryId.value = id
  try {
    await modelsStore.setAsrPrimary(id)
    toast('已設為主語音辨識。新主機須部署相同 ASR_DECODER_TOKEN；位址請為 decoder 根路徑（非 /v1）。', { tone: 'ok' })
  } catch (e) { toast(extractError(e, '設定主語音辨識失敗'), { tone: 'error' }) }
  finally { settingAsrPrimaryId.value = null }
}
async function handleUnsetAsrPrimary(id) {
  if (!(await confirm({ message: '取消主語音辨識？在你指定新的主語音模型前，asr-gateway 將改用環境變數 ASR_DECODE_URL。共享密鑰 ASR_DECODER_TOKEN 仍只在環境變數，不會寫進模型登錄。', confirmText: '取消主語音', danger: true }))) return
  settingAsrPrimaryId.value = id
  try { await modelsStore.unsetAsrPrimary(id) }
  catch (e) { toast(extractError(e, '取消主語音辨識失敗'), { tone: 'error' }) }
  finally { settingAsrPrimaryId.value = null }
}
function platformEmbedTitle(model) {
  const dim = model.embedding_native_dim
  if (!dim) return '平台主 embedding（記憶／新建知識庫／ingestion-worker）'
  if (dim > 4000) {
    return `平台主 embedding · 原生 ${dim} 維（寫入時截斷至 4000；pgvector halfvec HNSW 上限）`
  }
  if (dim < 4000) {
    return `平台主 embedding · 原生 ${dim} 維（寫入時補零至 4000）`
  }
  return `平台主 embedding · 原生 ${dim} 維`
}
async function handleSetPlatformEmbed(id) {
  // 換模型＝把舊索引整批作廢。本畫面對停用／取消主模型／永久刪除都先 confirm，
  // 唯獨後果最大的這個動作沒有；先攔一次（同一個模型重新確認則不攔）。
  const gate = designationConfirm(modelsStore.models, id)
  if (gate.needed && !(await confirm({
    message: gate.message, confirmText: gate.confirmText, danger: gate.danger,
  }))) return
  settingEmbedId.value = id
  try {
    const data = await modelsStore.setPlatformEmbed(id)
    // 嚴重度排序在 designationToast 裡；索引不一致絕不能落回綠色成功提示。
    const notice = designationToast(data)
    if (notice) toast(notice.message, { tone: notice.tone, duration: notice.duration })
  } catch (e) {
    toast(extractError(e, '設定主 embedding 失敗'), { tone: 'error' })
  } finally {
    settingEmbedId.value = null
  }
}
async function handleUnsetPlatformEmbed(id) {
  if (!(await confirm({
    message: '取消平台主 embedding？記憶與新建知識庫將改用第一個啟用中的 embedding 模型（若有）。',
    confirmText: '取消主 embedding',
    danger: true,
  }))) return
  settingEmbedId.value = id
  try { await modelsStore.unsetPlatformEmbed(id) }
  catch (e) { toast(extractError(e, '取消主 embedding 失敗'), { tone: 'error' }) }
  finally { settingEmbedId.value = null }
}
async function handleDeactivate(id) {
  if (await confirm({ message: '停用此模型？之後可透過該列的「啟用」按鈕重新啟用。', confirmText: '停用', danger: true })) {
    await modelsStore.remove(id)
  }
}
async function handleActivate(id) {
  try { await modelsStore.activate(id) }
  catch (e) { toast(extractError(e, '啟用失敗'), { tone: 'error' }) }
}
async function handlePurge(model) {
  if (!model || purgingId.value === model.id) return
  if (!(await confirm({ message: `永久刪除「${model.display_name}」？不可復原。若有用量紀錄或其他模型引用則會被拒絕。`, confirmText: '永久刪除', danger: true }))) return
  purgingId.value = model.id
  try { await modelsStore.purge(model.id) }
  catch (e) { toast(extractError(e, '清除失敗'), { tone: 'error' }) }
  finally { purgingId.value = null }
}
</script>

<style scoped>
.page { display: flex; flex-direction: column; gap: var(--gap-4); padding-bottom: var(--gap-8); }

.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: var(--gap-3); flex-wrap: wrap; }
.page-head__title { font-size: var(--t-2xl); font-weight: 600; letter-spacing: var(--tracking-tight); margin: 4px 0 2px; }
.page-head__sub { font-size: var(--t-xs); color: var(--c-fg-3); }
.page-head__actions { display: inline-flex; align-items: center; gap: var(--gap-2); flex-wrap: wrap; }

.author-grant { display: flex; flex-direction: column; gap: var(--gap-3); padding: var(--gap-3); }
.author-grant__form {
  max-width: 36rem;
}
.author-grant__row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.author-grant__row .term-select { flex: 1; min-width: 0; }
.author-grant__row :deep(.term-btn) { flex-shrink: 0; min-width: 4.5rem; }
.author-grant__row :deep(.term-btn:disabled) {
  background: var(--c-accent) !important;
  border-color: var(--c-accent);
  color: var(--c-accent-fg);
  opacity: 0.55;
}
.author-grant :deep(.term-empty) {
  text-align: left;
  padding: 8px 0 0;
}
.author-grant__table { margin-top: var(--gap-2); }

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--gap-3); }
@media (max-width: 800px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }

.import-hint { margin: 0; font-size: var(--t-sm); color: var(--c-fg-2); line-height: 1.5; }
.import-hint code {
  font-family: var(--font-mono); font-size: var(--t-2xs);
  background: var(--c-bg); border: var(--border-w) solid var(--c-border); padding: 1px 6px;
}
.import-result { display: flex; flex-direction: column; gap: var(--gap-2); }
.import-result__summary { margin: 0; font-size: var(--t-sm); color: var(--c-fg-1); font-weight: 500; }
.import-result__list {
  margin: 0; padding-left: 1.2em; font-size: var(--t-xs); color: var(--c-fg-2);
  max-height: 180px; overflow: auto;
}
.import-result__list--skip { color: var(--c-warn, #9a6700); }

.field-note {
  margin: -4px 0 12px;
  font-size: var(--t-2xs);
  color: var(--c-fg-2);
  line-height: 1.45;
}
.field-note code {
  font-family: var(--font-mono);
  font-size: var(--t-2xs);
  color: var(--c-accent);
}
.cell-strong { color: var(--c-fg-1); font-weight: 500; }
.cell-meta { color: var(--c-fg-3); font-size: var(--t-2xs); }
.cell-meta--internal { color: var(--c-ok, #2ea043); }
.cell-base { color: var(--c-info); font-size: var(--t-2xs); margin-top: 2px; }
.internal-lock {
  display: inline-block;
  margin-right: 4px;
  font-size: 0.85em;
  color: var(--c-ok, #2ea043);
  cursor: help;
}
.internal-checkbox {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: var(--t-xs);
  color: var(--c-fg-2);
  cursor: pointer;
  user-select: none;
}
.internal-checkbox input[type="checkbox"] {
  cursor: pointer;
}
.trust-prompt { display: flex; flex-direction: column; gap: var(--gap-3); }
.trust-prompt__hint { color: var(--c-fg-2); font-size: var(--t-sm); margin: 0; }
.trust-prompt__cta { color: var(--c-fg-1); font-size: var(--t-sm); margin: 0; }
.trust-prompt__cta code {
  font-family: var(--font-mono); background: var(--c-bg);
  border: var(--border-w) solid var(--c-border); padding: 1px 6px;
  font-size: var(--t-2xs); color: var(--c-accent);
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

.cell-latency { margin-top: 3px; }

/* Slice 6b — 治理能力晶片 + 模型金鑰指示（name cell meta）。 */
.cell-caps { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.cap-chip { font-size: var(--t-2xs); }
.cap-key {
  display: inline-flex;
  align-items: center;
  font-size: var(--t-2xs);
  letter-spacing: 0.02em;
  padding: 1px 6px;
  border: var(--border-w) solid var(--c-border);
  border-radius: var(--r-soft);
}
.cap-key--set { color: var(--c-ok, #2ea043); border-color: var(--c-ok, #2ea043); background: var(--c-ok-soft); }
.cap-key--global { color: var(--c-fg-3); }

.primary-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: var(--t-2xs);
  color: var(--c-warn);
  border: var(--border-w) solid var(--c-warn);
  padding: 1px 6px;
  background: var(--c-warn-soft);
  letter-spacing: 0.04em;
}
.primary-pill--embed {
  margin-left: 4px;
  color: var(--c-fg-2);
  border-color: var(--c-border-strong);
  background: transparent;
}

.audience-link {
  margin-top: 6px;
}
.audience-link .term-action {
  font-weight: 600;
}

.row-actions { display: inline-flex; align-items: center; gap: 6px; font-size: var(--t-xs); flex-wrap: wrap; }
.row-actions__sep { color: var(--c-border-strong); }

.form-grid { display: flex; flex-direction: column; gap: var(--gap-3); }
.form-row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: var(--gap-3); }
.form-section {
  display: flex;
  flex-direction: column;
  gap: var(--gap-3);
  padding-top: var(--gap-2);
  border-top: var(--border-w) solid var(--c-border);
}
.form-section__title {
  margin: 0;
  font-size: var(--t-sm);
  font-weight: 600;
  color: var(--c-fg-1);
}
.think-chip { color: var(--c-fg-2); }
.thinking-levels-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.grant-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 8px 0;
}
.grant-user { display: flex; flex-direction: column; gap: 4px; min-width: 14rem; }
</style>
