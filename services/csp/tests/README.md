# CSP 後端測試 —— 怎麼跑、基準線是什麼

> 最後實測:**2026-08-01**。改動測試或 production 後請重跑並更新這份數字。

## 怎麼跑

直譯器:本樹尚無自己的 venv,暫借
`~/桌面/ANILA/anila-migration-20260706/ANILA/services/csp/.venv/bin/python`
(套件版本已逐一比對過,與執行中的 csp 映像一致)。下文以 `$PY` 代稱該路徑。

```bash
# 跑法 1 — cwd = worktree / repo 根目錄
cd /path/to/ANILA   # 本 worktree 根
$PY -m pytest services/csp/tests -q

# 跑法 2 — cwd = services/csp
cd /path/to/ANILA/services/csp
$PY -m pytest tests -q
```

維護者核對兩種跑法是否一致:把兩邊的 summary 行(passed / failed / skipped /
collected)並排比對,**數字必須相同**。不一致就先查 cwd / `.env` / 殘留
DB 狀態,再看測試本身。

不需要先 export 任何環境變數,也不需要設 `PYTHONPATH`。`services/csp/pytest.ini`
把 rootdir 釘在 `services/csp` 並注入 `pythonpath`;`tests/conftest.py` 在 import
`app.*` 之前把 `SECRET_KEY` / `ANILA_ALLOW_DEV_SECRET` / `ENABLE_CARD_LOGIN` /
`REQUIRE_CARD_LOGIN_ONLY` / 臨時 `DATABASE_URL` 等變數釘死,這幾個就不依 cwd。
⚠ 其餘 `Settings` 欄位(如 `ADMIN_PASSWORD`、`CARD_INITIAL_OWNERS`、
`CSP_SERVICE_TOKEN`、`MODEL_GATEWAY_API_KEY`)從 repo 根跑時仍會讀到機上
`.env`——目前沒有測試依賴它們的 ambient 值,新增依賴前先來這裡補釘。

> ⚠ **2026-08-05 補充 —— 這份基準線這次是怎麼量的**
> 本檔第 54 行那條警告(「每包各自更新,最後合進來的那一包會寫成偏低值」)這次確實被遵守了:
> 八包在 08-05 分三批合併期間,**沒有任何一包在 worktree 裡改這個數字**,
> 全部由合併者在主樹跑完整套之後一次更新。三批各自量到的中間值是
> **1966 → 2025 → 2069**,可交叉對照。
>
> ⚠ 另外要知道:**這個數字綠不代表覆蓋夠。** 同一天六輪獨立驗收,每一輪都找到
> 「改一行讓功能整個死掉、而套件全綠」的位置。前端已經有突變檢查
> (`apps/anila-shell/scripts/mutation-check.mjs`),**csp 這邊還沒有**。

## 目前基準線(2026-08-05 實測,八包合併完成後在主樹量)

```
2069 passed · 13 skipped · 0 failed     (2082 collected)
```

兩種跑法都是這個數字,已驗證:

| 跑法 | cwd | 指令 | 結果 |
|---|---|---|---|
| 1 | worktree / repo 根 | `$PY -m pytest services/csp/tests -q` | 2069 passed · 13 skipped |
| 2 | `services/csp` | `$PY -m pytest tests -q` | 2069 passed · 13 skipped |

舊基準線(2026-07-31)的唯一紅燈
`test_template_download.py::test_developer_can_download_template` 已由
commit `6f12e600` 修掉,不再列入紅燈表。

### ⚠ 平行開發時這個數字一定會漂,原因是結構性的

每一包在**自己的 worktree** 上量到的是「分歧點的數量 ＋ 自己加的那幾條」。
合併之後的總數是「分歧點 ＋ 各包的總和」——**這個數字沒有任何一包量過**,
所以每包各自更新 README 時,最後合進來的那一包會把基準線寫成它自己看到的偏低值。

實例:`04da79f1` 把基準線從 1431 改成 1471(+40),但當時實際已經是 1482(+51),
少記了 11 條;後續兩包合併又各加了一些,到 08-01 收工時真值是 1494,
README 卻還停在 1471——**差 23,而且沒有任何測試是壞的**。

**規則**:一批平行包全部合併完之後,**在主樹重跑一次兩種 cwd**,用實測值改這裡。
不要用任何單一分支回報的數字。看到對不上,先假設是這個原因,再去懷疑測試。

## 這份基準線之前為什麼是假的

