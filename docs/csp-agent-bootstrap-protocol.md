# CSP Agent Bootstrap Protocol

> **Status**: protocol spec + Phase 0.5 implementation plan
> **Date**: 2026-05-02
> **Purpose**: 定義跨語言、跨實作的 agent 入會協定，並規畫 CSP dev 頁面強化以支援自寫 agent / legacy migration。
> **Audience**: 任何要寫 / fork agent 接 CSP 平台的 dev — Python / Node / Go / 其他。

---

## Part 1 — Protocol Spec（wire-level contract）

這部分**凍結**，AgenticRAG 跟 anila-core 兩邊 CLI 改動都要遵守。

### 角色與術語

| 名詞 | 定義 |
|---|---|
| **bsk-** token | bootstrap key。一次性、短效（預設 15min，最長 1h）。Admin 在 CSP UI 發給特定 agent_id。 |
| **csk-** token | service key。長效、agent 用來呼 CSP API 的憑證。bsk- 換來。 |
| **agent_id** | CSP 內 agent 紀錄的數字 PK，admin 發 bsk- 時連同 endpoint_url 一起 register。 |
| **endpoint_url** | agent 自身對外 URL。bsk- 換 csk- 時必須帶相同值（防 token 在錯誤 agent 上被 replay）。 |
| **state file** | agent 本機儲存 csk- 的位置。建議 `/var/lib/anila-agent/service_token.json` mode 0600。 |

### Endpoint：bsk- → csk- exchange

```
POST {csp_url}/api/agents/{agent_id}/bootstrap

Headers:
  Content-Type: application/json

Body:
  {
    "bootstrap_token": "bsk-XXXXXXXX",
    "endpoint_url":    "https://my-agent.example.com",
    "label":           "pod-1"   // optional, ≤100 chars
  }

Response 200 OK:
  {
    "service_token":  "csk-YYYYYYYYYY",
    "credential_id":  42,
    "issued_at":      "2026-05-02T08:42:31Z",
    "label":          "pod-1"
  }

Errors:
  401 Unauthorized        — bsk- invalid / wrong sha256 hash
  401 Unauthorized        — endpoint_url mismatch（防 replay）
  410 Gone                — bsk- already consumed
  410 Gone                — bsk- expired
  404 Not Found           — agent_id 不存在
  422 Unprocessable Entity — body schema invalid
```

**單次性**：同一個 bsk- 換成功一次後即廢。重發要走 admin UI 重 issue。

**State file 格式**（建議，非強制；只要 RotatingServiceTokenMiddleware 看得懂即可）：

```json
{
  "token": "csk-...",
  "previous_token": null,
  "previous_expires_at": null,
  "agent_id": 42,
  "endpoint_url": "https://my-agent.example.com",
  "csp_url": "https://csp.internal",
  "label": "pod-1",
  "credential_id": 42,
  "issued_at": "2026-05-02T08:42:31Z",
  "schema_version": 1
}
```

### Endpoint：csk- 使用方式

每個對 CSP 的呼叫帶 header：

```
X-CSP-Service-Token: csk-YYYYYYYYYY
```

### Endpoint：csk- rotation

當 admin 在 CSP UI 按「rotate」：
- CSP issue 新 csk-、舊的進入 `previous_token` grace（預設 24h）
- agent 端 `RotatingServiceTokenMiddleware` 會自動偵測 state file 變更 + reload
- 兩個 token 都 valid 直到 `previous_expires_at`

**自寫 agent 沒 RotatingServiceTokenMiddleware 怎麼辦**：admin 手動觸發 + agent 重啟 + 重新跑一次 bootstrap 流程；或實作自己的 file watcher。

### Endpoint：static credential（非 bootstrap path）

某些 agent 跑不起 bootstrap CLI（純 bash / 預先封包好的第三方），可以走：

```
POST {csp_url}/api/agents/{agent_id}/credentials/issue-static
  (admin auth required, no bsk- needed)

Response 200:
  {
    "service_token": "csk-...",
    "credential_id": 43,
    "issued_at": "...",
    "label": "..."
  }
```

⚠️ **沒有 endpoint_url verification**，所以 csk- 一旦洩漏可被任何 holder 用。**Admin 必須週期性手動 rotate**。優先走 bsk- 流程，static 是 fallback。

