#!/usr/bin/env bash
# ============================================================================
# fix-runtime-ownership.sh
# ----------------------------------------------------------------------------
# 把 host 端 bind mount 的所有權/權限,對齊 csp 與 ingestion-worker 降權之後的
# runtime UID/GID。**冪等,重跑安全**。deploy-prod.sh(deploy / up / rebuild
# 三條路徑)與 intranet-deploy.sh [4c] 都已經接進去,正常情況不必手動跑。
#
# 為什麼需要這一步
# ----------------
# csp / ingestion-worker 從 2026-08-06 起以 uid 10001 跑(FAKE-CONTROLS #50)。
# 但 bind mount 的所有權**是 host 決定的,映像裡 chown 什麼都沒用**。四個掛載
# 各自的壞法都是同一個形狀 —— 容器全綠、功能靜默死掉:
#
#   share/uploads/ingestion   Docker 自動建的話是 root:root 755
#                             → 上傳寫不進去,FastAPI 回 500,容器仍 healthy。
#   share/attachments         同上(attachment_service.py:307 要在裡面 mkdir)。
#   secrets/jwt-private.pem   0600 且屬於產金鑰的那個身分
#                             → csp 讀不到私鑰 → /.well-known/jwks.json 回 500、
#                               登入發不出 token、anila-studio crash-loop。
#   share/pki/*               管理員手放的 CA;若是 root 所有的 0600
#                             → SSL_CERT_FILE 讀不到 → csp **所有出向 https**
#                               全掛(SSL_CERT_FILE 是取代信任庫,不是疊加),
#                               而健康檢查完全看不出來。
#
# 做法上的三個刻意選擇
# ------------------
# 1. **不用 host 的 sudo chown,改用 root 一次性容器。** 內網主機跟開發機的
#    帳號不一樣,但容器裡的 root 到處都一樣;而且部署者本來就有 docker 權限,
#    不需要再多要一次 sudo。(同樣的手法 compose 裡已經有先例:
#    infra/compose/platform.yml 的 codeserver-init。)
#
# 2. **secrets/ 只對「指名的那幾個檔」補 group 讀,不做 chgrp -R / chmod -R。**
#    無差別放寬會讓**未來任何**丟進 secrets/ 的私鑰(TLS server.key、卡片 CA
#    的簽章金鑰…)自動對 gid 10001 開讀,而且沒有人會發現。所以這裡是白名單:
#    只有執行期使用者真的要讀的檔案才放寬,其餘原封不動,並在輸出裡列出來
#    讓操作者看得見這條界線。
#
#    ⚠ 擁有者是誰,這支腳本**一律不動**,而兩條部署路徑產出的擁有者不一樣:
#      - deploy-prod.sh 的 ensure_jwt_keypair 在 host 上用 openssl 產
#        → 擁有者 = 跑腳本的那個 host 帳號,他之後還能改能刪。
#      - intranet-deploy.sh [4b] 用 `--user 0:0` 的容器產
#        → 擁有者 = **root**。非 root 的部署者沒有 sudo 就改不動、刪不掉。
#        這是降權之前就有的行為(舊的 one-shot 也是 root),本包沒有改變它 ——
#        但別以為兩條路徑長得一樣。
#
# 3. **share/pki 是遞迴的,secrets/ 不是。** 那裡放的是憑證(CA 公開憑證鏈),
#    不是金鑰 —— 公開資料,對執行期使用者開讀沒有機密性問題
#    (infra/compose/platform.yml:561 已經把 share/pki 歸類為公開資料)。
#    而且檔名不固定:intranet-deploy 用 model-ca.pem,但管理員也可能直接把
#    IT 給的 PEM 丟進去,所以不能寫白名單。
#
# 用法:
#   bash infra/deployment/scripts/fix-runtime-ownership.sh [CSP_IMAGE]
#   CSP_IMAGE 預設 ${COMPOSE_PROJECT_NAME:-anila}-csp:latest(跟著 compose project name
#   → 映像名 <project>-<service>;intranet-deploy.sh 的 [4b] 用的也是這個名字。
#   出貨 project 是 anila;另設了 COMPOSE_PROJECT_NAME 就會跟著那顆)。
#   這支只借它當「有 chown/chmod 的 root 容器」,不跑裡面的任何應用程式碼。
# ============================================================================
set -euo pipefail

# ⚠ 不變式:這組數字必須等於 infra/docker/csp.Dockerfile 與
# services/ingestion-worker/Dockerfile 裡 groupadd/useradd 的號碼。
# 三者共用 share/uploads/ingestion 這顆 inode,任一邊漂開就是單向壞掉,
# 而且兩個容器都會照常 healthy。改號碼時 grep 10001。
ANILA_RUNTIME_UID=10001
ANILA_RUNTIME_GID=10001

CSP_IMAGE="${1:-${COMPOSE_PROJECT_NAME:-anila}-csp:latest}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

# 目錄不存在時先建出來,免得 docker 自己建成 root:root(那正是要修的東西)。
mkdir -p share/uploads/ingestion share/attachments share/pki secrets

