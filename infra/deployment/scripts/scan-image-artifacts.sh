#!/usr/bin/env bash
# scan-image-artifacts.sh
# ============================================================================
# 建好的映像裡到底有什麼 —— 對「不該被烘進映像的執行期產物」做**建後掃描**。
#
# 為什麼是掃描而不是再補 .dockerignore:
#   2026-08-06 驗收在 08-03 那包交付的 csp 映像裡撈出測試用 RSA 私鑰
#   (jwt-private.pem)與 25MB 開發期日誌;補了 `**/` 之後**重建**的映像裡
#   仍然有真實使用者附件(data/attachments)與 .pytest_cache。
#   build context 的排除規則永遠補不完,而建後看成品至少不管是哪條 COPY、
#   哪個 build stage、哪張 base image 帶進來的,都在同一個地方被看到。
#
# ⚠ 這道掃描**擋得住什麼、擋不住什麼**(不要把它當成「整個類都關掉了」):
#   擋得住 ——
#     (a) 下面 RULES 列的**檔名家族**(副檔名比對不分大小寫):
#         *.pem / *.key / *.ppk、*.pfx / *.p12、id_rsa* 這種沒有副檔名的
#         ssh 私鑰、.env 與 .env.*、*.log 與輪替壓縮日誌、secrets/ 目錄、
#         有內容的 logs/ 目錄、data/attachments、.pytest_cache
#     (b) **內容規則**:憑證/金鑰候選檔(*.pem / *.crt / *.key,以及任何被
#         白名單放行的命中檔)只要 bytes 裡出現私鑰區塊標頭,就是違規 ——
#         **白名單放行不了它**。這條是為了堵住「把私鑰改名成 cacert.pem
#         藏進 certifi 目錄」這種洗白路徑(2026-08-06 驗收實證的漏洞)。
#   擋不住(已知盲區,寫在這裡是為了不要有人以為掃過就乾淨)——
#     • 藏在其他副檔名裡的祕密:config.json 裡的 token、.py 裡寫死的密碼
#     • 壓縮檔/封裝檔內部:tar / zip / jar / whl 裡面的東西看不到
#     • 映像**設定**面的洩漏:ENV、build arg、LABEL、history —— docker export
#       只給檔案系統,這些完全不在掃描範圍內
#     • 二進位金鑰庫的內容(*.pfx/*.p12 是靠檔名擋的,沒有驗內容)
#
# 用法:
#   bash infra/deployment/scripts/scan-image-artifacts.sh <image> [<image> ...]
#   exit 0  = 全部乾淨
#   exit 1  = 至少一張有違規(逐張列出違規路徑)
#   exit 2  = 掃描本身失敗(映像不存在 / create / export 失敗 / 檔案清單是空的)
#   exit 3  = **自我測試沒過** —— 掃描器被改壞了,在掃任何映像之前就停
#
# 這支腳本已接進 infra/deployment/intranet/build-and-export-for-intranet.sh
# (build 之後、docker save 之前;有違規就中止匯出)。
# 也可以**單獨跑**——本機重建之後想確認一下、或事後稽核一張既有映像:
#   bash infra/deployment/scripts/scan-image-artifacts.sh anila-restart-csp:latest
# 沒有接進 deploy-prod.sh:本機重建是開發者自己的迴圈,擋在那裡只是煩;
# 髒映像變成**交付品**的地方是匯出,閘門就設在那裡。
#
# 掃描方法(刻意選最鈍的那個):
#   docker create(**不 start**)→ docker export → 第一趟 tar -t 取路徑清單,
#   第二趟只把「憑證/金鑰候選檔」抽出來驗內容。映像自己的 entrypoint/CMD
#   一行都不會跑,所以拿來掃來路不明的映像也安全;對被掃的映像是唯讀的。
#   相對地,`docker run ... find /` 要跑映像裡的 shell、`docker diff` 只看得到
#   容器層改了什麼(看不到映像本身烘進去的東西)—— 兩個都不合用。
#
# 環境變數:
#   SCAN_CONTAINER_PREFIX  暫時容器的名稱前綴(預設 anila-imgscan)。
#                          容器建完即刪(含 trap),改前綴只是為了在共用主機上
#                          一眼看出是誰留下的。
# ============================================================================
set -euo pipefail

