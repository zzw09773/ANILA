#!/usr/bin/env bash
# ============================================================================
# intranet-deploy.sh — 內網一條龍部署 (restart/from-redesign annotated-tag bundle / V1.0.0 卡片登入)
# ----------------------------------------------------------------------------
# 在內網平台主機 (.15) 上,一支互動式腳本跑完:
#   [1] TLS 憑證抽取 (從 server.pfx)
#   [2] 模型 gateway 出向 CA (share/pki/model-ca.pem)
#   [3] 產 / 更新 .env (自動生 secret + 互動填 gateway key / owner 員工編號)
#   [4] load image (呼叫 image 包的 INTRANET-LOAD.sh,含 SHA256 驗檔 + docker load)
#   [5] 建 docker network
#   [6] docker compose (bundle image override + 可選 ASR profile) up -d --no-build
#   [7] 等 healthy + 驗證
#
# 用法 (在與 image bundle 對應的 annotated-tag repo 根目錄;不要求特定 branch):
#   bash infra/deployment/intranet/intranet-deploy.sh [IMAGE_BUNDLE_DIR]
#   IMAGE_BUNDLE_DIR 預設自動找 ./intranet-prod-v1.0.0;找不到會提示輸入。
#
# 重跑安全:偵測到既有 .env 預設「保留現有 secret」— 避免重生 DB 密碼炸掉既有
# DB。只有你明確選擇重生才會換 secret(並先備份舊 .env)。
#
# 注意:這支不碰 model image / 權重(第一版走 .12 gateway)。後續日常操作用
# infra/deployment/scripts/deploy-prod.sh {status|logs|restart|down}。
# ============================================================================
set -euo pipefail

c()    { printf '\033[%sm%s\033[0m' "$1" "$2"; }
info() { echo "$(c '1;36' '▶') $*"; }
ok()   { echo "$(c '1;32' '✓') $*"; }
warn() { echo "$(c '1;33' '⚠') $*"; }
die()  { echo "$(c '1;31' '✗') $*" >&2; exit 1; }
ask()       { local p="$1" d="${2:-}" a; read -rp "$(c '1;35' '?') ${p}${d:+ [$d]}: " a; printf '%s' "${a:-$d}"; }
asksecret() { local p="$1" a; read -rsp "$(c '1;35' '?') ${p}: " a; echo >&2; printf '%s' "$a"; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

# 與 build-and-export-for-intranet.sh 對齊:bundle 的 image tag 是以這個 project name
# 產出的;INCLUDE_ASR=1 才把語音 profile 帶進有效組態。
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-anila-restart}"
INCLUDE_ASR="${INCLUDE_ASR:-1}"
# .15 是 CPU 主機(2× EPYC 9334),所以預設疊 CPU overlay,避免 ASR_DEVICE 落回
# cuda 造成 decoder crash-loop。GPU 主機請設 ASR_OVERLAY=infra/compose/asr-gpu.yml;
# ASR_OVERLAY= 空字串表示不疊 overlay,由操作者自行承擔組態責任。
ASR_OVERLAY="${ASR_OVERLAY-infra/compose/asr-cpu.yml}"
if [ "$INCLUDE_ASR" = "1" ] && [ -n "$ASR_OVERLAY" ] && [ ! -f "$ASR_OVERLAY" ]; then
  die "ASR overlay 檔案不存在: $ASR_OVERLAY"
fi

