# ANILA LM 發行閘門

> 2026-09-26 擁有者決定：**ANILA LM（知識庫 + Studio，`apps/anilalm`）對使用者開放。**
> 下面四道開關在這棵樹裡都是開的。`anilalm` 容器本來就在跑（compose 服務沒有 profile 限制）；
> 已經起來的棧要等重建映像並 force-recreate nginx 才會換上這版（見「套到正在跑的棧」）。

## 四個開關（現況：全開）

| # | 表面 | 檔案 | 現況（開放） | 再關閉時 |
|---|---|---|---|---|
| 1 | nginx 對外路徑 | `infra/nginx/anila.conf` | `ANILA_LM_NGINX_GATE = open`：443 與 4443 都是 `location = /anilalm`（補斜線）+ `location /anilalm` `proxy_pass` | 兩個 server block 改回 `location ~* ^/anilalm`（503 加維護頁，完整寫法見下節） |
| 2 | ANILA shell 導覽 | `apps/anila-shell/src/anilalmReleaseGate.js` | `ANILA_LM_ENTRY_ENABLED = true`（「我的知識庫」可點） | `false`（列還在，標「即將推出」、不可點） |
| 3 | 治理中心儀表板卡片 | `apps/csp-governance-ui/src/utils/anilalmReleaseGate.js` | `ANILA_LM_LINK_VISIBLE = true` | `false`（儀表板不渲染；**平台連結／服務存取照列，只標「未開放」**） |
| 4 | **API 回應本身** | `services/csp/app/services/anilalm_release_gate.py` | `ANILA_LM_RELEASED = True`：使用者面清單列出 ANILA LM，`POST /api/services/{id}/launch` 可啟動 | `False`：使用者面清單不列；launch 回 503 |

四道要一起動。只關一道會變成「看得到但 503」，或「API 開得了、頁面打不開」。

### 為什麼第 4 道還在（2026-08-02）

1、2、3 擋的是瀏覽得到的門。shell 的「專案入口」抽屜照 `GET /api/services` 畫。閘門若只關在畫面上，抽屜仍會出現一張按下去走到維護頁的卡片，而且知道 service id 的人打得到 `POST …/launch`。

⚠ **第 4 道不關管理面。** `include_inactive=true` 的管理清單（治理中心「平台連結」與「服務存取」）照常回傳 ANILA LM，單筆 CRUD 也照常。閘門關的是「可用」，不是「可管理」。

這一列怎麼來的：舊的 `platform_links` 列由 `registry_backfill` 原 id 搬進 `registered_services`，
`is_active` 沿用原值（欄位預設 True）。`auto_seed.py` 只種 admin 與 `anila-router` 模型列，
不建服務列；`services/csp/app/api/services.py` 的註解寫「種子」，可是程式上沒有那段種子，
要判行為請以 `registry_backfill` 為準。

## 為什麼關的時候用 503

對外 `/anilalm/` 在關閉時回 **503 + 短 zh-TW 維護頁**，不用 404。404 會讓人以為功能不存在；503 表示尚未開放。

關閉要用 nginx 的 `location ~* ^/anilalm` 直接砌一頁，不能只寫 `return 503;`。重開前的最後版本在 git 歷史的 `5c473d1e`（`git show 5c473d1e:infra/nginx/anila.conf`），兩個 TLS server block（443、4443）都要蓋上去；`types { }` 那三行是必要的，否則 nginx 會把 503 內文標成 `application/javascript`。下面是精簡示意：

```nginx
location ~* ^/anilalm {
    types { }
    default_type text/html;
    charset utf-8;
    return 503 '<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8"><title>ANILA LM · 尚未開放</title></head><body><main><h1>ANILA LM 尚未開放</h1><p>此功能尚在整備，暫時無法使用。</p></main></body></html>';
}
```

## 套到正在跑的棧（本機 `-p anila`）

改檔不會自動進已經起來的容器。

```bash
cd ~/桌面/ANILA/anila-restart-20260729/ANILA（桌面目錄若叫 Desktop，用 ~/Desktop/ANILA/anila-restart-20260729/ANILA）

# 治理中心與後端閘門都在 csp 映像裡；shell 在 anila-ui。
# nginx 是 bind-mount 綁 inode；git 改檔＝新 inode，reload 不夠。
docker compose -p anila build anila-ui csp
docker compose -p anila up -d anila-ui csp
docker compose -p anila up -d --force-recreate nginx
# ⚠ 不要 docker cp 進 conf.d/：本檔是掛成 templates/default.conf.template 生成到
#   conf.d 的，再 cp 一份進去就變第二份設定 → resolver 重複宣告。
```

## 驗收

```bash
# ANILA LM 應是 200 text/html（裸路徑可能先 301 到 /anilalm/）
curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1/anilalm/
curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' https://127.0.0.1/anilalm

# 其他入口不得被波及
for p in / /anila/ /asr/health /router/health; do
  printf '%-16s ' "$p"
  curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' "https://127.0.0.1$p"
done
```

```bash
# API 面：使用者面清單應列出 ANILA LM，launch 不是 503
#（$TOK = 任一已登入使用者的 access token）
curl -sk -H "Authorization: Bearer $TOK" https://127.0.0.1/api/services \
  | python3 -c 'import json,sys;print([r["name"] for r in json.load(sys.stdin)])'
```

- Shell：側欄「我的知識庫」是可點連結，沒有「即將推出」。
- Shell：「專案入口」抽屜有 ANILA LM 卡片，按下去開得起來。
- 治理中心：儀表板「平台 · 外部工具」有 ANILA LM 卡片。
- 治理中心：平台連結／服務存取那一列沒有「未開放」標籤（列本來就一直在）。

## 再關閉時不要做的事

- 不要 `docker compose stop anilalm` 或從 compose 刪服務。
- 不要只 `nginx -s reload` 就以為 anila.conf 已生效。
- 不要刪掉 shell 的「我的知識庫」列——只切旗標。
- **不要靠手動把 `is_active` 設成 false 當閘門。** 那是資料不是程式碼，重跑種子／遷移就回到 `is_active` 的預設值。
- **不要在治理中心的管理頁把整列濾掉。** 管理員看不到那一列，就管不動它。
