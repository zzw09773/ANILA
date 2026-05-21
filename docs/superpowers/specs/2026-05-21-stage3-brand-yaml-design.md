# Stage 3 — brand.yaml 多品牌風格（依部門）

**Date:** 2026-05-21
**Branch:** feature/studio-flux
**Spec context:** `docs/superpowers/plans/ANILA_Studio_FLUX_Spec.md` §6（Stage 3 — Style Consistency）、§3.6（StyleDescriptor 鎖定合約）
**Status:** 設計已核准，待寫實作計畫

---

## 1. 背景與決策

Stage 1 把 house style 寫死在 `flux_style.py` 的 `_DEFAULT_STYLE`（`get_style_descriptor` 忽略 `brand_id`，studio.py 也以 `brand_id=None` 呼叫）。Stage 3 要讓**不同部門拿到不同的視覺風格設定**，換部門就換整份簡報的 style suffix / style_id，貼合「不同單位交付」情境（國軍各單位、中科院）。

**決策**：
- 品牌對應鍵 = **觸發 job 的 user 所屬 department**（系統既有 `User.department_name`）。使用者不必選，自動套單位風格。
- 設定檔 = **每部門一個 YAML**，放在可掛載、可編輯（不需 rebuild）的範本目錄下。
- 查找失敗一律安全退回，**壞設定檔不得弄垮 job**。

**已知限制（重要，誠實聲明）**：本階段交付「換部門 → 換風格**設定**」的機制。它**不保證 FLUX 真的服從該風格** —— Stage 2 的 5.6 log 顯示 FLUX 會無視 suffix（要 flat editorial 卻畫成寫實風）。風格「服從度」屬 §6.1③（Redux / Style LoRA），不在本階段範圍。本階段換的是「要求什麼」，不是「強制照做」。

---

## 2. 範圍

**In scope**
- 新增設定檔目錄 `${ANILA_TEMPLATE_DIR}/brands/`（host: `./anila-agent/brands/`，已掛載 ro）。
- 改 `app/services/flux_style.py`：`get_style_descriptor(brand_id)` 從 YAML 讀取，含多層 fallback 與安全處理。
- 改 `app/api/studio.py:1468`：以 `llm._user.department_name` 當 `brand_id`。
- 單元測試（`tests/test_flux_style.py`，新增）。
- 一份範例 `brands/_default.yaml` 與 `brands/README` 說明格式（供 ops 參考）。

**Out of scope**
- LoRA / Redux（風格服從度）—— `StyleDescriptor.lora_path` / `redux_ref_path` 欄位保留但不實作、不讀取。
- frontend 選風格下拉、DB 管理介面。
- Stage 4（section band / content illustration）。
- 改動 FLUX `/generate` 服務合約。

---

## 3. 設定檔格式與位置

- 目錄：`${ANILA_TEMPLATE_DIR}/brands/`（容器內 `/app/anila-template/brands/`，host `./anila-agent/brands/`，唯讀掛載，ops 在 host 編輯即時生效、無需 rebuild）。
- 每部門一檔：`brands/<department_name>.yaml`，檔名 stem = 部門名（與 `User.department_name` 完全相符）。
- 內容：

```yaml
brand_id: 資通所
style_id: rd-institute-v1
suffix: "flat editorial illustration, deep navy and amber palette, soft lighting, generous negative space, clean unmarked surfaces, no text, no letters, no symbols, no signage"
# lora_path:  選填，本階段不讀取
# redux_ref_path:  選填，本階段不讀取
```

- 必要欄位：`style_id`、`suffix`。`brand_id` 選填（純供人閱讀）。
- 選填的 `brands/_default.yaml`：同格式，作為「平台預設」的可編輯覆蓋層（ops 不改 code 就能調整全平台預設風格）。

---

## 4. 查找邏輯（`get_style_descriptor`）

簽名不變：`get_style_descriptor(brand_id: str | None = None) -> StyleDescriptor`。

查找順序（任一步失敗就安靜進入下一步，並 `logger.warning`）：
1. `brand_id` 非空且通過安全檢查 → 嘗試讀 `brands/{brand_id}.yaml`。
2. 否則 / 上一步失敗 → 嘗試讀 `brands/_default.yaml`（若存在）。
3. 仍無 → 回傳寫死的 `_DEFAULT_STYLE`（保證永遠有可用風格，即使整個 brands/ 目錄不存在）。