---

## Part 2 — Code Snippets（實作參考）

### curl（最小可行）

```bash
#!/usr/bin/env bash
set -euo pipefail

CSP_URL="${CSP_URL:?must be set}"
AGENT_ID="${ANILA_AGENT_ID:?must be set}"
BSK="${BOOTSTRAP_TOKEN:?must be set}"
ENDPOINT="${AGENT_ENDPOINT_URL:?must be set}"

response=$(curl -sS -X POST \
  "${CSP_URL%/}/api/agents/${AGENT_ID}/bootstrap" \
  -H "Content-Type: application/json" \
  -d "{\"bootstrap_token\":\"${BSK}\",\"endpoint_url\":\"${ENDPOINT}\"}")

echo "$response" | jq -r '.service_token' > /var/lib/anila-agent/service_token.txt
chmod 0600 /var/lib/anila-agent/service_token.txt
```

### Python（推薦給自寫 agent）

```python
import httpx
import json
import os
import stat
from pathlib import Path

def bootstrap(csp_url: str, agent_id: int, bsk: str, endpoint_url: str,
              state_dir: str = "/var/lib/anila-agent") -> str:
    resp = httpx.post(
        f"{csp_url.rstrip('/')}/api/agents/{agent_id}/bootstrap",
        json={"bootstrap_token": bsk, "endpoint_url": endpoint_url},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    
    state_path = Path(state_dir) / "service_token.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "token": data["service_token"],
        "agent_id": agent_id,
        "endpoint_url": endpoint_url,
        "csp_url": csp_url,
        "credential_id": data["credential_id"],
        "issued_at": data["issued_at"],
        "schema_version": 1,
    }, indent=2))
    os.chmod(state_path, stat.S_IRUSR | stat.S_IWUSR)
    return data["service_token"]

# Usage in startup
csk = bootstrap(
    csp_url=os.environ["CSP_URL"],
    agent_id=int(os.environ["ANILA_AGENT_ID"]),
    bsk=os.environ["BOOTSTRAP_TOKEN"],
    endpoint_url=os.environ["AGENT_ENDPOINT_URL"],
)
# 後續呼 CSP 都帶 X-CSP-Service-Token: csk
```

### Node.js（fork 給寫 TS agent 的 dev）

```typescript
import { writeFileSync, chmodSync, mkdirSync } from 'fs';
import { dirname, join } from 'path';

interface BootstrapResponse {
  service_token: string;
  credential_id: number;
  issued_at: string;
  label: string | null;
}

async function bootstrap(opts: {
  cspUrl: string;
  agentId: number;
  bsk: string;
  endpointUrl: string;
  stateDir?: string;
}): Promise<string> {
  const { cspUrl, agentId, bsk, endpointUrl, stateDir = '/var/lib/anila-agent' } = opts;
  
  const resp = await fetch(
    `${cspUrl.replace(/\/$/, '')}/api/agents/${agentId}/bootstrap`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bootstrap_token: bsk, endpoint_url: endpointUrl }),
    }
  );
  
  if (!resp.ok) {
    throw new Error(`bootstrap failed: ${resp.status} ${await resp.text()}`);
  }
  
  const data: BootstrapResponse = await resp.json();
  const statePath = join(stateDir, 'service_token.json');
  mkdirSync(dirname(statePath), { recursive: true });
  writeFileSync(statePath, JSON.stringify({
    token: data.service_token,
    agent_id: agentId,
    endpoint_url: endpointUrl,
    csp_url: cspUrl,
    credential_id: data.credential_id,
    issued_at: data.issued_at,
    schema_version: 1,
  }, null, 2));
  chmodSync(statePath, 0o600);
  return data.service_token;
}
```

### Go