CONTAINER_PREFIX="${SCAN_CONTAINER_PREFIX:-anila-imgscan}"

# ── 違規規則 ────────────────────────────────────────────────────────────────
# 路徑是 image rootfs 的相對路徑(沒有開頭的 /),目錄結尾帶 /。
# ⚠ 比對前路徑會**先轉小寫**,所以規則一律寫小寫,而 `SERVER.KEY`、`ID_RSA`
#   這種大寫變形一樣擋得住(2026-08-06 驗收就是用大寫副檔名溜過去的)。
# 每一條規則配一個人看得懂的名字,違規報告會印出來——被擋的人要知道是哪一類。
#
# ⚠ `__pycache__` **不是**違規,故意不列:那是 python 匯入時自己生的 bytecode
#   快取,映像裡有它既不是機密也不是使用者資料,列進來只會製造每次都要放行的
#   噪音,然後訓練大家對這份報告視而不見。
RULES=(
    'secret-material|(\.pem|\.key|\.ppk)$'
    'keystore|(\.pfx|\.p12)$'
    # 沒有副檔名的 ssh 私鑰:id_rsa / id_ed25519 / …,以及 deploy_rsa 這種尾綴寫法
    'ssh-private-key|(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)[^/]*$|(^|/)[^/]*_rsa$'
    # .env 與 .env.<anything>;.env.example / .sample / .template 走白名單放行
    'env-file|(^|/)\.env($|\.[^/]*$)'
    # 日誌含輪替與壓縮:app.log / app.log.1 / app.log.gz / app.log.1.gz
    'runtime-log|(\.log|\.log\.[0-9]+|\.log\.gz|\.log\.[0-9]+\.gz)$'
    'secrets-dir|(^|/)secrets/'
    'logs-dir-with-content|(^|/)logs/.'
    'user-attachments|(^|/)data/attachments(/|$)'
    'pytest-cache|(^|/)\.pytest_cache(/|$)'
)

# 內容規則的類別名(這一條**壓過白名單**,不放在 RULES 裡,因為它要讀檔不是比路徑)
CONTENT_RULE_NAME='private-key-content'
# 私鑰區塊標頭(grep 逐行比對,`.` 本來就跨不過換行)。
# 這樣寫吃得到 RSA / EC / OPENSSH / ENCRYPTED 各種變體,腳本檔案本身也不留
# 一整串長得像真金鑰的字面值。
PRIVATE_KEY_RE='BEGIN.*PRIVATE KEY'

# 要驗內容的候選檔:憑證/金鑰家族(不分大小寫)。
# 只驗這些是為了效能 —— 一張映像兩萬個檔,全部抽出來讀不切實際;而
# 「把私鑰藏成別的副檔名」本來就已經被檔名家族那組規則擋住了。
CONTENT_CANDIDATE_RE='(\.pem|\.crt|\.key)$'