**載入單檔的步驟**（`_load_brand_file(path) -> StyleDescriptor | None`）：
- 檔不存在 → 回 None。
- `yaml.safe_load`（**不可用 `yaml.load`**）。解析例外 → log warning、回 None。
- 缺 `style_id` 或 `suffix`（非空字串）→ log warning、回 None。
- 成功 → `StyleDescriptor(style_id=..., suffix=..., lora_path=data.get("lora_path"), redux_ref_path=data.get("redux_ref_path"))`。

**安全**：
- `brand_id` 經 `_is_safe_brand_id`：拒絕含 `/`、`\`、`..`、或空白/空字串者（防目錄穿越）。不安全 → 視為無 brand，進入第 2 步。
- 設定目錄根透過 `ANILA_TEMPLATE_DIR` 環境變數推導：`Path(os.environ.get("ANILA_TEMPLATE_DIR", "/app/anila-template")) / "brands"`。組出路徑後額外確認其 `resolve()` 仍在 brands 目錄底下，否則拒。

---

## 5. 接上部門（`studio.py`）

`studio.py:1468`，將：
```python
            style = get_style_descriptor(brand_id=None)
```
改為：
```python
            style = get_style_descriptor(brand_id=llm._user.department_name)
```
`llm._user` 在此 scope 已可用（同段稍後 1492 行即以 `llm._user` 建 VLM gate）。使用者無部門 → `department_name` 為 None → 退 default。

`style_id` 早已在 FLUX provider cache key 內（§3.3），換部門 → 不同 style_id → 不會命中別部門/舊 default 的快取 PNG。

---

## 6. 元件邊界

- `flux_style.py`：唯一改動點為 `get_style_descriptor` 的實作 + 兩個私有 helper（`_load_brand_file`、`_is_safe_brand_id`）。`StyleDescriptor` 資料形狀不變。對呼叫端（rewriter、provider、studio）介面零變動。
- `studio.py`：僅一行 `brand_id` 來源變更。
- 設定資料與程式分離：風格內容在 YAML，邏輯在 `flux_style.py`。

---

## 7. 錯誤處理

- 任何設定層級的問題（缺檔、壞 YAML、缺欄位、不安全 brand_id、目錄不存在）→ 降級到下一 fallback + warning，最終保證 `_DEFAULT_STYLE`。job 永不因風格設定失敗。
- 不對 ops 編輯內容做語意校驗（suffix 寫什麼是 ops 的事）；只確保結構合法。

---

## 8. 測試計畫（TDD：先紅後綠）

新檔 `tests/test_flux_style.py`，用 `tmp_path` + monkeypatch `ANILA_TEMPLATE_DIR` 指向暫存 brands 目錄：

1. 有 `brands/資通所.yaml`（合法）→ `get_style_descriptor("資通所")` 回該 style_id/suffix。
2. 查無部門檔、無 `_default.yaml` → 回 `_DEFAULT_STYLE`。
3. 有 `brands/_default.yaml` 而無部門檔 → 回 `_default.yaml` 的風格。
4. 部門檔 YAML 損壞 → 回 fallback + （可斷言 warning 不拋例外）。
5. 部門檔缺 `suffix` → 回 fallback。
6. `brand_id=None` → fallback 鏈（無 _default 時 `_DEFAULT_STYLE`）。
7. 不安全 `brand_id`（`"../x"`、`"a/b"`、`""`、`"  "`）→ 不讀該路徑，回 fallback。
8. 合法檔含 `lora_path` → StyleDescriptor 帶入該值（欄位保留驗證）。

`studio.py` 的一行接線不另寫單元測試（屬既有 FLUX pipeline，由 Stage 2 5.6 同類實機路徑涵蓋；如需，後續實機觸發時 log 驗 `style_id`）。

---

## 9. 驗收（對應 spec §6.4，按本階段範圍調整）

1. 為某部門放一份 `brands/<dept>.yaml`，該部門 user 觸發 job → log 顯示採用該 `style_id`、flux_prompt 尾巴帶該 suffix。
2. 換成另一部門（或無檔部門）→ 採用對應風格 / 退 default，且因 style_id 不同不命中舊 cache。
3. 放一份壞掉的 YAML → job 仍正常完成（退 default），log 有 warning。

（實機觸發一律 user 從 frontend 點，本機 read-only 撈 log。）

---

## 10. 風險

- 部門名含特殊字元（中文、空白）作檔名：Linux 支援，但 ops 命名需與 `department_name` 完全一致才命中；不一致 → 安靜退 default（可由驗收 1 的 log 發現）。
- brand.yaml 改了 suffix，但 FLUX 服從度未解（見 §1 限制）→ 視覺結果可能仍偏離期望風格；這是 Redux/LoRA 的後續工作，非本階段缺陷。
- 唯讀掛載：csp 只讀不寫，正確；ops 在 host 編輯。