2026-07-31 以前流傳的基準線是「26 failing,別加第 27 個」。實測:

- 從 repo 根目錄跑 = **27 failed / 1262 passed / 2 errors**
- 從 `services/csp` 跑並先 `export SECRET_KEY` = **14 failed / 1275 passed / 2 errors**

**同一份程式碼,兩個答案。** 差的 13 支全是環境,不是程式碼:

1. `anila-core` 的 `credential_crypto` 直接讀 `os.environ["SECRET_KEY"]`,沒設就
   `RuntimeError` → `test_agent_credentials` 12 支紅。
2. `test_service_registry` 用相對路徑 `Config("alembic.ini")`,而 `alembic.ini`
   在 `services/csp/` → 從根目錄跑就找不到。

而且引用的 1199 也早就過期 —— 中間加了約 100 支測試沒人重量過。

另外還有一種更隱蔽的形態:**只挑幾個檔跑會整批 error**。因為
`app.main.lifespan` 的 dev-default 閘門需要 `ANILA_ALLOW_DEV_SECRET=1`,而這個
變數以前是靠 `test_token_revoke_publish.py` 在 import 期
`os.environ.setdefault` 的副作用「順便」被設起來的。跑全套會綠,跑子集全 error。
現在搬進 `tests/conftest.py`,不再取決於你選了哪些檔。

2026-08-01 又釘掉兩類 cwd / 磁碟狀態依賴:

1. `Settings(env_file=".env")` 相對 cwd 解析 → 根目錄跑會吃到
   `ENABLE_CARD_LOGIN=true`;`conftest` 現在硬設 `ENABLE_CARD_LOGIN=false`,
   卡登負向測試也自己釘 precondition。
2. 舊的 `DATABASE_URL=sqlite:///./.pytest-csp.db` 相對 cwd 且跨行程殘留 →
   改成 per-session 臨時檔,並在 import 後對 `SessionLocal` 引擎 `create_all`。

## 執行順序污染(已修)

`importlib.reload(app.config)` 會重跑 `settings = Settings()`,產生一顆**新的**
`Settings`;但所有在 import 期做過 `from app.config import settings` 的模組
(`app.utils.security` 等)手上還是**舊**那顆。`monkeypatch` 還原得了環境變數,
還原不了已經被建出來的物件 —— 於是同時存在兩顆 settings,誰 patch 到哪一顆
變成執行順序的函數。

三個 fixture 會 reload config,現在都在 teardown 把 `app.config.settings` 指回
原物件:

- `tests/test_startup_security.py`(`reload_startup_security`)
- `tests/test_token_revoke_publish.py`(`_ensure_dev_secret_gate`)
- `tests/test_revocations_endpoint.py`(`_ensure_dev_secret_gate`)

症狀範例:`test_rs256_jwt::test_expired_token_rejected` 曾經是紅的,因為它
monkeypatch 新 settings 的 `ACCESS_TOKEN_EXPIRE_MINUTES`,而 `create_access_token`
讀的是舊那顆 —— token 被簽成沒有過期時間,`decode_token` 正確地接受了它。
**那不是認證漏洞,是測試自己沒測到東西。**

本樹沒裝 `pytest-randomly`。要換執行順序驗證,把檔案清單倒過來當參數傳:

```bash
cd services/csp
$PY -m pytest $(ls tests/test_*.py | sort -r) -q
```

## 卡登測試怎麼看

卡登是這個平台唯一的入口,測試分兩層,**兩層都要綠**:

- `tests/test_card_auth.py` —— 單元層。用 `cht/` mock 的真實卡片材料 + 釘死的
  `cspki_ca_bundle.pem`,守簽章驗證、憑證鏈、自簽偽造工號的 CVE 回歸。
- `tests/test_card_endpoints.py` —— 端點層。用**測試現場合成**的兩層 PKI
  (`CARD_CA_BUNDLE_PATH` 指到臨時 bundle),每次刷卡拿當次 challenge 的 nonce
  現簽 CMS。nonce 綁定、簽章驗證、鏈驗證全部照跑,沒有打開任何 dev 旁路旗標。

`cht/` 的 mock 簽章 eContent 永遠是 `b"TBS"`,**在架構上不可能通過 nonce 綁定**。
Dev 手動刷卡要靠 `CARD_DEV_SKIP_NONCE_BINDING=1`;**測試不准用那條路**,
否則守衛等於關掉。
