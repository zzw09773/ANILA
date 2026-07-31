# CSP 後端測試 —— 怎麼跑、基準線是什麼

> 最後實測:**2026-07-31**。改動測試或 production 後請重跑並更新這份數字。

## 怎麼跑

```bash
# 從 repo 根目錄
python -m pytest services/csp/tests -q

# 或從 services/csp
cd services/csp && python -m pytest -q
```

**兩種跑法的結果必須一模一樣。不一樣就是壞了,先修跑法再看測試。**

不需要先 export 任何環境變數,也不需要設 `PYTHONPATH`。`services/csp/pytest.ini`
把 rootdir 釘在 `services/csp` 並注入 `pythonpath`;`tests/conftest.py` 在 import
`app.*` 之前把 `SECRET_KEY` / `ANILA_ALLOW_DEV_SECRET` 等必要變數設好。

直譯器:本樹尚無自己的 venv,暫借
`~/桌面/ANILA/anila-migration-20260706/ANILA/services/csp/.venv/bin/python`
(套件版本已逐一比對過,與執行中的 csp 映像一致)。

## 目前基準線(2026-07-31 實測)

```
1 failed · 1296 passed · 13 skipped · 0 errors     (約 8 分鐘)
```

四種跑法都是這個數字,已驗證:

| 跑法 | 結果 |
|---|---|
| cwd = repo 根目錄 | 1 failed · 1296 passed · 13 skipped |
| cwd = `services/csp` | 1 failed · 1296 passed · 13 skipped |
| `services/csp` + 檔案順序反轉 | 1 failed · 1296 passed · 13 skipped |

### 唯一的紅燈

| 測試 | 分類 | 說明 |
|---|---|---|
| `test_template_download.py::test_developer_can_download_template` | **真缺陷** | `GET /api/agents/template/download` 回 404 —— `app/api/agents/registration.py` 找不到 template 目錄(該檔的 fallback 路徑缺陷由另一個包負責處理,不在本包範圍)。**這是 production 缺陷,不是測試問題。** |

`test_plain_user_cannot_download_template` 是綠的(非 developer 本來就該被擋),
所以權限那一半沒問題,壞的是 template 目錄解析。

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
python -m pytest $(ls tests/test_*.py | sort -r) -q
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
