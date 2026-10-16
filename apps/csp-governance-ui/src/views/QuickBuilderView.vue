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
    <details class="qb-start-guide">
      <summary>三步開始：選範本 → 補設定、接步驟 → 儲存後試跑</summary>
      <ol><li>選接近工作情境的範本，再調整流程。</li><li>點選步驟看用途、輸入輸出與例子；展開設定提醒補缺件。</li><li>儲存後用一個實際問題試跑，確認結果再送主管審核。</li></ol>
    </details>
    <fieldset class="qb-content" :disabled="loading || identityChanged || !actorReady">
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
    <details v-if="chosenTemplate" class="qb-template-notes"><summary>範本說明與套用後的設定</summary><p>{{ chosenTemplate.description }}</p><p>{{ (chosenTemplate.configuration_notes || chosenTemplate.required_settings || []).join?.('；') || chosenTemplate.configuration_notes }}</p></details>
    <form class="qb-metadata" novalidate @submit.prevent="save">
      <details ref="metadataDetails" class="qb-metadata-details">
        <summary>助手基本資料 <span>{{ form.name || '尚未命名' }} · {{ selectedModelName }} · {{ form.classification_level }} · {{ scopeName }}</span><small>展開修改名稱、模型與開放範圍</small></summary>
      <fieldset :disabled="!editable || busy">
        <legend>助手設定</legend>
        <div class="qb-meta-grid">
          <label>
            助手名稱
            <input v-model="form.name" required maxlength="80" placeholder="例如：規章問答助手">
          </label>
          <label>
            平台模型（必填）
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
      </details>
      <div class="qb-actions qb-save-actions">
        <button type="submit" class="primary" :disabled="busy || !editable || !dirty">{{ busy ? '處理中…' : '儲存新版本' }}</button>
        <span>{{ dirty ? '有尚未儲存的變更' : '目前內容已儲存' }}</span>
      </div>
    </form>
    <div class="qb-tabs" aria-label="助手工作區">
      <button v-for="tab in tabs" :key="tab.id" :aria-pressed="panel === tab.id" :disabled="testing" @click="panel = tab.id">{{ tab.label }}</button>
    </div>
    <template v-if="panel === 'canvas'">
      <details v-if="setupIssues.length" class="qb-setup-check">
        <summary>還有 {{ setupIssues.length }} 項設定待確認 <small>展開清單並前往設定</small></summary>
        <p class="hint">這是基本設定提醒；執行時仍會檢查模型、來源權限與流程。</p>
        <ul><li v-for="(issue, index) in setupIssues" :key="`${issue.key}-${index}`">{{ issue.message }} <button @click="goToSetting(issue)">前往設定</button></li></ul>
      </details>
      <p v-else class="qb-setup-ready">基本設定已填妥；儲存後可到「測試對話」試跑。</p>
      <div class="qb-workspace" :class="{'qb-workspace--config-closed': !configOpen}">
        <aside class="qb-palette" aria-label="模組庫">
          <h2>模組庫</h2>
          <label class="qb-module-search">找需要的模組<input v-model="moduleQuery" type="search" placeholder="例如：文件、判斷、回答"></label>
          <p class="hint">按一下加入，也可以拖入畫布。</p>
          <section v-for="group in visibleModuleGroups" :key="group.id" class="qb-module-group">
            <h3>{{ group.name }}</h3>
            <button v-for="m in group.modules" :key="m.kind" :data-module-kind="m.kind" :disabled="!editable || busy" draggable="true" :title="moduleGuide(m.kind).example" @dragstart="paletteDrag($event, m.kind)" @click="addNode(m.kind)">
              <strong>{{ moduleGuide(m.kind).title }}</strong>
              <small>{{ moduleGuide(m.kind).description }}</small>
              <small>輸入：{{ moduleGuide(m.kind).input }}<br>輸出：{{ moduleGuide(m.kind).output }}</small>
            </button>
          </section>
          <p v-if="!visibleModuleGroups.length && !visibleTools.length" role="status">找不到符合的模組，試試「文件」或清除搜尋。</p>
          <h3>可直接加入的查詢工具</h3>
          <button v-for="tool in visibleTools" :key="tool.id" :disabled="!editable || busy" draggable="true" @dragstart="paletteDrag($event, 'mcp', tool.id)" @click="addNode('mcp', null, tool.id)">
            {{ tool.name }}
            <small>{{ tool.description }}</small>
          </button>
          <p v-if="!catalog.mcp_tools?.length">尚無可用工具。請聯絡管理員登錄與核准。</p>
        </aside>
        <section class="qb-stage" aria-label="流程編輯區">
          <div class="qb-canvas-tools" role="group" aria-label="畫布檢視控制">
            <strong>流程畫布 <small>{{ form.graph.nodes.length }} 個步驟</small></strong>
            <div class="qb-zoom-tools">
              <button aria-label="縮小畫布" :disabled="zoom <= 0.4" @click="changeZoom(zoom - 0.1)">縮小</button>
              <output aria-label="畫布縮放比例" aria-live="polite">{{ Math.round(zoom * 100) }}%</output>
              <button aria-label="放大畫布" :disabled="zoom >= 1.5" @click="changeZoom(zoom + 0.1)">放大</button>
              <button @click="changeZoom(1)">100%</button>
              <button :aria-pressed="fitMode" @click="fitCanvas">適合畫面</button>
            </div>
            <div class="qb-view-tools">
              <button :disabled="!editable || busy" @click="arrangeNodes">整理排列</button>
              <button :aria-expanded="configOpen" aria-controls="qb-step-settings" @click="configOpen = !configOpen">{{ configOpen ? '收合步驟設定' : '顯示步驟設定' }}</button>
            </div>
          </div>
          <div class="qb-canvas-status" role="status">{{ selected ? `已選取：${selected.label}` : '選取步驟後可調整設定' }}<span v-if="!configOpen">；步驟設定已收合</span>。拖曳模組加入；從出口連到「接到這裡」。<details class="qb-canvas-help"><summary>操作說明</summary><p>拖曳步驟標題可移動；標題取得焦點後可用方向鍵移動。可捲動或按「適合畫面」查看全圖，也可用下方表單連線。</p></details></div>
          <p v-if="sourceNode" class="qb-link-status" role="status">正在從「{{ sourceNode.label }}」{{ sourceNode.kind === 'condition' ? (branch === 'false' ? '不成立' : '成立') + '出口' : '' }}連線。請按下一步的「接到這裡」。<button @click="sourceId = ''; targetId = ''">取消連線</button></p>
          <p v-if="drawnEdges.some(item => !item.geometry)" class="qb-canvas-status">部分步驟重疊或位置太近，連線暫時無法清楚顯示。可按「整理排列」重新排列步驟（GRAPH_LAYOUT）。</p>
          <div ref="canvasViewport" class="qb-canvas-scroll" tabindex="0" aria-label="流程畫布，可捲動查看所有步驟" @dragover.prevent @drop.prevent="dropNode">
            <div class="qb-canvas-size" :style="{width: canvasWidth * zoom + 'px', height: canvasHeight * zoom + 'px'}">
              <div class="qb-canvas" :style="{width: canvasWidth + 'px', height: canvasHeight + 'px', transform: `scale(${zoom})`}">
                <div class="qb-world" :style="{left: -canvasBounds.x + 'px', top: -canvasBounds.y + 'px'}">
                  <svg :width="canvasWidth" :height="canvasHeight" class="qb-lines" aria-hidden="true">
                    <defs>
                      <marker id="qb-arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto">
                        <path d="M0,0 L0,6 L8,3 z" fill="currentColor"/>
                      </marker>
                    </defs>
                    <g v-for="({edge, geometry}, i) in drawnEdges" :key="i" :class="{'qb-edge--false': edge.branch === 'false'}">
                      <path :d="geometry?.path || ''" marker-end="url(#qb-arrow)"/>
                      <g v-if="geometry && edge.branch" class="qb-edge-label" :transform="`translate(${geometry.label.x}, ${geometry.label.y})`">
                        <rect :x="edge.branch === 'false' ? -32 : -24" y="-13" :width="edge.branch === 'false' ? 64 : 48" height="26" rx="4"/>
                        <text text-anchor="middle" dominant-baseline="central">{{ edge.branch === 'true' ? '成立' : '不成立' }}</text>
                      </g>
                    </g>
                  </svg>
                  <article v-for="node in form.graph.nodes" :key="node.id" class="qb-node" :aria-label="`流程步驟：${node.label}`" :class="{selected: selectedId === node.id, 'qb-node--linking': sourceId === node.id}" :style="{left: node.position.x + 'px', top: node.position.y + 'px'}">
                    <div class="qb-node-state">{{ selectedId === node.id ? '已選取' : moduleGuide(node.kind).title }}<span v-if="setupIssues.some(i => i.nodeId === node.id)"> · 待設定</span></div>
                    <button class="qb-node-title" :title="node.label" :aria-label="`選取${node.label}；方向鍵移動`" @click="selectNode(node.id)" @pointerdown="startMove($event, node)" @pointermove="moveNode($event, node)" @pointerup="endMove" @pointercancel="endMove" @keydown="moveKey($event, node)">
                      {{ node.label }}
                    </button>
                    <p v-if="node.kind !== 'condition'" class="qb-node-description">{{ nodeSummary(node) }}</p>
                    <div class="qb-node-io" :title="`輸入：${moduleGuide(node.kind).input} 輸出：${moduleGuide(node.kind).output}`">{{ nodeIO(node.kind).input }} → {{ nodeIO(node.kind).output }}</div>
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
            </div>
          </div>
        </section>
        <aside v-show="configOpen" id="qb-step-settings" class="qb-config" aria-label="步驟設定">
          <div class="qb-config-head"><h2>步驟設定</h2><button aria-label="收合步驟設定面板" @click="configOpen = false">收合</button></div>
          <p v-if="!selected">選取畫布上的步驟，再調整設定。</p>
          <fieldset v-else :disabled="!editable || busy">
            <NodeSettings :key="selected.id" :node="selected" :catalog="catalog" :fields="extractFields"/>
            <button class="danger" @click="deleteNode">移除這個步驟</button>
          </fieldset>
        </aside>
      </div>
      <section class="qb-connections">
        <h2>流程連線</h2>
        <p class="hint">連線表示資料接著交給哪一步。要改接同一出口，先移除原連線，再新增；移除連線不會刪除步驟。</p>
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
          <textarea v-model="message" :disabled="testing" rows="3" required maxlength="8000" placeholder="例如：出差申請需要哪些文件？"/>
        </label>
        <p v-if="dirty || !version" class="hint">請先回到助手設定儲存新版本，再用這個版本試跑。</p>
        <button class="primary" type="submit" :disabled="busy || unresolvedTest || dirty || !version || !message.trim()">{{ testing ? '正在測試…' : '傳送測試' }}</button>
        <button v-if="testing" type="button" @click="stopTest">停止等待</button>
      </form>
      <p v-if="testing" class="qb-test-wait" role="status">問題已送出，正在等待流程回傳結果。目前尚未收到逐步進度，完成後會列出實際執行的步驟。</p>
      <section v-if="trackedTest" class="qb-test-tracking" aria-label="目前試跑追蹤" :data-run-id="trackedTest.run || ''" :data-request-id="trackedTest.request_id || ''" :data-run-status="trackedTest.serverStatus">
        <p><strong>這次試跑：{{ runStatus(trackedTest.serverStatus) }}</strong><span v-if="trackedTest.run"> · 執行編號 {{ trackedTest.run }}</span> · 第 {{ versions.find(v => v.id === trackedTest.version)?.revision || '—' }} 版</p>
        <p v-if="trackedTest.request_id">請求識別碼：{{ trackedTest.request_id }}</p>
        <button type="button" :disabled="checkingTest" @click="queryTracked()">{{ checkingTest ? '正在查詢…' : '查詢這次試跑' }}</button>
        <button v-if="unresolvedTest && !testing" type="button" @click="leaveUnknownTest">結束查詢，準備另一個測試</button>
      </section>
      <button type="button" :disabled="checkingTest || testing || !version" @click="findMyTests">查詢我的試跑</button>
      <ul v-if="recentTests.length" class="qb-test-history" aria-label="本人的試跑紀錄"><li v-for="run in recentTests" :key="run.id"><button type="button" :disabled="testing || checkingTest" @click="selectTestRun(run)">查看執行 {{ run.id }} · 第 {{ run.revision }} 版 · {{ runStatus(run.status) }}</button></li></ul>
      <p v-if="testNotice" class="qb-test-notice" role="status">{{ testNotice }}</p>
      <p v-if="testResult && testedRevision !== version?.revision" class="qb-test-notice">下方是第 {{ testedRevision }} 版的結果，目前選的是第 {{ version?.revision }} 版。修改後請儲存並重新試跑，不能用舊結果確認新內容。</p>
      <TestRunReview v-if="testResult" :result="testResult" :graph="testedGraph" :revision="testedRevision" :question="testedQuestion" @edit="editTestStep"/>

    </section>
    <details v-if="catalog.can_manage_templates || catalog.can_manage_readiness" class="qb-management">
      <summary>範本、文件開放與離線工具管理</summary>
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
    <ReadinessPanel v-if="catalog.can_manage_readiness === true && !identityChanged"/>
    </details>
    </fieldset>
  </div>