# ── .env 存取:腳本眼中的「已設」必須等於 compose 眼中的「已設」 ─────────────
# 舊版用 `^KEY=` 認鍵,compose 不是這樣解的。實測 v2.36.2,下面每一種寫法
# compose 都讀成同一個值,而 `^KEY=` 一種都認不出來:
#     ` KEY=1`(行首空白)  `\tKEY=1`  `export KEY=1`  `KEY =1`(= 前空白)
#     `KEY="1"` / `KEY='1'`(引號)  `KEY=1 `(行尾空白)  `KEY=1\r\n`(CRLF)
# 差別會直接吃掉操作者的設定:手寫 ` ANILA_ALLOW_GRPC_ENDPOINT=1` 之後重跑本
# 腳本 → grep 看不見那一行 → 檔尾又 append 一行 `...=0` → compose 取最後一筆 →
# 剛開起來的旗標被靜默關掉。症狀只是註冊 400,現場反推不出是腳本改的,而
# preserve_flag 存在的理由正是要防這件事。
# (同一套判準已經在 infra/deployment/scripts/deploy-prod.sh 的 check_env 裡:
#  `^[[:space:]]*(export[[:space:]]+)?KEY[[:space:]]*=` —— 這裡跟它對齊。)
_trim() {  # 去前後空白(對齊 url_guard._env_flag 的 .strip())
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  printf '%s' "${s%"${s##*[![:space:]]}"}"
}
_env_key_re() { printf '^[[:space:]]*(export[[:space:]]+)?%s[[:space:]]*=' "$1"; }
env_has_key() { grep -qE "$(_env_key_re "$1")" .env 2>/dev/null; }

set_env() {  # set_env KEY VALUE — 去重後 append (literal,不怕特殊字元/sed 跳脫)
  local key="$1" val="$2"
  [ -f .env ] || die ".env 不存在"
  grep -vE "$(_env_key_re "$key")" .env > .env.tmp 2>/dev/null || true
  mv .env.tmp .env
  printf '%s=%s\n' "$key" "$val" >> .env
}
secret_set() {  # secret_set KEY VALUE KIND — 寫入成功後報告這一次寫入
  local key="$1" val="$2" kind="$3"
  set_env "$key" "$val"
  info "  secret: $key（$kind）"
}
# 回傳 compose 會讀到的那個值:重複鍵取**最後一行**(實測 v2.36.2:FOO=first /
# FOO=second → second;取第一行會讓腳本看到的值與 stack 實際用的值不同),
# 去 CR、去引號、去 ` #` 行尾註解(compose 只吃「空白+#」這一種)、去前後空白。
#
# ⚠ 引號要**先**認、而且只認到「下一個同款引號」為止,不能要求整串頭尾都是引號。
# 舊版的 `'"'*'"'` 樣式對 `KEY="1" # 註解` 不成立(結尾是註解不是引號),掉進
# `*)` 分支只砍掉註解、引號留著 → `"1" != "1"` → **compose 讀成 1、腳本卻不警示**。
# 這是本檔矩陣裡兩種情況(引號、行尾註解)的組合,單獨各自都對、合起來就漏。
# 實測 compose v2.36.2(2026-08-05,本機 `docker compose config`):
#     `K="1" # note` → 1     `K='1' # note` → 1     `export K="1" # note` → 1
#     `K="1" # note` + CRLF → 1                     `K="1"junk` → 1
#     `K="1 # note"` → `1 # note`(引號內的 # 不是註解)  `K=" 1 "` → ` 1 `
#     `K=1#note` → `1#note`  `K=#1` → `#1`(compose 只認「空白+#」)
# (`K="1` 這種沒收尾的引號 compose 直接報錯拒絕渲染,這裡怎麼判都不影響結果。)
get_env() {
  local line val
  line="$(grep -E "$(_env_key_re "$1")" .env 2>/dev/null | tail -1)" || true
  [ -n "$line" ] || return 0
  val="${line%$'\r'}"
  val="$(_trim "${val#*=}")"
  case "$val" in
    '"'*) val="${val#\"}"; case "$val" in *'"'*) val="${val%%\"*}" ;; esac ;;
    "'"*) val="${val#\'}"; case "$val" in *"'"*) val="${val%%\'*}" ;; esac ;;
    *)    val="$(_trim "${val%% #*}")" ;;
  esac
  printf '%s' "$val"
}

preflight_share_dirs() {
  local dir parent owner
  owner="$(id -u):$(id -g)"
  for dir in "$@"; do
    if [ -e "$dir" ]; then
      if [ ! -d "$dir" ] || [ ! -w "$dir" ] || [ ! -x "$dir" ]; then
        die "share 目錄不可寫: $dir
  若它是 root-owned 的空目錄，先執行: rmdir $dir
  （之後腳本會重新 mkdir，借用 parent 權限）；若不是空目錄，需由管理員 chown -R $owner \"$dir\" 後重跑。"
      fi
      continue
    fi

    parent="$dir"
    while [ ! -e "$parent" ]; do
      parent="$(dirname "$parent")"
    done
    if [ ! -d "$parent" ] || [ ! -w "$parent" ] || [ ! -x "$parent" ]; then
      die "share 路徑無法建立: $dir（最近既有 parent $parent 不可寫）
  請先讓 parent 可寫；若 $parent 是 root-owned 的空目錄，先執行: rmdir $parent
  （之後腳本會重新 mkdir，借用更上層 parent 權限）；若不是空目錄，需由管理員 chown -R $owner \"$parent\" 後重跑。"
    fi
  done
}