```go
package bootstrap

import (
    "bytes"
    "encoding/json"
    "fmt"
    "net/http"
    "os"
    "path/filepath"
    "time"
)

type BootstrapResponse struct {
    ServiceToken string    `json:"service_token"`
    CredentialID int       `json:"credential_id"`
    IssuedAt     time.Time `json:"issued_at"`
    Label        *string   `json:"label"`
}

func Bootstrap(cspURL string, agentID int, bsk, endpointURL, stateDir string) (string, error) {
    body, _ := json.Marshal(map[string]string{
        "bootstrap_token": bsk,
        "endpoint_url":    endpointURL,
    })
    
    url := fmt.Sprintf("%s/api/agents/%d/bootstrap", cspURL, agentID)
    resp, err := http.Post(url, "application/json", bytes.NewBuffer(body))
    if err != nil {
        return "", fmt.Errorf("post bootstrap: %w", err)
    }
    defer resp.Body.Close()
    
    if resp.StatusCode != http.StatusOK {
        return "", fmt.Errorf("bootstrap returned %d", resp.StatusCode)
    }
    
    var data BootstrapResponse
    if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
        return "", fmt.Errorf("decode response: %w", err)
    }
    
    statePath := filepath.Join(stateDir, "service_token.json")
    if err := os.MkdirAll(stateDir, 0700); err != nil {
        return "", err
    }
    statePayload, _ := json.MarshalIndent(map[string]any{
        "token":          data.ServiceToken,
        "agent_id":       agentID,
        "endpoint_url":   endpointURL,
        "csp_url":        cspURL,
        "credential_id":  data.CredentialID,
        "issued_at":      data.IssuedAt,
        "schema_version": 1,
    }, "", "  ")
    if err := os.WriteFile(statePath, statePayload, 0600); err != nil {
        return "", err
    }
    return data.ServiceToken, nil
}
```

---

## Part 3 — Phase 0.5 實作工作項目

### 3.1 文件落地

| 檔 | 內容 | LOC |
|---|---|---|
| `docs/csp-agent-bootstrap-protocol.md` | 本檔（凍結 protocol + snippets）| ✅ 已寫 |
| `myCSPPlatform/docs/agent-onboarding.md`（新）| 整理三條入會路徑：(a) AgenticRAG fork (b) 自寫 agent + bsk- (c) 第三方 + static credential | ~150 |
| `AgenticRAG/docs/CSP_INTEGRATION.md`（更新）| 改寫 fork-template 視角，刪除 anila-core 提及，引用本 protocol 文件 | ~80 |

### 3.2 CSP DeveloperAgentsView 強化

`myCSPPlatform/frontend/src/views/DeveloperAgentsView.vue` 的 `secret-banner`（line 301-323）目前只顯示 bsk- + 一行使用提示。要擴充：

```
[secret-banner]
  bootstrap token (bsk-)        copy now — will not be shown again
  ┌─ csk-XXXXXXXXXXXX [copy] [hide] ─┐
  │ expires 2026-05-02T... — agent must call POST /api/agents/42/bootstrap
  │   with this token + endpoint_url=https://...
  ├──────────────────────────────────┤
  │ [▸ how to use]                   │  ← 新增 collapsible
  └──────────────────────────────────┘

[展開後]
  Tabs: [AgenticRAG fork] [Python] [Node] [Go] [curl] [docs]
  
  ┌── AgenticRAG fork ──────────────────────────┐
  │  echo "BOOTSTRAP_TOKEN=bsk-..." >> .env     │
  │  echo "ANILA_AGENT_ID=42"      >> .env      │
  │  echo "AGENT_ENDPOINT_URL=..." >> .env      │
  │  docker compose up                          │
  │  → bootstrap CLI runs automatically         │
  └─────────────────────────────────────────────┘

  ┌── Python ────────────────────────────────────┐
  │  [pre-filled snippet with this agent's id   │
  │   and endpoint_url already substituted]     │
  │  [copy button]                              │
  └─────────────────────────────────────────────┘

  [docs] tab: link to /docs/csp-agent-bootstrap-protocol.md
```

**新組件**：

| 檔 | 用途 | LOC |
|---|---|---|
| `frontend/src/components/agents/BootstrapHowToTabs.vue` | tabs + snippets 展示 | ~180 |
| `frontend/src/lib/bootstrapSnippets.js` | snippet templates 函式（接收 agent_id / endpoint_url 回填）| ~120 |
| 修 `DeveloperAgentsView.vue` 的 secret-banner | embed 新組件 | ~30 |

**Total UI LOC**: ~330。

**Snippet 函式範例**：

```js
// bootstrapSnippets.js
export function pythonSnippet({ cspUrl, agentId, endpointUrl, bsk }) {
  return `import httpx, json, os