# ── 白名單 ──────────────────────────────────────────────────────────────────
# 格式:<glob>|<理由>。glob 用 bash [[ == ]] 比對,`*` **會跨 /**,
# 所以 `*/certifi/cacert.pem` 等於「任何深度下的 certifi/cacert.pem」,
# 而 `*.env.example` 連最上層的 `.env.example` 也吃得到(`*` 可以是空字串)。
# ⚠ 白名單比對是**分大小寫**的(路徑原樣比),規則比對才轉小寫 ——
#   所以 `ETC/SSL/CERTS/evil.pem` 這種變形只會命中規則、不會被放行。
#
# 規矩(這份清單一定會長,長歪了整個掃描就白做了):
#   1. 每一條都要寫理由,而且理由要說明「為什麼這個檔在成品裡是對的」,
#      不能寫「掃描擋住了」。擋住了是現象不是理由。
#   2. 只放**公開**的東西(公開 CA 鏈、base image 自帶的信任庫與套件管理日誌)。
#      私鑰、使用者資料、開發期日誌沒有任何理由該被放行——那是要去修 Dockerfile
#      或 .dockerignore,不是來加白名單。
#   3. **寫到副檔名為止**,不要用目錄萬用字元。2026-08-06 驗收把
#      `etc/ssl/certs/planted-evil.key` 塞進去,舊的 `etc/ssl/certs/*` 就用
#      「公開 CA 憑證」這個理由把一把 .key 放行了 —— 白名單放行的東西
#      必須就是它的理由講的那個東西。
#   4. 加白名單的人要在 commit message 裡說是哪張映像、哪次建置撞到的。
#   5. 就算放行了,內容規則還是會跑;白名單沒有放行私鑰的權力。
ALLOWLIST=(
    '*app/services/cspki_ca_bundle.pem|內網 CSPKI 憑證鏈(root+中繼,公開資訊)。卡登驗章與出向 https 都靠它,**必須**在映像裡;拿掉的症狀是 csp 所有出向 https 全掛。前面留 `*` 是因為 csp 映像的 WORKDIR 是 /app,實際路徑會變成 app/app/services/…。'
    'etc/ssl/certs/*.pem|base image 的作業系統信任庫(公開 CA 憑證),不是本專案放進去的。只放行 .pem —— 同一個目錄下的 .key 沒有正當理由存在。'
    'usr/lib/ssl/*.pem|同上,Debian 把 openssl 的信任庫連結在這裡。'
    'usr/share/ca-certificates/*.pem|同上,ca-certificates 套件的來源檔(多數是 .crt,.pem 型的一起放行)。'
    'etc/ssl/cert.pem|alpine base image 的信任庫符號連結(2026-08-06 tar -tv 實測 → certs/ca-certificates.crt),anila-ui / anilalm / asr-gateway 這種 alpine 底的映像都會有。'
    'etc/ssl1.1/cert.pem|同上,alpine 給 openssl 1.1 的相容路徑(→ /etc/ssl/cert.pem)。'
    '*/site-packages/certifi/cacert.pem|certifi 套件自帶的公開 CA bundle,python 生態幾乎每個 https client 都要它。⚠ 這條只放行「檔名」;內容規則照跑,藏私鑰在這裡照樣是違規。'
    '*/pip/_vendor/certifi/cacert.pem|pip 內嵌的 certifi 副本,pip 裝在映像裡就會有。'
    '*/grpc/_cython/_credentials/roots.pem|grpcio 套件自帶的公開 root CA bundle(2026-08-06 在 csp 映像實測:115 段 CERTIFICATE、0 段私鑰)。'
    'usr/share/gnupg/sks-keyservers.netCA.pem|gnupg 套件自帶的 keyserver 公開 CA 憑證(base image 帶的,pptx-renderer 那類要裝 gnupg 的映像會有)。'
    'var/log/nginx/*.log|官方 nginx 映像把它們連到 /dev/stdout、/dev/stderr(2026-08-06 tar -tv 實測是 symlink、0 bytes),不是真的日誌檔。'
    'var/log/apk.log|nginx:1.30.4-alpine pinned base layer 自帶的 apk 安裝紀錄(2026-07-15,8908 bytes,203 lines);內容只有上游維護者的 apk 操作,0 行本專案資料、0 行 secrets。依 Q38 裁決不重整或壓平上游 base,以理由豁免。'
    'var/log/dpkg.log|Debian 套件管理器的安裝紀錄(base image 帶的,內容是套件名與版本)。'
    'var/log/alternatives.log|同上,update-alternatives 的紀錄。'
    'var/log/apt/*.log|同上,apt 的安裝紀錄。'
    'var/log/fontconfig.log|裝字型套件時 fc-cache 產生的紀錄(2026-08-06 實測 945 bytes,內容只有掃過的字型目錄清單)。'
    '*.env.example|設定**範本**:內容應該是鍵名與說明、不是值,而且它要跟著程式碼出貨才有用。⚠ 代價寫清楚:有人把真值填進範本再提交,這條就會放行它 —— 這是刻意接受的取捨,不是沒想到。'
    '*.env.sample|同上,另一種常見的範本命名。'
    '*.env.template|同上。'
)

# ── helpers ─────────────────────────────────────────────────────────────────
die() { echo "✗ $*" >&2; exit 2; }

command -v docker >/dev/null 2>&1 || die "docker 不在 PATH 上"