# ── url_guard 的三個 opt-in 旗標:預設 0,但**保留操作者已設的值** ─────────
# 這三個都是 runbook §3.1b/§3.1c 明文要求現場自己開的。硬寫 0 的版本會讓
# 「重跑一次部署腳本」把操作者剛剛開起來的東西靜默關掉 —— 症狀只是註冊/健檢
# 400,現場幾乎不可能反推到「是部署腳本把它改回去了」。缺鍵時仍補 0,所以
# 全新部署的預設姿態沒有變寬,變的只是「腳本不再推翻現場的決定」。
# 鍵已經在就**一個字都不改**(不重寫、不搬到檔尾):重寫會吃掉操作者的註解與
# 引號,而 compose 讀得到就夠了。警示的判準是「容器裡會不會拿到 1」:引號與
# 行尾註解由 compose 拆掉(見 get_env 上方的實測),app 端的 ``_env_flag`` 只
# 再 ``.strip() == "1"``。所以 `KEY="1"`、`KEY="1" # 註解` 都會被警示。
# ⚠ 舊註解寫「ANILA_ENV=production → 模型 http 一律 fail-closed,不受任何旗標
#    放行」——那句自 2026-07-29(PLAN P0.2)起就不成立了:model kind 的 http
#    改成純由 ANILA_ALLOW_HTTP_ENDPOINT 決定、與 env 無關,所以那一行真的會把
#    §3.1b 的本機模型組態關掉。
preserve_flag() {  # preserve_flag KEY 提醒字串
  local key="$1" note="$2"
  if env_has_key "$key"; then
    [ "$(_trim "$(get_env "$key")")" = "1" ] && warn "$key=1 — $note"
  else
    set_env "$key" 0
  fi
  return 0
}

echo "============================================================"
echo " ANILA 內網一條龍部署 — V1.0.0 (restart/from-redesign annotated-tag bundle / 卡片登入)"
echo "============================================================"

# ── 0. 前置檢查 ───────────────────────────────────────────────────────────
info "[0/7] 前置檢查"
command -v docker >/dev/null   || die "找不到 docker"
command -v openssl >/dev/null  || die "找不到 openssl"
docker info >/dev/null 2>&1    || die "docker daemon 沒在跑 / 當前使用者無權限"
[ -f compose.yaml ] && [ -f .env.example ] \
  || die "請在 ANILA repo 根目錄執行(找不到 compose.yaml / .env.example)"
br="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
[ "$br" = '?' ] || info "目前 git 分支是 '$br';內網交付以 bundle 的 MANIFEST/tag+commit 為準,不要求特定 branch"

BUNDLE="${1:-}"
if [ -z "$BUNDLE" ]; then
  for cand in ./intranet-prod-v1.0.0 ../intranet-prod-v1.0.0 ./intranet-images-export /tmp/anila-images-export; do
    [ -f "$cand/INTRANET-LOAD.sh" ] && { BUNDLE="$cand"; break; }
  done
fi
[ -n "$BUNDLE" ] || BUNDLE="$(ask 'image 包資料夾路徑 (含 INTRANET-LOAD.sh)')"
[ -f "$BUNDLE/INTRANET-LOAD.sh" ] || die "在 '$BUNDLE' 找不到 INTRANET-LOAD.sh"
[ -f "$BUNDLE/intranet-image-overrides.yml" ] \
  || die "image 包缺少 intranet-image-overrides.yml — 舊 bundle 早於 digest fix,請重新匯出 bundle"
BUNDLE="$(cd "$BUNDLE" && pwd)"
ok "image 包: $BUNDLE"

# ── 1. TLS 憑證 ───────────────────────────────────────────────────────────
info "[1/7] TLS 憑證 (wildcard *.ai.ncsist.org.tw)"
mkdir -p infra/nginx/certs
CRT=infra/nginx/certs/server.crt
KEY=infra/nginx/certs/server.key
DO_TLS=1
if [ -f "$CRT" ] && [ -f "$KEY" ]; then
  warn "已有 server.crt / server.key"
  [ "$(ask '重新從 pfx 抽取?(會覆蓋) [y/N]' N)" = y ] || DO_TLS=0