from pathlib import Path
resp = httpx.post(
  "${cspUrl}/api/agents/${agentId}/bootstrap",
  json={"bootstrap_token": "${bsk}", "endpoint_url": "${endpointUrl}"},
  timeout=30.0,
)
resp.raise_for_status()
csk = resp.json()["service_token"]
Path("/var/lib/anila-agent/service_token.json").write_text(...)
`;
}

// 同樣有 nodeSnippet / goSnippet / curlSnippet / agenticRagSnippet
```

### 3.3 Static credential UI 路徑

`DeveloperAgentsView.vue` line 330 已經有 `[issue static (csk-)]` 按鈕（Phase F）。要補充：

- 點按鈕後彈 modal：「為何選 static？」說明（給沒讀文件的 admin 看）
- 顯示後也要有 same `[▸ how to use]` 但 tabs 不同：
  - Tabs: [Bash] [docker env] [config file]
  - 沒有 bsk- 換手步驟，純 「export X-CSP-Service-Token=csk-... 然後送 request」

**LOC**: ~80（modal + 簡化版 BootstrapHowToTabs）。

### 3.4 Legacy CSP_SERVICE_TOKEN migration UX

當前狀態（`auth_service.py:168`）：
- 共用 `CSP_SERVICE_TOKEN` env var **仍然接受**（Tier 3 fallback）
- 每次 hit 寫 `service_token_legacy_env_used` audit log
- TODO: cutover step 5 移除（runbook 在 `docs/runbooks/service-token-cutover.md`）

**建議的 UI banner**（DashboardView 或 Admin overview）：

```
[⚠ Legacy CSP_SERVICE_TOKEN still active]
  N audit events in last 24h — caller unattributed.
  
  Per-agent migration progress:  [████░░░░] 5/8 agents migrated
  
  [→ See unmigrated agents]         [→ Cutover runbook]
```

**Per-agent action**: agent detail 頁加按鈕「migrate to per-agent token」：
- 等同呼 `issue-bootstrap` 或 `issue-static`，再給 admin 截圖 / instructions「請在 agent 端 update env」
- 完成後 audit log 從該 agent 不再出現 `service_token_legacy_env_used`

**LOC**: ~150（dashboard banner + agent migration helper modal）。

### 3.5 Backend：audit log 查詢支援

要讓 dashboard banner 跑得起來，audit log 要能 group by `metadata.agent_id` 或 caller IP 算出「哪些 agent 還在用 legacy」。

如果現在 audit log 沒這資料，Phase 0.5 加 backfill：每次 `service_token_legacy_env_used` 寫 caller IP + User-Agent 進 metadata（已經有就跳過）。

**LOC**: ~40（最壞情況）。

## 工作量 / 排期

| 項目 | LOC | 天 |
|---|---|---|
| 3.1 文件 | ~230 | 0.5 |
| 3.2 BootstrapHowToTabs + snippets | ~330 | 1.5 |
| 3.3 Static credential UX | ~80 | 0.5 |
| 3.4 Legacy migration banner / per-agent button | ~150 | 1 |
| 3.5 Audit log metadata（如需）| ~40 | 0.5 |
| Buffer / E2E | — | 0.5 |
| **總** | **~830** | **~4.5 天** |

**前置依賴**：Phase 0 完成（AgenticRAG decoupled）。理由：snippet 裡的 AgenticRAG fork tab 要寫 `python -m agentic_rag.cli bootstrap`，那條 CLI 路徑得先存在。

## 驗收條件

- [ ] `docs/csp-agent-bootstrap-protocol.md` 凍結，被 AgenticRAG / anila-core 兩邊 README 引用
- [ ] DeveloperAgentsView 發 bsk- 後，admin 看得到一個展開「how to use」面板，含 5 個 tabs（含 docs link）
- [ ] 每個 snippet 已預填當前 agent 的 agent_id / endpoint_url，可一鍵 copy
- [ ] static credential 按鈕也有對應 how-to
- [ ] Dashboard 顯示 legacy CSP_SERVICE_TOKEN 仍在用的 banner（如果有）
- [ ] 隨機抽一個寫 Go agent 的 dev，能照本文件 + UI snippet 在 30 分鐘內接通

---

**Last updated**: 2026-05-02 · **Estimate**: ~4.5 days · **Total LOC**: ~830 · **Blocked by**: Phase 0