CREATED_CONTAINERS=()
TMP_PATHS=()
cleanup() {
    # 建過的容器與暫存檔一律清掉——就算中途被 Ctrl-C 或 set -e 打斷。
    if [ ${#CREATED_CONTAINERS[@]} -gt 0 ]; then
        docker rm -f "${CREATED_CONTAINERS[@]}" >/dev/null 2>&1 || true
    fi
    if [ ${#TMP_PATHS[@]} -gt 0 ]; then
        rm -rf "${TMP_PATHS[@]}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 一條路徑是否被白名單放行;放行則回傳 0 並把命中的白名單編號塞進 ALLOW_IDX。
# 回報時**按白名單條目彙總**而不是逐檔印:一張 debian 底的映像光 OS 信任庫就有
# 150 幾個 .pem,逐檔印會把真正的違規淹掉,然後大家開始跳過這段不看。
ALLOW_IDX=-1
is_allowlisted() {
    local path="$1" i pattern
    for i in "${!ALLOWLIST[@]}"; do
        pattern="${ALLOWLIST[$i]%%|*}"
        # shellcheck disable=SC2053  # 右側刻意當 glob 用
        if [[ "$path" == $pattern ]]; then
            ALLOW_IDX="$i"
            return 0
        fi
    done
    ALLOW_IDX=-1
    return 1
}

# 一條路徑命中哪一條規則(第一條命中的就算);沒命中回傳 1。
MATCHED_RULE=""
match_rule() {
    local path="${1,,}" rule name regex   # ${1,,} = 轉小寫,副檔名比對不分大小寫
    for rule in "${RULES[@]}"; do
        name="${rule%%|*}"
        regex="${rule#*|}"
        if [[ "$path" =~ $regex ]]; then
            MATCHED_RULE="$name"
            return 0
        fi
    done
    MATCHED_RULE=""
    return 1
}

# 路徑分類:VIOLATION(違規)/ ALLOWED(命中規則但白名單放行)/ IGNORED(沒命中)。
# 自我測試與正式掃描走的是**同一個函式**,測到的就是實際會跑的邏輯。
VERDICT=""
classify_path() {
    local path="$1"
    if ! match_rule "$path"; then
        VERDICT="IGNORED"; MATCHED_RULE=""; ALLOW_IDX=-1
        return 0
    fi
    if is_allowlisted "$path"; then
        VERDICT="ALLOWED"
    else
        VERDICT="VIOLATION"
    fi
    return 0
}

# 這個檔要不要驗內容:憑證/金鑰候選,或是「命中規則但被白名單放行」的檔。
# 後者是重點 —— 白名單放行的東西正是最需要被讀一眼的東西。
needs_content_check() {
    local path="${1,,}" verdict="$2"
    [[ "$path" =~ $CONTENT_CANDIDATE_RE ]] && return 0
    [ "$verdict" = "ALLOWED" ] && return 0
    return 1
}

# 檔案內容裡有沒有私鑰區塊。回傳 0 = 有(違規)。
content_has_private_key() {
    local f="$1"
    [ -L "$f" ] && return 1          # symlink 不判定(指向的東西不在映像裡也常見)
    [ -f "$f" ] || return 1          # 目錄 / 抽不出來 → 不判定
    LC_ALL=C grep -qaE "$PRIVATE_KEY_RE" "$f" 2>/dev/null
}

# ── 自我測試 ────────────────────────────────────────────────────────────────
# 為什麼要有這一段:掃描器最危險的壞法不是報錯,是**安靜地全部放行** ——
# 有人把 RULES 刪掉一半、或把 regex 改壞,輸出還是「✓ 乾淨」,而閘門看起來是綠的。
# 所以每次跑都先拿一組已知答案的假路徑餵給正式的分類函式,對不上就在掃任何
# 映像之前 exit 3。fixture 裡沒有真金鑰,私鑰標頭是執行時拼出來的字串。
SELF_TEST_CASES=(
    # 路徑|期望判定|期望規則(IGNORED / ALLOWED 時規則欄寫 -)
    'app/secrets/jwt-private.pem|VIOLATION|secret-material'
    'opt/x/SERVER.KEY|VIOLATION|secret-material'
    'opt/x/site.ppk|VIOLATION|secret-material'
    'opt/x/keystore.pfx|VIOLATION|keystore'
    'opt/x/backup.P12|VIOLATION|keystore'
    'home/u/.ssh/id_rsa|VIOLATION|ssh-private-key'
    'opt/x/deploy_rsa|VIOLATION|ssh-private-key'
    'opt/x/.env|VIOLATION|env-file'
    'opt/x/.env.production|VIOLATION|env-file'
    'opt/x/trace.log|VIOLATION|runtime-log'
    'opt/x/app.log.1|VIOLATION|runtime-log'
    'opt/x/old.log.gz|VIOLATION|runtime-log'
    'var/lib/app/secrets/token.txt|VIOLATION|secrets-dir'
    'opt/x/logs/thing.txt|VIOLATION|logs-dir-with-content'
    'srv/x/data/attachments/1/a.txt|VIOLATION|user-attachments'
    'opt/x/.pytest_cache/v/cache/nodeids|VIOLATION|pytest-cache'
    # 白名單洗白:目錄型 glob 曾經用「公開 CA 憑證」這個理由放行這把 .key
    'etc/ssl/certs/planted-evil.key|VIOLATION|secret-material'
    # 該放行的
    'etc/ssl/certs/DigiCert_Global_Root_CA.pem|ALLOWED|-'
    'app/app/services/cspki_ca_bundle.pem|ALLOWED|-'
    'var/log/dpkg.log|ALLOWED|-'
    'app/.env.example|ALLOWED|-'
    # 完全不該碰的
    'app/main.py|IGNORED|-'
    'app/__pycache__/main.cpython-311.pyc|IGNORED|-'
    'usr/share/doc/logs/|IGNORED|-'
)

self_test() {
    local failures=0 case_line path want_verdict want_rule got_rule
    for case_line in "${SELF_TEST_CASES[@]}"; do
        IFS='|' read -r path want_verdict want_rule <<<"$case_line"
        classify_path "$path"
        got_rule="$MATCHED_RULE"
        [ "$VERDICT" = "VIOLATION" ] || got_rule="-"
        if [ "$VERDICT" != "$want_verdict" ] || [ "$got_rule" != "$want_rule" ]; then
            echo "  ✗ self-test: $path → $VERDICT/$got_rule(期望 $want_verdict/$want_rule)" >&2
            failures=$((failures + 1))
        fi
    done

    # 內容規則:私鑰藏在一個**白名單放行**的路徑底下,必須仍然是違規。
    local probe_dir probe_file certifi_path
    certifi_path='usr/local/lib/python3.11/site-packages/certifi/cacert.pem'
    probe_dir="$(mktemp -d)"
    TMP_PATHS+=("$probe_dir")
    probe_file="$probe_dir/cacert.pem"
    printf -- '-----%s %s-----\nnot-a-real-key\n' "BEGIN" "PRIVATE KEY" > "$probe_file"
    classify_path "$certifi_path"
    if [ "$VERDICT" != "ALLOWED" ]; then
        echo "  ✗ self-test: certifi/cacert.pem 應該先被白名單放行(才輪得到內容規則接手)" >&2
        failures=$((failures + 1))
    fi
    if ! needs_content_check "$certifi_path" "ALLOWED"; then
        echo "  ✗ self-test: 白名單放行的憑證檔應該要進內容檢查" >&2
        failures=$((failures + 1))
    fi
    if ! content_has_private_key "$probe_file"; then
        echo "  ✗ self-test: 內容規則沒認出私鑰標頭" >&2
        failures=$((failures + 1))
    fi
    printf -- '-----%s %s-----\nMIIB…\n' "BEGIN" "CERTIFICATE" > "$probe_file"
    if content_has_private_key "$probe_file"; then
        echo "  ✗ self-test: 純憑證檔被誤判成私鑰" >&2
        failures=$((failures + 1))
    fi
    rm -rf "$probe_dir"

    # ── 白名單自己的形狀也要驗 ────────────────────────────────────────────
    # 自我測試原本只驗規則,抓得到「有人把 RULES 刪光」,卻抓不到
    # 「規則都在、但有人往白名單加了一條 `*/logs/*` 」——後者才是這份清單
    # 實際會腐化的方向。所以這裡把上面第 3 條規矩變成會失敗的檢查:
    #   (a) 不准用目錄型萬用字元收尾(最後一段不能以 * 結尾、不能以 / 結尾)
    #   (b) 任何一條都不准放行 fixture 裡的髒路徑
    local entry pattern last_seg dirty_path dirty_verdict
    for entry in "${ALLOWLIST[@]}"; do
        pattern="${entry%%|*}"
        if [ -z "${entry#*|}" ] || [ "${entry#*|}" = "$entry" ]; then
            echo "  ✗ self-test: 白名單條目沒有理由欄:$pattern" >&2
            failures=$((failures + 1))
        fi
        last_seg="${pattern##*/}"
        if [ -z "$last_seg" ] || [ "${last_seg: -1}" = "*" ]; then
            echo "  ✗ self-test: 白名單條目以目錄型萬用字元收尾(要寫到檔名或副檔名):$pattern" >&2
            failures=$((failures + 1))
        fi
        for case_line in "${SELF_TEST_CASES[@]}"; do
            IFS='|' read -r dirty_path dirty_verdict _ <<<"$case_line"
            [ "$dirty_verdict" = "VIOLATION" ] || continue
            # shellcheck disable=SC2053  # 右側刻意當 glob 用
            if [[ "$dirty_path" == $pattern ]]; then
                echo "  ✗ self-test: 白名單條目會放行已知的髒路徑:$pattern → $dirty_path" >&2
                failures=$((failures + 1))
            fi
        done
    done

    if [ "$failures" -gt 0 ]; then
        echo "✗ 自我測試失敗 $failures 項 —— 掃描器已經被改壞了,拒絕繼續。" >&2
        echo "  (這種壞法的症狀是『每張映像都乾淨』,所以寧可停在這裡。)" >&2
        exit 3
    fi
}

if [ $# -eq 0 ]; then
    # 用檔頭那兩條 `# ====` 當邊界,不寫死行號 —— 寫死的行號會在下次改檔頭時
    # 悄悄把說明切在半句話上。
    sed -n '/^# ====/,/^# ====/p' "$0" | sed 's/^# \?//' >&2
    exit 2
fi

# 給 grep 用的粗篩(把 RULES 的 regex 併成一條),避免對十萬行做 bash 迴圈。
# 粗篩只負責挑出「可能違規或要驗內容」的行,真正判定仍然走 classify_path;
# 內容候選(*.crt 這種不違規但要讀一眼的)也併進來,否則第二趟會漏看。
COARSE_REGEX="$(
    { for rule in "${RULES[@]}"; do printf '%s|' "${rule#*|}"; done
      printf '%s' "$CONTENT_CANDIDATE_RE"; }
)"

# ── 主迴圈 ──────────────────────────────────────────────────────────────────
TOTAL_VIOLATIONS=0
DIRTY_IMAGES=()

echo "============================================================"
echo "ANILA — post-build image artifact scan"
echo "  Images: $*"
echo "  Method: docker create (never started) + docker export | tar -t"
echo "============================================================"

self_test
echo "  ✓ 自我測試通過(${#SELF_TEST_CASES[@]} 條路徑 fixture + 內容規則 4 項 + ${#ALLOWLIST[@]} 條白名單的形狀)"

idx=0
for img in "$@"; do
    idx=$((idx + 1))
    echo
    echo "▶ $img"

    docker image inspect "$img" >/dev/null 2>&1 || die "映像不存在:$img(先 build/pull)"

    cname="${CONTAINER_PREFIX}-$$-${idx}"
    # 已經有同名殘留就先清掉(冪等:同一支腳本重跑不該因為上次被 kill 而失敗)
    docker rm -f "$cname" >/dev/null 2>&1 || true

    # docker create 不 start;映像的 entrypoint/CMD 不會執行。
    # 有些映像沒有 CMD 也沒有 entrypoint,create 會抱怨「no command specified」——
    # 補一個永遠不會被跑到的 entrypoint 就能建起來。
    if ! docker create --name "$cname" "$img" >/dev/null 2>&1; then
        docker create --name "$cname" --entrypoint /nonexistent-scan-placeholder "$img" >/dev/null 2>&1 \
            || die "docker create 失敗:$img"
    fi
    CREATED_CONTAINERS+=("$cname")

    listing="$(mktemp)"
    TMP_PATHS+=("$listing")
    if ! docker export "$cname" | tar -tf - > "$listing" 2>/dev/null; then
        die "docker export 失敗:$img"
    fi
    total_paths="$(wc -l < "$listing")"
    # 「什麼都沒掃到」**不是乾淨**,是掃描根本沒看到映像內容(export 壞掉 /
    # 拿到空映像)。這種情況回 0 會讓閘門亮綠燈,正是這支腳本要消滅的形狀。
    # ⚠ 不能只判「清單是空的」:docker export 一張完全空的映像仍然會給 12 條
    #   **容器骨架**(.dockerenv、dev/、etc/hosts、proc/、sys/ …,2026-08-06 實測),
    #   所以門檻要下在「扣掉骨架之後還剩幾條」。
    content_paths="$(grep -cvxE '\.dockerenv|dev/|dev/console|dev/pts/|dev/shm/|etc/|etc/hostname|etc/hosts|etc/mtab|etc/resolv\.conf|proc/|sys/' "$listing" || true)"
    [ "$content_paths" -gt 0 ] || die "$img 掃出來只有 docker 自己加的容器骨架($total_paths 條)—— 映像內容沒被讀到,當成掃描失敗,不是乾淨"

    # 第一趟:分類所有粗篩命中的路徑,同時收集要驗內容的候選檔。
    violations=()
    allowed_total=0
    declare -a ALLOW_HITS=() ALLOW_SAMPLE=()
    for i in "${!ALLOWLIST[@]}"; do ALLOW_HITS[$i]=0; ALLOW_SAMPLE[$i]=""; done

    candidates="$(mktemp)"
    TMP_PATHS+=("$candidates")
    : > "$candidates"
    declare -A CAND_VERDICT=()

    while IFS= read -r path; do
        [ -n "$path" ] || continue
        if [ "${path: -1}" = "/" ]; then is_dir=1; else is_dir=0; fi
        classify_path "$path"
        case "$VERDICT" in
            VIOLATION)
                violations+=("$path|$MATCHED_RULE")
                ;;
            ALLOWED)
                ALLOW_HITS[$ALLOW_IDX]=$(( ALLOW_HITS[ALLOW_IDX] + 1 ))
                [ -z "${ALLOW_SAMPLE[$ALLOW_IDX]}" ] && ALLOW_SAMPLE[$ALLOW_IDX]="$path"
                allowed_total=$(( allowed_total + 1 ))
                ;;
        esac
        # 目錄不必抽內容
        if [ "$is_dir" -eq 0 ] && needs_content_check "$path" "$VERDICT"; then
            printf '%s\n' "$path" >> "$candidates"
            CAND_VERDICT["$path"]="$VERDICT"
        fi
    done < <(grep -Ei "$COARSE_REGEX" "$listing" || true)

    # 第二趟:只把候選檔抽出來讀。私鑰內容規則**壓過白名單**。
    #
    # ⚠ 這一趟**抽失敗就是掃描失敗**,不是乾淨。
    # 舊版把 tar 的錯誤吞掉(`|| true`)又拿「候選數」當「讀過數」印出來,
    # 結果是:第二趟整個沒抽到東西,報告照樣寫「讀了 4 個候選檔的內容 ✓乾淨」。
    # 這台主機的觸發條件是真的 —— 本機 IDS 會在 export 途中把 overlay 讀壞
    # (build-and-export 的 REBUILD_ON_SAVE_FAIL 就是為了這件事存在的),
    # 而那正好會讓「藏在 cacert.pem 裡的私鑰」變成綠燈。
    # 所以這裡數的是**真的落地的檔案數**,少一個就 die。
    content_violations=()
    landed=0
    if [ -s "$candidates" ]; then
        exdir="$(mktemp -d)"
        TMP_PATHS+=("$exdir")
        # tar 本身的 exit code 不夠用(部分成功也可能回 0/2),所以不靠它判斷,
        # 靠「點名數落地的檔案」。
        docker export "$cname" \
            | tar -xf - -C "$exdir" --no-same-owner --no-same-permissions \
                  --files-from "$candidates" >/dev/null 2>&1 || true
        missing_samples=()
        while IFS= read -r path; do
            [ -n "$path" ] || continue
            # symlink 也算落地(它確實被抽出來了,只是指向的東西可能不在候選集裡)
            if [ -e "$exdir/$path" ] || [ -L "$exdir/$path" ]; then
                landed=$(( landed + 1 ))
            elif [ ${#missing_samples[@]} -lt 3 ]; then
                missing_samples+=("$path")
            fi
            if content_has_private_key "$exdir/$path"; then
                content_violations+=("$path|${CAND_VERDICT[$path]:-?}")
            fi
        done < "$candidates"
        rm -rf "$exdir"
        cand_count="$(wc -l < "$candidates")"
        if [ "$landed" -lt "$cand_count" ]; then
            die "$img 的內容檢查沒做完:點名 $cand_count 個候選檔,只落地 $landed 個(例:${missing_samples[*]:-?})—— 抽檔失敗當掃描失敗,不會給乾淨"
        fi
    fi

    docker rm -f "$cname" >/dev/null 2>&1 || true
    for i in "${!CREATED_CONTAINERS[@]}"; do
        [ "${CREATED_CONTAINERS[$i]}" = "$cname" ] && unset 'CREATED_CONTAINERS[i]'
    done
    cand_total="$(wc -l < "$candidates")"
    rm -f "$listing" "$candidates"

    # 印的是**真的讀到的**數量($landed),不是點名數($cand_total)——
    # 兩者不一致的情況上面已經 die 了,這裡印 landed 是為了讓報告上的數字
    # 永遠是「我真的看過幾個檔」,而不是「我本來打算看幾個」。
    echo "  掃了 $total_paths 條路徑(扣掉 docker 容器骨架後 $content_paths 條),讀了 $landed/$cand_total 個憑證/金鑰候選檔的內容"

    if [ "$allowed_total" -gt 0 ]; then
        echo "  白名單放行(有看到,但有理由留著)— $allowed_total 筆:"
        for i in "${!ALLOWLIST[@]}"; do
            [ "${ALLOW_HITS[$i]}" -gt 0 ] || continue
            printf '    ~ %s  (%d 筆,例:%s)\n' \
                "${ALLOWLIST[$i]%%|*}" "${ALLOW_HITS[$i]}" "${ALLOW_SAMPLE[$i]}"
            printf '        理由:%s\n' "${ALLOWLIST[$i]#*|}"
        done
    fi

    img_violations=$(( ${#violations[@]} + ${#content_violations[@]} ))
    if [ "$img_violations" -eq 0 ]; then
        echo "  ✓ 乾淨:沒有非預期的執行期產物"
    else
        echo "  ✗ 違規 $img_violations 筆:"
        for v in "${violations[@]}"; do
            printf '    [%s] %s\n' "${v#*|}" "${v%%|*}"
        done
        for v in "${content_violations[@]}"; do
            if [ "${v#*|}" = "ALLOWED" ]; then
                printf '    [%s] %s  ← 檔名被白名單放行,內容出賣了它\n' \
                    "$CONTENT_RULE_NAME" "${v%%|*}"
            else
                printf '    [%s] %s\n' "$CONTENT_RULE_NAME" "${v%%|*}"
            fi
        done
        TOTAL_VIOLATIONS=$(( TOTAL_VIOLATIONS + img_violations ))
        DIRTY_IMAGES+=("$img")
    fi
    # 機器可讀的一行,給閘門判「掃到 0 個檔」用
    echo "  SCAN-SUMMARY image=$img paths=$total_paths content_paths=$content_paths violations=$img_violations"
done

echo
echo "============================================================"
if [ "$TOTAL_VIOLATIONS" -eq 0 ]; then
    echo "✓ 全部乾淨($# 張映像)"
    echo "============================================================"
    exit 0
fi
echo "✗ $TOTAL_VIOLATIONS 筆違規,分布在 ${#DIRTY_IMAGES[@]} 張映像:"
for img in "${DIRTY_IMAGES[@]}"; do
    echo "    - $img"
done
echo
echo "  怎麼修(照這個順序想,不要直接跳到最後一條):"
echo "    1. 這個檔本來就不該進 build context → 修 .dockerignore(或別 COPY 整個目錄)"
echo "    2. 是建置過程產生的 → 在同一個 RUN 裡刪掉,或搬到不會被 COPY 的 build stage"
echo "    3. 成品**真的需要**它,而且它是公開資訊 → 才輪到加白名單,"
echo "       在 $0 的 ALLOWLIST 補一條並寫清楚理由"
echo "    ⚠ [$CONTENT_RULE_NAME] 那一類沒有第 3 條 —— 私鑰不該在映像裡,白名單也放行不了。"
echo "============================================================"
exit 1