fi
if [ "$DO_TLS" = 1 ]; then
  PFX="$(ask 'server.pfx 路徑')"; [ -f "$PFX" ] || die "找不到 $PFX"
  PFXPW="$(asksecret 'pfx 密碼 (空就直接 Enter)')"
  # server.crt 抽 fullchain:leaf(client cert)在前 + pfx 內所有中繼 CA,瀏覽器才能
  # 驗完整鏈(只放 leaf 在缺中繼的環境會跳「憑證不受信任」)。sed 只留 PEM 區塊,
  # 去掉 openssl 的 Bag Attributes 雜訊。
  {
    openssl pkcs12 -in "$PFX" -clcerts -nokeys -legacy -passin "pass:$PFXPW" 2>/dev/null
    openssl pkcs12 -in "$PFX" -cacerts -nokeys -legacy -passin "pass:$PFXPW" 2>/dev/null
  } | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' > "$CRT"
  [ -s "$CRT" ] && grep -q 'BEGIN CERTIFICATE' "$CRT" || die "抽 server.crt 失敗(pfx 路徑/密碼?)"
  openssl pkcs12 -in "$PFX" -nocerts -noenc -legacy -passin "pass:$PFXPW" 2>/dev/null | openssl pkey > "$KEY" || die "抽 server.key 失敗"
  chmod 600 "$KEY"
  _ncert=$(grep -c 'BEGIN CERTIFICATE' "$CRT")
  ok "TLS 憑證抽取完成(fullchain $_ncert 張:leaf + $((_ncert - 1)) 中繼)"
fi
echo "    $(openssl x509 -in "$CRT" -noout -subject -enddate 2>/dev/null | tr '\n' ' ')"
openssl x509 -in "$CRT" -noout -subject 2>/dev/null | grep -q 'ai.ncsist.org.tw' \
  || warn "subject 看起來不是 *.ai.ncsist.org.tw — 確認憑證對不對"

# ── 2. 模型 gateway 出向 CA ───────────────────────────────────────────────
info "[2/7] 模型 gateway CA (對 https://aiagent2.ai.ncsist.org.tw 的出向 TLS 信任)"
# share/* 是 nginx(static)、csp + ingestion-worker(uploads)的 bind-mount 來源;
# 先建好,否則 docker 會以 root 自動建空目錄(權限/擁有者錯亂)。
# 這個檢查必須在任何 mkdir/cp 前做:root-owned 空目錄可 rmdir 讓腳本借用 parent
# 權限重建;已有內容的目錄則需要管理員 chown,不能讓 [2/7] 寫到一半才失敗。
preflight_share_dirs share/pki share/static share/uploads share/uploads/ingestion share/attachments
mkdir -p share/pki share/static share/uploads/ingestion share/attachments
MCA=share/pki/model-ca.pem
# 內網模型 gateway 走 *.ai.ncsist.org.tw,憑證由中科院 CSPKI 簽發
# (CSPKI Root CA G1 → 中科院憑證管理中心 G1 → leaf)。中科院整套 PKI 同一條根:
# PKI 卡與內網伺服器憑證皆 CSPKI 簽。repo 內 cspki_ca_bundle.pem 是「卡片登入驗章」
# 釘死的同一套信任錨(Root + 中繼),正好就是這條鏈 → 直接當 model-ca,csp 即可
# 信任 .12 gateway 的 https。免下載、免跟 IT 要,離線就有。
CSPKI_BUNDLE=services/csp/app/services/cspki_ca_bundle.pem
if [ -f "$MCA" ]; then
  ok "已有 $MCA (沿用,不覆蓋)"
elif [ -f "$CSPKI_BUNDLE" ]; then
  cp "$CSPKI_BUNDLE" "$MCA"
  ok "model-ca.pem ← CSPKI bundle (Root CA G1 + 中科院憑證管理中心;內網 https 同一套 CA)"
else
  warn "找不到 $CSPKI_BUNDLE — 改由 IT 提供"
  P="$(ask 'model CA PEM 路徑 (IT 給的;留空則稍後手動放 share/pki/model-ca.pem)')"
  if [ -n "$P" ] && [ -f "$P" ]; then cp "$P" "$MCA"; ok "已複製 model-ca.pem"
  else warn "略過 — 部署前務必把 model CA 放到 $MCA,否則 csp 連 gateway 會 TLS 失敗"; fi
fi

# ── 3. .env ──────────────────────────────────────────────────────────────
info "[3/7] 產生 / 更新 .env"
REGEN=1
if [ -f .env ]; then
  warn "已有 .env"
  if [ "$(ask '保留現有 secret?(建議 Y — 重生 DB 密碼會炸掉既有 DB) [Y/n]' Y)" = n ]; then
    cp .env ".env.bak.$(date +%s)"; warn "已備份舊 .env;將重生 secret"
  else
    REGEN=0
  fi
else
  cp .env.example .env; ok "由 .env.example 建立 .env"
fi