# ── 映像不在:大聲警告,但**不中止** ────────────────────────────────────────
# 這一步是「對齊」不是「閘門」。全新主機上如果映像還沒 build/load,誠實的
# 失敗是 compose 自己那句 image not found,不是我們搶在它前面 exit 1 ——
# 那會把「還沒 build」誤報成「所有權有問題」,而且讓 deploy-prod.sh up 在
# set -e 底下直接死在 compose 有機會講話之前。
# 有 build 的路徑(deploy / rebuild)一律把這一步排在 build 之後,走不到這裡。
if ! docker image inspect "$CSP_IMAGE" >/dev/null 2>&1; then
  echo "⚠ 找不到映像 $CSP_IMAGE — **跳過**所有權對齊(不視為錯誤)。" >&2
  echo "  接下來 compose 會自己說映像的事。若 csp 起來了但上傳 500 / 登入 500," >&2
  echo "  那就是這一步沒跑到,手動補:" >&2
  echo "    bash infra/deployment/scripts/fix-runtime-ownership.sh <實際的 csp 映像名>" >&2
  exit 0
fi

# --network none:這一步只碰檔案系統,不需要網路。
# --user 0:0:映像預設已經是 uid 10001,要 chown 別人的檔案必須明確要回 root。
docker run --rm --user 0:0 --network none \
  -e FIX_UID="$ANILA_RUNTIME_UID" \
  -e FIX_GID="$ANILA_RUNTIME_GID" \
  -v "$REPO_ROOT/share/uploads/ingestion:/fix/ingestion-uploads" \
  -v "$REPO_ROOT/share/attachments:/fix/attachments" \
  -v "$REPO_ROOT/share/pki:/fix/pki" \
  -v "$REPO_ROOT/secrets:/fix/secrets" \
  --entrypoint /bin/sh "$CSP_IMAGE" -c '
set -eu

# ── 1. RW 資料目錄:整包交給 runtime user ──────────────────────────────────
# csp 與 ingestion-worker 是同一組 uid/gid,所以一次 chown 兩邊都通。
# 只 chown 不 chmod:root 建出來的目錄本來就是 0755 / 檔案 0644,換了擁有者
# 之後 owner 位元就已經是 rwx / rw。
for d in /fix/ingestion-uploads /fix/attachments; do
  chown -R "$FIX_UID:$FIX_GID" "$d"
done

# ── 2. secrets/:白名單,只放寬指名的檔 ────────────────────────────────────
# 兩個 helper 都是**只加不減**:不動 owner、不動 other 位元、不動 u 位元。
# 所以重跑不會越開越寬,也不會把操作者手動收緊的東西again打開。
widen_dir() {   # 目錄:補 traverse,不然裡面的檔連 stat 都做不到
  [ -d "$1" ] || return 0
  chgrp "$FIX_GID" "$1"; chmod g+rx "$1"
}
widen_file() {  # 檔案:只補 group 讀
  [ -f "$1" ] || return 0
  chgrp "$FIX_GID" "$1"; chmod g+r "$1"
}

widen_dir  /fix/secrets
widen_file /fix/secrets/jwt-private.pem          # csp 簽 access token
widen_file /fix/secrets/jwt-public.pem           # csp 發 JWKS
# 本機開發用的假讀卡機測試 CA。csp 只讀 bundle(CARD_CA_BUNDLE_PATH),
# 同目錄的 *.key(簽測試卡用的私鑰)刻意**不**放寬 —— 它們是 mock 讀卡機
# 自己要用的,csp 一輩子不會讀。
widen_dir  /fix/secrets/dev-card-ca
widen_file /fix/secrets/dev-card-ca/dev_ca_bundle.pem

# ── 3. share/pki:遞迴(公開憑證,理由見檔頭第 3 點)───────────────────────
# g+rX 的大寫 X 只對目錄、或本來就有執行位元的檔案加 x。
chgrp -R "$FIX_GID" /fix/pki
chmod -R g+rX /fix/pki

# ── 回報收斂結果 ───────────────────────────────────────────────────────────
# -n 用數字印 uid/gid:容器裡沒有 10001 這個帳號的名字,印名字會變問號。
echo "fix-runtime-ownership: 目標 runtime = ${FIX_UID}:${FIX_GID}"
ls -ldn /fix/ingestion-uploads /fix/attachments /fix/pki /fix/secrets
# 用 if 而不是 `[ -f x ] && ls`:後者在兩個檔都不存在時會讓 for 迴圈以非零
# 狀態結束,而這整段跑在 `set -e` 底下。
for f in /fix/secrets/jwt-private.pem /fix/secrets/jwt-public.pem; do
  if [ -f "$f" ]; then ls -ln "$f"; fi
done
if [ ! -f /fix/secrets/jwt-private.pem ]; then
  echo "  (尚無 jwt-private.pem — 產生之後重跑這支即可,它是冪等的)"
fi

# 把「刻意沒有動」的東西印出來,讓白名單這條界線是看得見的。
# 沒有這幾行,以後有人把私鑰丟進 secrets/ 然後納悶 csp 為什麼讀不到。
_untouched=$(find /fix/secrets -type f \
  ! -name jwt-private.pem ! -name jwt-public.pem ! -name dev_ca_bundle.pem \
  2>/dev/null || true)
if [ -n "$_untouched" ]; then
  echo "  secrets/ 底下**刻意未放寬**(模式與群組原封不動):"
  echo "$_untouched" | while IFS= read -r f; do ls -ln "$f" | sed "s|^|    |"; done
  echo "    ↑ 若其中有 csp 執行期真的要讀的檔,請在本腳本加一行 widen_file,"
  echo "      不要改成 chmod -R —— 那等於把未來所有的私鑰都一起開出去。"
fi
'
