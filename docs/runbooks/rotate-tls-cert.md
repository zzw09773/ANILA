# Rotate TLS Certificate / Purge Leaked Private Key

> 適用情境：Sprint 5 X 安全審查發現 `infra/nginx/certs/server.key`
> 自 commit `c043a23` / `2e96978` 起被 git 追蹤。Sprint 6 X / A3 提供徹底
> 移除歷史 + 重簽憑證的步驟。**此為破壞性操作**：執行 `git filter-repo`
> 會 rewrite all branches，所有 clone 必須重 clone。

---

## 步驟 1：通知所有 contributor 暫停推送

在執行歷史改寫前，請於團隊 channel 發出停推通知（建議 30 分鐘以上的窗口）：

```
[Sprint 6 X] TLS 私鑰歷史改寫
- 預定時間：YYYY-MM-DD HH:MM ~ HH:30
- 影響範圍：ingestion-design 及所有 active branch
- 行動：執行期間請勿 push；完成後請執行 `git fetch && git reset --hard
  origin/<branch>`，或乾脆 fresh clone（推薦）
```

## 步驟 2：備份 repo（一定要！）

```bash
cd $HOME
cp -a ANILA ANILA.backup-$(date +%Y%m%d-%H%M)
```

## 步驟 3：用 git filter-repo 移除歷史中的私鑰

```bash
# 安裝 git-filter-repo 若尚未安裝
pip install --user git-filter-repo

cd $HOME/ANILA
git filter-repo --invert-paths \
  --path infra/nginx/certs/server.key \
  --path infra/nginx/certs/server.key.bak \
  --force
```

執行後 `.git` 已重新打包。現行 Compose 不再從 working tree 讀 private key；
正式憑證只放 repo 外 `ANILA_TLS_CERTS_DIR`。

## 步驟 4：force-push 到 remote

```bash
# 列舉所有 branch 並全部 force-push（包含 origin/main）
git push --force --all
git push --force --tags
```

## 步驟 5：所有 contributor 重 clone

舊 clone 上的歷史仍含舊 commit hash，無法直接 fast-forward。最安全做法：

```bash
# 在每個 contributor 的工作站
cd $HOME
mv ANILA ANILA.old
git clone <repo-url> ANILA
cp ANILA.old/.env ANILA/.env  # 把本機 secrets 搬過來
# 不要把舊 TLS key 搬回 repo；新 key 會產在 repo 外 state 目錄
```

若 contributor 拒絕 fresh clone（有 in-flight branch），可使用：

```bash
git fetch origin
git reset --hard origin/<branch>
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

⚠️ 警告：`reset --hard` 會丟掉 local-only commit；contributor 必須先把
in-flight branch push 出去（push 會被拒，得 force-push 自己的 branch
到備援 remote 或 stash）。

## 步驟 6：重簽 TLS 憑證

舊私鑰在改寫歷史前已外洩，**必須**重簽。執行：

```bash
cd $HOME/ANILA
export ANILA_STATE_DIR=/var/lib/anila
export ANILA_TLS_CERTS_DIR=$ANILA_STATE_DIR/tls
bash infra/deployment/scripts/reissue-tls-cert.sh
```

該 script 會：
1. 從 `ANILA_CERT_SAN` 讀 SAN；未設定時使用主平台、n8n、GitLab、
   code-server、localhost 與既有 LAN IP 的安全預設。正式換發前仍須核對實際主機名。
2. 產生 `$ANILA_TLS_CERTS_DIR/server.{key,crt}`，覆蓋舊檔。
3. 把舊 pair 放到 `$ANILA_STATE_DIR/tls-archive/<timestamp>/`（不暴露給 nginx mount）。
4. 顯示新憑證 fingerprint。

完成後 restart nginx：

```bash
docker compose restart nginx
```

## 步驟 7：驗證

```bash
# 1. 檢查歷史已乾淨
git log --all --full-history --oneline -- '*.key' '*.pem'
# 應該回空

# 2. 檢查新憑證
openssl x509 -in "$ANILA_TLS_CERTS_DIR/server.crt" -noout -fingerprint -sha256
# 應該是新 fingerprint

# 3. 連線測試
curl -sk -I https://localhost/health | head -1
# HTTP/2 200
```

## 步驟 8：最後通知

於 channel 發完成通知，附上新憑證 SHA-256 fingerprint，方便團隊在 client
端 pin。

---

## 何時需要再執行此 runbook

- 任何時候有人不小心 commit 了 `*.key` / `*.pem`（hooks 與 `.gitignore`
  已經多重防護，但若仍漏網要立即執行）。
- 憑證到期前 30 天（見 `server.crt` 的 `notAfter` 欄位）。
- 任何已知或疑似的私鑰外洩事件。

## 為什麼不建議「只重簽不改寫歷史」

- 舊 commit hash 對應的歷史快照仍含明文私鑰，攻擊者只要 clone 任何時間
  點的 repo（含 fork、CI cache、本機 backup）就能拿到。
- 即使 deploy 已經換新憑證，舊憑證仍可被攻擊者用來偽造 TLS endpoint，
  受害的 client（例如 pin 舊 fingerprint 的 SDK）不會察覺。
- 改寫歷史是唯一能讓「未來新 clone 拿不到舊私鑰」的做法。