# 預設值來源(選用):image 包裡的 intranet-defaults.env(刻意不進 git,實體隨包帶入
# air-gap)。提供 ADMIN_PASSWORD / CODESERVER_PASSWORD / CARD_INITIAL_OWNERS /
# GITLAB_ROOT_PASSWORD 等;沒提供的 secret 一律 openssl 隨機生成。
# 安全:不用 `source`(被竄改的 defaults 檔會執行任意指令),改嚴格解析 KEY=VALUE
# + 白名單;非白名單 / 註解 / 空行一律略過。
DEFAULTS="$BUNDLE/intranet-defaults.env"
if [ -f "$DEFAULTS" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in
      ADMIN_PASSWORD|CODESERVER_PASSWORD|CARD_INITIAL_OWNERS|GITLAB_ROOT_PASSWORD|SECRET_KEY|CSP_SECRET_KEY|CSP_SERVICE_TOKEN|ASR_DECODER_TOKEN|CSP_DB_PASSWORD|CSP_APP_DB_PASSWORD|INTERNAL_PLATFORM_API_KEY)
        _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"   # 去頭尾引號
        printf -v "$_k" '%s' "$_v" ;;                                # 賦值,非 eval
      *) : ;;
    esac
  done < "$DEFAULTS"
  ok "已套用 $DEFAULTS 的預設值(僅限白名單 KEY)"
fi

NEWLY_GENERATED_KEYS=()
if [ "$REGEN" = 1 ]; then
  info "  secret:有預設用預設,否則 openssl 隨機生成"
  SECRET_KEY_VALUE="${SECRET_KEY:-${CSP_SECRET_KEY:-$(openssl rand -hex 32)}}"
  set_env SECRET_KEY                "$SECRET_KEY_VALUE"
  # 兩行同值是對 compose dotenv 邊角行為的實測防禦,勿刪其一
  set_env CSP_SECRET_KEY            "$SECRET_KEY_VALUE"
  set_env CSP_SERVICE_TOKEN         "${CSP_SERVICE_TOKEN:-$(openssl rand -hex 32)}"
  set_env ASR_DECODER_TOKEN         "${ASR_DECODER_TOKEN:-$(openssl rand -hex 32)}"
  set_env INTERNAL_PLATFORM_API_KEY "${INTERNAL_PLATFORM_API_KEY:-sk-internal-$(openssl rand -hex 24)}"
  set_env ADMIN_PASSWORD            "${ADMIN_PASSWORD:-$(openssl rand -base64 24)}"
  set_env CSP_DB_PASSWORD           "${CSP_DB_PASSWORD:-$(openssl rand -hex 32)}"
  set_env CSP_APP_DB_PASSWORD       "${CSP_APP_DB_PASSWORD:-$(openssl rand -hex 32)}"
  set_env CODESERVER_PASSWORD       "${CODESERVER_PASSWORD:-$(openssl rand -base64 24)}"
  # gitlab root 初始密碼:與其他 secret 同 parity — 只在 REGEN(全新/重生)寫;
  # REGEN=0(保留現有)時不動,避免 re-run 偷改既有密碼。compose 用 env 帶入,
  # 只在 gitlab 首次 reconfigure 生效。
  [ -n "${GITLAB_ROOT_PASSWORD:-}" ] && set_env GITLAB_ROOT_PASSWORD "$GITLAB_ROOT_PASSWORD"
else
  SECRET_KEY_VALUE="$(get_env SECRET_KEY)"
  [ -n "$SECRET_KEY_VALUE" ] || SECRET_KEY_VALUE="$(get_env CSP_SECRET_KEY)"
  [ -n "$SECRET_KEY_VALUE" ] || die ".env 缺 SECRET_KEY / CSP_SECRET_KEY"
  if [ -z "$(get_env SECRET_KEY)" ]; then
    secret_set SECRET_KEY "$SECRET_KEY_VALUE" "從別名補正名"
  elif [ -z "$(get_env CSP_SECRET_KEY)" ]; then
    # alias 缺席=這份 .env 多半寫於本腳本改版前,正是 #7 解析謎(值在、compose
    # 整檔讀不到)的族群——趁補別名的同一輪,順手把 canonical 行重寫成 set_env 的
    # 正規形。兩名俱在的 .env 已是本腳本寫過的形,所以不再動它:正規化只做這一族。
    secret_set SECRET_KEY "$SECRET_KEY_VALUE" "重寫既有行"
  elif [ "$(get_env CSP_SECRET_KEY)" != "$SECRET_KEY_VALUE" ]; then
    # SECRET_KEY 是 canonical；既有 alias 若漂移，只修正 alias。
    secret_set CSP_SECRET_KEY "$SECRET_KEY_VALUE" "重寫既有行"
  fi
  if [ -z "$(get_env CSP_SECRET_KEY)" ]; then
    # 兩行同值是對 compose dotenv 邊角行為的實測防禦,勿刪其一。
    secret_set CSP_SECRET_KEY "$SECRET_KEY_VALUE" "補別名"
  fi
  # ⚠ 刻意的不對稱,不要「修」它:preserve 模式下其他必填 secret 缺席一律 die
  # (使用者答「保留現有」,腳本就不無中生有;停下點名讓人去密碼管理器拿=好的失敗)。
  # ASR_DECODER_TOKEN 是唯一例外——它是後加的第八把,既有 .env 必缺,不補則所有
  # 既有部署重跑必死。這是**遷移期權宜**:第一次正式安裝落地後,這段應改回 die。
  # 把它改成通則(缺了就生)=重開 preserve 模式悄悄生祕密的洞(1061ecc3 剛關的那個)。
  if [ -z "$(get_env ASR_DECODER_TOKEN)" ]; then
    if [ -n "${ASR_DECODER_TOKEN:-}" ]; then
      ASR_DECODER_TOKEN_VALUE="$ASR_DECODER_TOKEN"
      ASR_DECODER_TOKEN_KIND="補既有值"
    else
      ASR_DECODER_TOKEN_VALUE="$(openssl rand -hex 32)"
      ASR_DECODER_TOKEN_KIND="新生成"
      NEWLY_GENERATED_KEYS+=(ASR_DECODER_TOKEN)
    fi
    secret_set ASR_DECODER_TOKEN "$ASR_DECODER_TOKEN_VALUE" "$ASR_DECODER_TOKEN_KIND"
  fi