</template>
<script setup>
import { computed, nextTick, onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { onBeforeRouteLeave } from 'vue-router';
import { PageHead } from '../components/cli';
import { extractError, errorType } from '../api/errors';
import * as api from '../api/quickBuilder';
import NodeSettings from '../quickBuilder/NodeSettings.vue';
import ReadinessPanel from '../quickBuilder/ReadinessPanel.vue';
import TestRunReview from '../quickBuilder/TestRunReview.vue';
import { moduleGuide, MODULE_GROUPS } from '../quickBuilder/moduleGuides';
import { setupNeeds, nodeSummary, nodeIO } from '../quickBuilder/builderGuidance';
import { readTestSession, saveTestSession, testStorage, newTestRequestId } from '../quickBuilder/testSession';
import { useDraftActor } from '../quickBuilder/useDraftActor';
import { initializeToolArguments } from '../quickBuilder/toolArguments';
import { completionTestResult } from '../quickBuilder/completion';
import { clone, modules, initialGraph, makeNode, connect, removeNode, maskTrace, statusLabel, GRAPH_GEOMETRY, layoutGraph, edgeGeometry, graphBounds, canvasPoint, canvasDelta } from '../quickBuilder/graph';
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
const moduleQuery = ref(''), testNotice = ref(''), testedRevision = ref(null), testedGraph = ref(null), testedQuestion = ref('');
const trackedTest = ref(null), checkingTest = ref(false), recentTests = ref([]), waitingStopped = ref(false);
const unresolvedTest = computed(() => trackedTest.value && !['completed','failed','cancelled'].includes(trackedTest.value.serverStatus));
const runStatus = value => ({running:'執行中',completed:'已完成',failed:'執行失敗',cancelled:'已取消'}[value] || '結果待確認');
let testController, testEpoch = 0, trackEpoch = 0, trackTimer, trackController, trackSelectedVersion, trackAttempts = 0;
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
const metadataDetails = ref(null);
const selectedModelName = computed(() => catalog.value.models.find(m => String(m.id) === String(form.model_id))?.name || (form.model_id ? '已選模型' : '未選模型'));
const scopeName = computed(() => ({unit:'本單位',departments:'指定單位',public:'全院'}[form.scope] || '未選範圍'));
const configOpen = ref(true), canvasViewport = ref(null), zoom = ref(1), fitMode = ref(false);
const setupIssues = computed(() => setupNeeds(form));
const moduleMatches = m => { const g = moduleGuide(m.kind); return [g.title,g.description,g.input,g.output,g.example,g.technicalName].join(' ').toLocaleLowerCase().includes(moduleQuery.value.trim().toLocaleLowerCase()); };
const visibleModuleGroups = computed(() => MODULE_GROUPS.map(g => ({...g,modules:modules.filter(m => moduleGuide(m.kind).group === g.id && moduleMatches(m))})).filter(g => g.modules.length));
const visibleTools = computed(() => (catalog.value.mcp_tools || []).filter(t => `${t.name} ${t.description || ''}`.toLocaleLowerCase().includes(moduleQuery.value.trim().toLocaleLowerCase())));
const versions = computed(() => detail.value?.versions || []);
const version = computed(() => versions.value.find(v => String(v.id) === String(versionId.value)));
const editable = computed(() => !blocked.value && actorReady.value && (!detail.value || detail.value.can_edit === true && (!version.value || Number(version.value.revision) === Number(detail.value.draft_revision))));
const dirty = computed(() => JSON.stringify(form) !== baseline.value);
const selected = computed(() => form.graph.nodes.find(n => n.id === selectedId.value));
const sourceNode = computed(() => form.graph.nodes.find(n => n.id === sourceId.value));
const chosenTemplate = computed(() => templates.value.find(t => String(t.id) === String(templateId.value)));
const extractFields = computed(() => [...new Set(form.graph.nodes.filter(n => n.kind === 'extract').flatMap(n => n.config.fields || []))]);
const drawnEdges = computed(() => form.graph.edges.map(edge => ({edge, geometry: edgeGeometry(form.graph, edge)})));
const canvasBounds = computed(() => graphBounds(form.graph, {}, drawnEdges.value.map(item => item.geometry)));
const contentBounds = computed(() => graphBounds(form.graph, {includeOrigin: false}, drawnEdges.value.map(item => item.geometry)));
const canvasWidth = computed(() => Math.max(480, canvasBounds.value.width));
const canvasHeight = computed(() => Math.max(500, canvasBounds.value.height));
async function changeZoom(value) {
  const viewport = canvasViewport.value;
  const previous = zoom.value;
  const centre = viewport ? {x: (viewport.scrollLeft + viewport.clientWidth / 2) / previous, y: (viewport.scrollTop + viewport.clientHeight / 2) / previous} : null;
  zoom.value = Math.min(1.5, Math.max(0.4, Math.round(value * 100) / 100));
  fitMode.value = false;
  await nextTick();
  if (viewport && centre) {
    viewport.scrollLeft = Math.max(0, centre.x * zoom.value - viewport.clientWidth / 2);
    viewport.scrollTop = Math.max(0, centre.y * zoom.value - viewport.clientHeight / 2);
  }
}
async function fitCanvas() {
  fitMode.value = true;
  await nextTick();
  const viewport = canvasViewport.value;
  if (!viewport?.clientWidth || !viewport.clientHeight) return;
  const bounds = contentBounds.value;
  zoom.value = Math.min(1, Math.max(1, viewport.clientWidth - 48) / bounds.width, Math.max(1, viewport.clientHeight - 48) / bounds.height);
  await nextTick();
  viewport.scrollLeft = Math.max(0, (bounds.x - canvasBounds.value.x + bounds.width / 2) * zoom.value - viewport.clientWidth / 2);
  viewport.scrollTop = Math.max(0, (bounds.y - canvasBounds.value.y + bounds.height / 2) * zoom.value - viewport.clientHeight / 2);
}
async function resetCanvasView() {
  zoom.value = 1;
  fitMode.value = false;
  await nextTick();
  if (canvasViewport.value) {
    canvasViewport.value.scrollLeft = 0;
    canvasViewport.value.scrollTop = 0;
  }
}
function arrangeNodes() {
  if (!editable.value || busy.value) return;
  form.graph = layoutGraph(form.graph);
  notice.value = '已整理步驟位置；流程與設定不變。儲存新版本可保留這個排列。';
  fitCanvas();
}
let canvasObserver;
watch([canvasViewport, configOpen], async () => {
  await nextTick();
  canvasObserver?.disconnect();
  if (canvasViewport.value) canvasObserver?.observe(canvasViewport.value);
  if (fitMode.value) fitCanvas();
});
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
const contextSourceLabel = id => form.graph.nodes.find(n => n.id === id)?.label || '未標示的文件步驟';
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
  invalidateTest();
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
  testNotice.value = '';
  testedQuestion.value = '';
  resetCanvasView();
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
  await restoreTest();
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
  form.graph = layoutGraph(chosenTemplate.value.graph);
  form.name = chosenTemplate.value.name;
  form.description = chosenTemplate.value.description;
  selectedId.value = form.graph.nodes[0]?.id;
  testResult.value = null;
  testNotice.value = '';
  resetCanvasView();
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
  if (!form.name.trim() || !form.model_id) {
    error.value = '請先填寫助手名稱並選擇平台模型（CONFIGURATION_REQUIRED）。';
    await goToSetting({key:!form.name.trim() ? 'name' : 'model'});
    return;
  }
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
function selectNode(id, open = false) {
  selectedId.value = id;
  if (open) configOpen.value = true;
}
async function goToSetting(issue) {
  panel.value = 'canvas';
  if (issue.nodeId) selectNode(issue.nodeId, true);
  else if (metadataDetails.value) metadataDetails.value.open = true;
  await nextTick();
  const target = issue.nodeId ? document.getElementById('qb-step-settings') : document.querySelector(issue.key === 'model' ? '.qb-metadata select' : '.qb-metadata input');
  target?.scrollIntoView?.({block:'nearest'});
  (issue.nodeId ? target?.querySelector('input,select,textarea') : target)?.focus?.();
}
function editTestStep(id) {
  if (id) goToSetting({nodeId:id});
  else panel.value = 'canvas';
}
function stopTracking() {
  ++trackEpoch;
  clearTimeout(trackTimer);
  trackController?.abort();
  trackController = null;
  checkingTest.value = false;
}
function invalidateTest() {
  ++testEpoch;
  testController?.abort();
  testController = null;
  stopTracking();
  trackedTest.value = null;
  recentTests.value = [];
  testing.value = false;
  waitingStopped.value = false;
}
function trackingCurrent(pointer, epoch) {
  return epoch === trackEpoch && !identityChanged.value && actor.actorId.value === pointer.actor && detail.value?.id === pointer.assistant && String(versionId.value) === String(trackSelectedVersion);
}
function rememberTest(pointer) {
  return saveTestSession(testStorage(), pointer);
}
function scheduleTracking(pointer, epoch) {
  if (!trackingCurrent(pointer, epoch) || trackAttempts >= 60) return;
  clearTimeout(trackTimer);
  trackTimer = setTimeout(() => queryTracked(pointer, epoch), trackAttempts < 10 ? 1500 : 5000);
}
function acceptRun(raw, pointer, epoch, restored = false) {
  if (!trackingCurrent(pointer, epoch)) return;
  if (!Number.isSafeInteger(raw?.id) || raw.id <= 0 || pointer.run && raw.id !== pointer.run || raw.version_id !== undefined && raw.version_id !== pointer.version || pointer.request_id && (raw.version_id !== pointer.version || raw.request_id !== pointer.request_id)) throw new Error('TEST_RESULT_INVALID');
  const status = ['running','completed','failed','cancelled'].includes(raw.status) ? raw.status : 'unknown';
  const current = {...pointer,run:raw.id,state:status === 'running' ? 'waiting' : status === 'unknown' ? 'unknown' : 'finished'};
  trackedTest.value = {...current,serverStatus:status};
  const stored = rememberTest(current);
  if (status === 'running' || status === 'unknown') {
    testNotice.value = status === 'running' ? (waitingStopped.value ? '已停止等待回覆，伺服器仍在執行；這裡會繼續查詢同一次試跑。' : '已找到同一次試跑，伺服器仍在執行；尚未提供逐步進度。') : '這次試跑的伺服器狀態尚未確認（TEST_RESULT_UNKNOWN），查詢不會重新執行。';
    scheduleTracking(current, epoch);
    return;
  }
  stopTracking();
  const sourceVersion = versions.value.find(v => v.id === pointer.version);
  if (!sourceVersion) throw new Error('TEST_RESULT_INVALID');
  testResult.value = completionTestResult(raw, maskTrace(raw));
  testedGraph.value = clone(sourceVersion.graph);
  testedRevision.value = sourceVersion.revision;
  if (!testedQuestion.value) {
    const inputId = sourceVersion.graph.nodes.find(n => n.kind === 'input')?.id;
    const input = raw.steps?.find(step => step.node_id === inputId)?.input;
    testedQuestion.value = typeof input === 'string' ? maskTrace(input) : typeof input?.text === 'string' ? maskTrace(input.text) : '';
  }
  testing.value = false;
  busy.value = false;
  ++testEpoch;
  testController?.abort();
  testController = null;
  testNotice.value = restored ? `已重新取得第 ${sourceVersion.revision} 版的同一次試跑結果；這不是新的執行。` : '';
  if (!stored) testNotice.value += ' 瀏覽器未保留執行指標（TEST_HISTORY_UNAVAILABLE）；重載後可從「查詢我的試跑」找回。';
}
async function queryTracked(pointer = trackedTest.value, epoch = trackEpoch) {
  if (!pointer || !trackingCurrent(pointer, epoch) || checkingTest.value) return;
  checkingTest.value = true;
  trackAttempts++;
  const controller = new AbortController();
  trackController = controller;
  try {
    let current = pointer;
    if (!current.run) {
      const rows = (await api.getTestRuns(pointer.assistant, {version_id:pointer.version,...(pointer.request_id ? {request_id:pointer.request_id} : {})}, {...actor.mutationOptions(),signal:controller.signal})).data.runs;
      if (!trackingCurrent(pointer, epoch)) return;
      const row = pointer.request_id ? rows.find(run => run.request_id === pointer.request_id && run.version_id === pointer.version) : null;
      if (!row) {
        trackedTest.value = {...pointer,state:'unknown',serverStatus:'unknown'};
        rememberTest(trackedTest.value);
        testNotice.value = '尚未找到這次試跑紀錄（TEST_RESULT_UNKNOWN）。請求可能仍在處理或連線中斷；查詢不會重新執行。';
        scheduleTracking(pointer, epoch);
        return;
      }
      current = {...pointer,run:row.id};
    }
    const raw = (await api.getRun(current.run, {...actor.mutationOptions(),signal:controller.signal})).data;
    if (!trackingCurrent(pointer, epoch)) return;
    acceptRun(raw, current, epoch, !testing.value);
  } catch (e) {
    if (!trackingCurrent(pointer, epoch)) return;
    if (errorType(e) === 'IDENTITY_CHANGED') { actor.lockIdentity(); return; }
    trackedTest.value = {...pointer,state:'unknown',serverStatus:'unknown'};
    rememberTest(trackedTest.value);
    if ([401,403,404].includes(e?.response?.status)) {
      testResult.value = null;
      testNotice.value = '無法讀取這次試跑結果（TEST_RESULT_UNAVAILABLE）。可能已無權限或資料已不可用；請確認登入身分與助手版本。';
    } else {
      testNotice.value = '暫時無法確認試跑狀態（TEST_RESULT_UNKNOWN）。伺服器可能仍在執行，可繼續查詢同一次試跑。';
      scheduleTracking(pointer, epoch);
    }
  } finally {
    if (epoch === trackEpoch) checkingTest.value = false;
    if (trackController === controller) trackController = null;
  }
}
function trackTest(pointer) {
  stopTracking();
  trackSelectedVersion = versionId.value;
  trackAttempts = 0;
  trackedTest.value = {...pointer,serverStatus:'unknown'};
  return trackEpoch;
}
async function restoreTest() {
  const current = readTestSession(testStorage(), actor.actorId.value, detail.value?.id);
  if (!current || identityChanged.value) return;
  if (!current.run && !current.request_id) {
    testNotice.value = '上次試跑未保留可查詢的識別碼（TEST_RESULT_UNKNOWN）。可用「查詢我的試跑」查看本人有權限的紀錄。';
    return;
  }
  if (!versions.value.some(v => v.id === current.version)) return;
  const epoch = trackTest(current);
  await queryTracked(current, epoch);
}
async function findMyTests() {
  if (checkingTest.value || !detail.value || !version.value || identityChanged.value) return;
  const context = {actor:actor.actorId.value,assistant:detail.value.id,version:version.value.id};
  const epoch = trackEpoch;
  checkingTest.value = true;
  try {
    const rows = (await api.getTestRuns(context.assistant, {version_id:context.version}, actor.mutationOptions())).data.runs;
    if (epoch !== trackEpoch || identityChanged.value || context.actor !== actor.actorId.value || context.assistant !== detail.value?.id || context.version !== version.value?.id) return;
    recentTests.value = rows.filter(run => run.version_id === context.version);
    testNotice.value = rows.length ? '請選擇要查看的本人試跑紀錄；查詢不會重新執行。' : '目前沒有可查詢的本人試跑紀錄。尚未找到不代表先前請求一定未執行。';
  } catch (e) {
    if (epoch === trackEpoch && errorType(e) === 'IDENTITY_CHANGED') { actor.lockIdentity(); return; }
    if (epoch === trackEpoch && !identityChanged.value) testNotice.value = '無法讀取本人試跑紀錄（TEST_RESULT_UNAVAILABLE）。請確認登入身分與助手版本。';
  } finally { if (epoch === trackEpoch) checkingTest.value = false; }
}
async function selectTestRun(row) {
  if (testing.value || identityChanged.value || row.version_id !== version.value?.id) return;
  testResult.value = null;
  testedQuestion.value = '';
  const pointer = {actor:actor.actorId.value,assistant:detail.value.id,version:row.version_id,run:row.id,state:row.status === 'running' ? 'waiting' : 'finished',...(row.request_id ? {request_id:row.request_id} : {})};
  const epoch = trackTest(pointer);
  rememberTest(pointer);
  await queryTracked(pointer, epoch);
}
function stopTest() {
  if (!testing.value) return;
  ++testEpoch;
  testController?.abort();
  testController = null;
  testing.value = false;
  busy.value = false;
  waitingStopped.value = true;
  testNotice.value = '已停止等待回覆，沒有取消伺服器執行。會用同一個識別碼查詢結果；重新整理後也可找回。';
  queryTracked();
}
function leaveUnknownTest() {
  if (testing.value || !unresolvedTest.value) return;
  if (!window.confirm('先前試跑可能仍在執行。建立另一個測試可能產生額外執行；確定不再追蹤目前紀錄？')) return;
  stopTracking();
  trackedTest.value = null;
  testNotice.value = '已結束目前的查詢；先前試跑的伺服器狀態仍未確認。';
}
async function test() {
  if (blocked.value || unresolvedTest.value || dirty.value || !version.value || !message.value.trim()) return;
  let requestId;
  try { requestId = newTestRequestId(); } catch { testNotice.value = '瀏覽器無法建立試跑識別碼，請重新開啟頁面（TEST_REQUEST_ID_UNAVAILABLE）。'; return; }
  busy.value = true;
  testing.value = true;
  waitingStopped.value = false;
  error.value = '';
  testNotice.value = '';
  testResult.value = null;
  const token = ++testEpoch;
  const pointer = {actor:actor.actorId.value,assistant:detail.value.id,version:version.value.id,state:'waiting',request_id:requestId};
  testedGraph.value = clone(version.value.graph);
  testedRevision.value = version.value.revision;
  testedQuestion.value = maskTrace(message.value);
  rememberTest(pointer);
  const epoch = trackTest(pointer);
  const controller = new AbortController();
  testController = controller;
  scheduleTracking(pointer, epoch);
  try {
    const raw = (await api.assistantAction(pointer.assistant, 'test', {
      version_id: pointer.version,
      message: message.value,
      request_id: pointer.request_id
    }, {...actor.mutationOptions(), signal:controller.signal})).data;
    if (token !== testEpoch || !trackingCurrent(pointer, epoch)) return;
    acceptRun(raw, pointer, epoch);
  } catch (e) {
    if (token !== testEpoch || !trackingCurrent(pointer, epoch)) return;
    trackedTest.value = {...pointer,state:'unknown',serverStatus:'unknown'};
    rememberTest(trackedTest.value);
    testNotice.value = '尚未取得可確認的試跑結果（TEST_RESULT_UNKNOWN）。會查詢同一次試跑，連線中斷不代表執行已取消。';
    fail(e);
    scheduleTracking(pointer, epoch);
  } finally {
    if (testController === controller) testController = null;
    if (token === testEpoch) {
      busy.value = false;
      testing.value = false;
    }
  }
}
watch(identityChanged, changed => {
  if (!changed) return;
  invalidateTest();
  busy.value = false;
  testResult.value = null;
  testNotice.value = '';
  testedQuestion.value = '';
});

function addNode(kind, position, toolId) {
  if (!editable.value || busy.value) return;
  fitMode.value = false;
  const nodes = form.graph.nodes;
  const available = position || {
    x: nodes.length ? Math.min(...nodes.map(node => node.position.x)) : GRAPH_GEOMETRY.padding,
    y: nodes.length ? Math.max(...nodes.map(node => node.position.y + GRAPH_GEOMETRY.nodeHeight)) + GRAPH_GEOMETRY.rankGap : GRAPH_GEOMETRY.padding
  };
  const n = makeNode(kind, nodes.length, available);
  if (toolId) n.config.tool_id = toolId;
  form.graph.nodes.push(n);
  selectNode(n.id, true);
  if (!position) nextTick(() => {
    const viewport = canvasViewport.value;
    if (!viewport?.clientWidth || !viewport.clientHeight) return;
    viewport.scrollLeft = Math.max(0, (n.position.x - canvasBounds.value.x) * zoom.value - 24);
    viewport.scrollTop = Math.max(0, (n.position.y - canvasBounds.value.y) * zoom.value - 24);
  });
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
    notice.value = '已連接步驟，儲存新版本後即可試跑。';
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
    const point = canvasPoint(event, {left: rect.left + event.currentTarget.clientLeft, top: rect.top + event.currentTarget.clientTop, scrollLeft: event.currentTarget.scrollLeft, scrollTop: event.currentTarget.scrollTop, zoom: zoom.value});
    addNode(data.kind, {
      x: Math.max(0, point.x + canvasBounds.value.x - GRAPH_GEOMETRY.nodeWidth / 2),
      y: Math.max(0, point.y + canvasBounds.value.y - 20)
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
    top: node.position.y,
    zoom: zoom.value
  };
  e.currentTarget.setPointerCapture?.(e.pointerId);
}
function moveNode(e, node) {
  if (moving?.id !== node.id) return;
  const delta = canvasDelta({x: e.clientX - moving.x, y: e.clientY - moving.y}, moving.zoom);
  if (delta.x || delta.y) fitMode.value = false;
  node.position.x = Math.max(0, moving.left + delta.x);
  node.position.y = Math.max(0, moving.top + delta.y);
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
  fitMode.value = false;
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
function beforeUnload(e) {
  if (dirty.value) {
    e.preventDefault();
    e.returnValue = '';
  }
}
baseline.value = JSON.stringify(form);
onMounted(() => {
  load();
  if (typeof ResizeObserver !== 'undefined') {
    canvasObserver = new ResizeObserver(() => { if (fitMode.value) fitCanvas(); });
    if (canvasViewport.value) canvasObserver.observe(canvasViewport.value);
  }
  window.addEventListener('beforeunload', beforeUnload);
});
onBeforeUnmount(() => { invalidateTest(); window.removeEventListener('beforeunload', beforeUnload); canvasObserver?.disconnect(); });
onBeforeRouteLeave(() => discard());
</script>
<style src="../quickBuilder/builder.css"></style>
