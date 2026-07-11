# Gate 0 Closeout Evidence Runbook

本 runbook 只負責把 Gate 0 的部署驗收變成可重跑、可雜湊、可簽核的證據。
安全修補已進入 `prod-intranet-card` 不等於 Gate 0 已關閉；下列必要證據任一缺少，
狀態仍是 **Open / No-Go**。

實體卡的完整 OS／瀏覽器／讀卡機／卡型矩陣與 DR restore drill 屬 Gate 6，
不阻擋 Gate 1 開工；但部署中 TLS 私鑰是否仍等於 Git 歷史外洩金鑰、以及換發
前後 fingerprint，屬現行 Critical incident，必須先結案。

## 安全邊界

- 實際 evidence 一律寫到 repo 外的 `$ANILA_STATE_DIR/evidence/gate0-closeout/`。
- 產物只保存 allow-listed posture、mount／UID／ACL、HTTP 結果、certificate 與
  SPKI fingerprint；不保存 `.env` 展開內容、Bearer token 或 private-key bytes。
- evidence directory 與 JSON 預設分別為 mode `0700`／`0600`；`manifest.json`
  會保存每個 artifact 的 SHA-256。
- `--token-file` 必須是短效、由實際卡片 session 取得的 admin／owner JWT，
  mode `0600`，放在 repo 外；完成 Trace 回讀後立即刪除。
- 不得把正式 evidence bundle commit 到 PUBLIC repo。PR 只提交本 harness、
  runbook，以及經 Security Lead 核准的 redacted 結論／manifest hash。

## 0. 正式主機準備

```bash
cd /path/to/ANILA
set -a; source .env; set +a
export EVIDENCE_DIR="$ANILA_STATE_DIR/evidence/gate0-closeout/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$EVIDENCE_DIR"
chmod 700 "$EVIDENCE_DIR"
```

所有命令都從同一 commit 與同一 `EVIDENCE_DIR` 執行。若部署 image 不是由該
commit 建立／匯出的 image，證據無效。

## 1. TLS 歷史金鑰比對與輪換（Critical，先做）

先保存輪換前狀態；`before` 允許結果顯示「仍命中歷史 key」，以便留存 incident
證據，且不會因命中而阻止後續輪換：

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" tls --label before
```

`tls-before.json` 會把部署中 `server.key` 的 SPKI SHA-256 與 audit 內兩個
critical Git blob 的 SPKI SHA-256 比對。它不會輸出任何 private-key bytes。

若私鑰已在 collector 導入前輪換，禁止拿新 key 冒充 before。改由既有 audit
列出的 Git objects 重建只含 fingerprint 的歷史 cert/key pairs：

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" tls-history-before
```

若無法證明歷史 key 已停用，依 `docs/runbooks/rotate-tls-cert.md` 安裝新的正式
CA-signed pair；`reissue-tls-cert.sh` 只是在院內 CA 尚未就緒時的 on-prem
fallback。完成後 recreate／restart nginx，確認正式 endpoint 正常，再收 after：

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" tls --label after

python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" tls-compare \
  --before "$EVIDENCE_DIR/tls-before.json" \
  --after "$EVIDENCE_DIR/tls-after.json"
```

`tls-rotation-comparison.json` 必須同時證明：key SPKI 改變、certificate fingerprint
改變、新 pair 相符、after key 不命中任何歷史 key。由 Security Lead 記錄核准人、
執行時間、manifest hash，才可解除 incident 的 deployment No-Go。

## 2. Fresh-host air-gap 啟動

這一步只能在沒有既有 `anila-platform` containers／volumes 的乾淨內網主機執行。
它會先依 `platform-image-inventory.tsv` 確認所有 default image 已 preload；缺 image
時立即失敗，絕不 pull 或 build。確認主機確實可被本次驗收建立 stack 後執行：

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" fresh-host --confirm-empty-host
```

實際啟動命令固定為：

```text
docker compose up -d --no-build --pull never
```

必要產物為 `fresh-host-preflight.json` 與 `fresh-host-startup.json`。在既有主機上
清 volume 冒充 fresh host 不可接受；正式資料 volume 也不得為了驗收而刪除。

## 3. 正式 profile、mount、ACL、non-root runtime

stack ready 後執行：

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" profile
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" runtime
```

`formal-profile.json` 必須證明 codeserver 不在 default stack、n8n／GitLab 保留、
三個工具 link 指向獨立 HTTPS origin、GitLab SSH 不綁 wildcard、Memory/Public Share
關閉、Trace endpoint 存在、沒有 repo-root RW mount。

`runtime-mount-acl.json` 必須證明 CSP UID 非 0、log 與 ingestion mount 可寫、
secrets mount 寫入會失敗、TLS／secrets mount 唯讀、key directory/file ACL 已記錄、
沒有 privileged container 或 repo-root RW mount。

## 4. n8n／GitLab 獨立 origin 與 ingress

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" verify
```

fresh-host stack 全數 healthy 後，先執行一次可重複的 runtime posture 收斂，再做純回讀驗證：

```bash
bash infra/deployment/scripts/deploy-prod.sh postconfigure
```

接著由 closeout 工具執行正式 `deploy-prod.sh verify` 並保存輸出。必須覆蓋：
若 closeout host 使用尚未匯入系統 trust store 的內部 CA，可把該 CA bundle 的
repo 外絕對路徑設為 `ANILA_VERIFY_CA_FILE`；verify 會明確傳給所有 HTTPS curl。


- 主平台 `/n8n*`、`/gitlab*`、`/codeserver*` 全為 404。
- n8n／GitLab 由自己的 FQDN root origin 到達，且 native owner／signup posture 成立。
- n8n webhook／form／MCP／OAuth machine ingress 全為 404。
- n8n／GitLab／code host 在 CSP-bearing `:4443` 被 nginx 444 拒絕。
- GitLab schema/background migration 完成，image pin 與核准版本一致。

## 5. Full Trace 實送與 CSP 回讀

由正式卡片登入取得短效 admin／owner JWT，只把 token 寫入 repo 外的 mode 0600
暫存檔。harness 會在正式 Router container 內使用實際 `TraceExporter` 與 Router
service credential 送 span，再用該使用者 JWT 從 CSP control plane 回讀並比對
一次性 marker：

```bash
python3 infra/deployment/scripts/gate0-closeout-evidence.py \
  --evidence-dir "$EVIDENCE_DIR" trace \
  --base-url https://anila.ai.ncsist.org.tw \
  --token-file /run/user/$UID/anila-gate0.jwt \
  --ca-file /path/to/approved-intranet-ca.pem
rm -f /run/user/$UID/anila-gate0.jwt
```

不得用 DB 查詢代替 `GET /api/traces/{trace_id}`，也不得只引用 unit test。
`full-trace-roundtrip.json` 的 `router_exporter_stats.sent` 必須為 1，且
`marker_verified` 必須為 true。

## 6. 關閉與簽核

必需 artifacts：

- `tls-before.json`、`tls-after.json`、`tls-rotation-comparison.json`
- `fresh-host-preflight.json`、`fresh-host-startup.json`
- `formal-profile.json`、`runtime-mount-acl.json`
- `formal-deploy-verify.json`
- `full-trace-roundtrip.json`
- `manifest.json`

全部 `passed=true` 且 `manifest.json.git_worktree_clean=true` 後，由 Security
Lead、維運 owner 與 system owner 保存完整 bundle、核對 manifest hash 並簽核。
Gate 0 才可標示 Closed；Gate 1 第一支分支再從包含
closeout 修補的最新 `prod-intranet-card` 建立。