fi

# 內網 strict 模式 + 卡片登入 + 模型 CA 路徑(每次都確保正確)
set_env ANILA_ALLOW_DEV_SECRET      0
set_env ANILA_ENV                   production
# MLSteam agent 是純 http NodePort → agent 專用旗標開 1。
set_env ANILA_ALLOW_HTTP_AGENT_ENDPOINT 1

# url_guard 的三個 opt-in 旗標:預設 0,但保留操作者已設的值(preserve_flag
# 定義在檔案上方,與其他 .env 存取函式放在一起)。
preserve_flag ANILA_ALLOW_HTTP_ENDPOINT \
  "放行 http:// 模型端點(runbook §3.1b 本機模型容器);模型走 https gateway 就該是 0"
preserve_flag ANILA_ALLOW_PRIVATE_ENDPOINT \
  "放行 RFC1918 私網 IP 端點(runbook §3.1c 直連 Triton 用);端點都是 FQDN 就該是 0"
preserve_flag ANILA_ALLOW_GRPC_ENDPOINT \
  "放行 cleartext grpc:// 模型端點(Triton);內網無 TLS 時才需要,有 grpcs:// 請改回 0"
set_env ANILA_AUTH_MODE             card-only
# 只在 model-ca.pem 真的有憑證時才指過去。ANILA_MODEL_CA_FILE → csp 的 SSL_CERT_FILE,
# 而 SSL_CERT_FILE 是「取代」整個系統信任庫(非疊加):指到空/壞檔 → csp 所有出向 https
# 全 CERTIFICATE_VERIFY_FAILED(連 agent 都連不上)。空字串則 fallback 系統 CA,無副作用。
if [ -s "$MCA" ] && grep -q 'BEGIN CERTIFICATE' "$MCA" 2>/dev/null; then
  set_env ANILA_MODEL_CA_FILE       /etc/anila/pki/model-ca.pem
else
  set_env ANILA_MODEL_CA_FILE       ""
  warn "model-ca.pem 無有效憑證 → 暫不設 ANILA_MODEL_CA_FILE(csp 用系統 CA);補好 CA 再 up -d csp"
fi
# codeserver workspace:下方必填檢查需要它;compose 搬到 infra/compose 後,
# 相對路徑以該目錄為基準,repo root = `../..`(與 .env.example 預設同值)。
[ -n "$(get_env CODESERVER_WORKSPACE)" ] || set_env CODESERVER_WORKSPACE ../..

echo
# owner 是最高權限種子帳號。不讓「被竄改的 bundle 預設 + 一個 Enter」就生效:
# 有預設就明確顯示出來、要求打 y 確認;否則一律手動輸入。
DEF_CIO="${CARD_INITIAL_OWNERS:-$(get_env CARD_INITIAL_OWNERS)}"
if [ -n "$DEF_CIO" ] && \
   [ "$(ask "偵測到預設 owner 員工編號 '$(c '1;33' "$DEF_CIO")' — 確定用這組?(N 則手動輸入) [y/N]" N)" = y ]; then
  CIO="$DEF_CIO"
else
  CIO="$(ask 'CARD_INITIAL_OWNERS — owner 員工編號 (CSV,含你自己,例 1147259,1234567)')"
fi
[ -n "$CIO" ] || die "CARD_INITIAL_OWNERS 不能空(否則沒人是 owner,進不了管理)"
ok "owner = $CIO"
set_env CARD_INITIAL_OWNERS "$CIO"

MGK="$(asksecret 'MODEL_GATEWAY_API_KEY — 在 .12 gateway 簽發的 key (還沒有就 Enter 跳過)')"
if [ -n "$MGK" ]; then set_env MODEL_GATEWAY_API_KEY "$MGK"; ok "已設 MODEL_GATEWAY_API_KEY"
else warn "MODEL_GATEWAY_API_KEY 留空 — 模型 proxy 暫時打不通。拿到後填進 .env 再 'docker compose up -d csp'"; fi

# 必填齊全檢查
for k in SECRET_KEY CSP_SECRET_KEY CSP_SERVICE_TOKEN ASR_DECODER_TOKEN INTERNAL_PLATFORM_API_KEY ADMIN_PASSWORD \
         CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD CODESERVER_WORKSPACE CARD_INITIAL_OWNERS; do
  [ -n "$(get_env "$k")" ] || die ".env 缺必填值: $k"
done
ok ".env 就緒 (strict + 卡片登入 + 模型走 .12 gateway)"

if [ "$REGEN" = 1 ]; then
  echo
  echo "$(c '1;33' '──── 請把以下 secret 存進密碼管理器(只顯示這一次) ────')"
  for k in ADMIN_PASSWORD SECRET_KEY CSP_SECRET_KEY CSP_SERVICE_TOKEN ASR_DECODER_TOKEN \
           INTERNAL_PLATFORM_API_KEY CSP_DB_PASSWORD CSP_APP_DB_PASSWORD CODESERVER_PASSWORD; do
    printf '  %-26s %s\n' "$k" "$(get_env "$k")"
  done
  echo "$(c '1;33' '──────────────────────────────────────────────────────')"
  read -rp "存好了按 Enter 繼續 ... " _
elif [ "${#NEWLY_GENERATED_KEYS[@]}" -gt 0 ]; then
  echo
  echo "$(c '1;33' '──── 本次新生成 secret，請存進密碼管理器(只顯示這一次) ────')"
  for k in "${NEWLY_GENERATED_KEYS[@]}"; do
    printf '  %-26s %s\n' "$k" "$(get_env "$k")"
  done
  echo "$(c '1;33' '──────────────────────────────────────────────────────')"
  read -rp "存好了按 Enter 繼續 ... " _
fi

# ── 4. load image ────────────────────────────────────────────────────────
info "[4/7] load image (SHA256 驗檔 + docker load;沿用 bundle image tags)"
bash "$BUNDLE/INTRANET-LOAD.sh"

# ── 4b. JWT 簽章金鑰 ───────────────────────────────────────────────────────
# csp 用這把 RSA 私鑰簽登入 access token,並對 anila-studio 等服務發 JWKS 公鑰。
# production 不在 csp runtime 自動生;缺這把:csp /.well-known/jwks.json
# 回 500、登入發不了 token、anila-studio 啟動 crash-loop。compose 以 :ro 把 ./secrets
# mount 進 csp:/app/secrets。必須在 [6] up 之前產好。需 csp image(故排在 load 之後)。
info "[4b/7] JWT 簽章金鑰 (secrets/jwt-private.pem)"
mkdir -p secrets
if [ -f secrets/jwt-private.pem ] && [ -f secrets/jwt-public.pem ]; then
  ok "已有 JWT keypair (重跑沿用,token 不失效)"
else
  # --user 0:0 是**必要的**,不是保險。csp image 自 2026-08-06 起預設 uid 10001
  # (FAKE-CONTROLS #50),而 $PWD/secrets 這個 bind mount 屬於 host 帳號 →
  # 不搶回 root 的話,這一步會在寫檔時 PermissionError,整個部署卡在這裡。
  # 產出來的私鑰是 root:root 0600;下一步 [4c] 再把 group 開給 runtime user。
  # image 名跟著 compose project 走(bundle 就是用這個 -p 打的);寫死另一個
  # project 的名字會在乾淨機器上 pull 失敗、整個部署死在這一步(2026-09-02 演練實撞)。
  docker run --rm --user 0:0 -v "$PWD/secrets:/out" --entrypoint python "${COMPOSE_PROJECT_NAME:-anila-restart}-csp:latest" \
    /app/scripts/generate-jwt-keypair.py --output-dir /out \
    && ok "已產生 JWT keypair (RSA-2048 / RS256 / PKCS#8)" \
    || die "JWT keypair 產生失敗 (csp image 在? scripts/generate-jwt-keypair.py 在?)"
fi

# ── 4c. bind mount 所有權對齊 ─────────────────────────────────────────────
# csp / ingestion-worker 以 uid 10001 跑,但 bind mount 的所有權是 host 說了算。
# 不修的症狀**不是**起不來,是**容器全綠、上傳回 500、JWKS 回 500、出向 https 全掛**
# —— 一條龍部署最不該留的那種。冪等,重跑安全。理由與細節在腳本檔頭。
# 排在 [4b] 之後(私鑰要先存在)、[2] 放 model CA 之後、[6] up 之前。
info "[4c/7] bind mount 所有權對齊 (share/uploads/ingestion、share/attachments、share/pki、secrets)"
bash infra/deployment/scripts/fix-runtime-ownership.sh "${COMPOSE_PROJECT_NAME:-anila-restart}-csp:latest" \
  && ok "所有權已對齊 runtime uid 10001" \
  || die "所有權對齊失敗 — 沒有這一步 csp 會 healthy 但上傳與登入都壞掉,不要跳過"

# ── 5. network ───────────────────────────────────────────────────────────
info "[5/7] docker network anila-models-net"
docker network inspect anila-models-net >/dev/null 2>&1 \
  && ok "已存在" \
  || { docker network create anila-models-net >/dev/null && ok "已建立"; }

# ── 6. up ────────────────────────────────────────────────────────────────
info "[6/7] docker compose up -d --no-build"
COMPOSE_BASE_ARGS=(-p "$COMPOSE_PROJECT_NAME" -f compose.yaml)
if [ "$INCLUDE_ASR" = "1" ] && [ -n "$ASR_OVERLAY" ]; then
  COMPOSE_BASE_ARGS+=(-f "$ASR_OVERLAY")
fi
COMPOSE_BASE_ARGS+=(-f "$BUNDLE/intranet-image-overrides.yml")
COMPOSE_PROFILE_ARGS=()
if [ "$INCLUDE_ASR" = "1" ]; then
  COMPOSE_PROFILE_ARGS=(--profile asr)
fi
COMPOSE_ARGS=("${COMPOSE_BASE_ARGS[@]}" "${COMPOSE_PROFILE_ARGS[@]}")
COMPOSE_UP_ARGS=("${COMPOSE_ARGS[@]}" up -d --no-build)
COMPOSE_CMD_TEXT=""
printf -v COMPOSE_CMD_TEXT ' %q' "${COMPOSE_UP_ARGS[@]}"
echo "    docker compose${COMPOSE_CMD_TEXT}"
docker compose "${COMPOSE_UP_ARGS[@]}"

# ── 7. 驗證 ──────────────────────────────────────────────────────────────
info "[7/7] 等 csp healthy + 驗證"
CID="$(docker compose "${COMPOSE_ARGS[@]}" ps -q csp 2>/dev/null || true)"
h='?'
for _ in $(seq 1 40); do
  h="$(docker inspect "$CID" --format '{{.State.Health.Status}}' 2>/dev/null || echo '?')"
  [ "$h" = healthy ] && break
  sleep 3
done
echo "    csp health: $h"
[ "$h" = healthy ] || warn "csp 未在預期時間內 healthy — 查 log: docker compose logs csp | tail -80"
if [ "$h" = healthy ]; then
  # csp healthy 且 [7/7] 尚未輸出 nginx/完成訊息時提醒，避免被後續輸出淹沒。
  # 檢查本身永遠回 0，不會把初裝提醒變成部署閘門。
  bash infra/deployment/scripts/check-departments.sh "${COMPOSE_ARGS[@]}"
fi
curl -skf -o /dev/null -w "    nginx https(443): %{http_code}\n" https://localhost/ 2>/dev/null \
  || warn "nginx 443 未回應(可能還在起,稍等再試 curl -sk https://localhost/health)"

echo
echo "============================================================"
ok "內網部署完成"
echo "  • 登入:員工從瀏覽器插卡 + HiPKI(localhost:16888)走卡片登入"
echo "  • 日常:infra/deployment/scripts/deploy-prod.sh {status | logs <svc> | restart | down}"
[ -z "${MGK:-}" ] && echo "  • $(c '1;33' '待辦'):MODEL_GATEWAY_API_KEY 拿到後填 .env → docker compose up -d csp"
echo "  • DNS:確認 anila.ai.ncsist.org.tw → 本機、aiagent2.ai.ncsist.org.tw → .12"
echo "============================================================"
