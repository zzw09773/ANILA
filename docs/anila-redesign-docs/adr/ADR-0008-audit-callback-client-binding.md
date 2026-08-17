# ADR-0008: 稽核回呼(audit-callback)綁定 Service Client,預設拒絕(fail-closed)

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的決策紀錄(ADR),保留決策當時的理由與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> Status: accepted
> Date: 2026-07-02
> Deciders: ANILA 平台安全審查(commit security review)
> Related: doc-07 §3(RegisteredService schema)、doc-03(Service Client Token 命名)、doc-10 §10(audit callback)、Slice 7a、migration r1_0008

## 背景

Slice 7a 新增 `POST /api/services/{service_id}/audit-callbacks`,讓已註冊的服務
把稽核事件回報給 CSP,以其 **Service Client Token** 認證。原實作僅驗到
「這是一把合法的 Service Client Token」(`verify_service_token` 回
`kind == "service_client"`)就放行 append,**沒有**驗這把金鑰是否屬於路徑上的
那個 `{service_id}`。

後果:任何持有**任一把**合法整合金鑰的呼叫者(Router、ingestion-worker、任何
未來的 service client),都能對**任意服務**注入稽核事件 —— 造成跨服務的
audit-trail 污染(可竄改他服務的稽核紀錄、埋設誤導性事件)。稽核紀錄是分類與
鑑識的信任錨,污染它等於弱化整條可追溯鏈。

根因是 **doc-07 §3 的 `registered_services` schema 缺一個 client↔service 綁定
欄位** —— 端點在資料模型上根本沒有辦法表達「這個服務只接受這把金鑰」。屬文件層級
的缺口(doc gap),非單純程式疏漏。

## 決策

1. `registered_services` 新增 `service_client_id`(nullable FK →
   `service_clients.id`,`ON DELETE SET NULL`,建索引;migration `r1_0008`)。
2. audit-callback 端點在解出 Service Client 身分後,**額外要求**
   `registered_service.service_client_id == 解出的 client id` 才放行:
   - 未綁定(`NULL`)→ **403**,拒絕所有回呼(fail-closed 預設拒絕),寫入
     `attempted_unbound_callback` 稽核。
   - 綁定不符 → **403**,寫入 `attempted_cross_service_callback`(含
     `bound_client_id` 與 `presented_client_id`)稽核。
   - 相符 → 沿用原 append 路徑,行為不變。
3. 綁定只由 **admin-tier(系統管理員/owner)** 設定(create/update);
   **per-service admin 不得自綁**,即使該欄位被列進 `db_editable_fields` 也一律
   403(admin-only 欄位優先於 `db_editable_fields` 白名單)。

## 理由

- **Fail-closed / default-deny**:此端點全新、零正式資料,沒有相容包袱,可直接採
  最嚴設定。未綁定的服務「拒絕全部」而非「放行全部」,符合平台的單向閂鎖與最小
  權限不變量。
- **綁定=授予 audit-write 身分**:誰能改綁定,誰就能決定哪把金鑰可代表此服務寫
  稽核。若讓 per-service admin(委派角色)自綁,等於委派可以自我擴權;因此綁定
  收斂在 admin-tier,`db_editable_fields` 白名單不得覆寫這條紅線。
- **鑑識可用**:兩種拒絕都落稽核(denied),把 presented/bound client id 都記下,
  讓越權嘗試可被偵測與回溯。

## 替代方案

- **維持只驗 token 合法**:成本最低,但正是本次弱點本身 —— 不採用。
- **用 mTLS 憑證指紋(`client_cert_fingerprint`)綁定**:內網 CSPKI 下可行,但需
  每服務簽發客戶端憑證、部署面更重,且與現有 token 驗證路徑重疊;先以資料層綁定
  達到 fail-closed,憑證綁定列為未來強化。
- **綁定放行、僅記錄不阻擋(monitor-only)**:違反 fail-closed;污染已寫入才被
  發現無意義 —— 不採用。

## 影響

- **契約 / 遷移**:`registered_services` schema +1 欄(`service_client_id`);
  migration `r1_0008`(down=`r1_0007`,單一 head 維持 `r1_` 命名空間)。
  `RegisteredServiceCreate/Update/Response` 三個 schema 同步增列。
- **安全不變量**:強化稽核可追溯鏈(fail-closed、default-deny),不弱化任何既有
  不變量。
- **治理 UI**:本 ADR **不含**治理 UI(綁定的設定介面)。此欄位沿用既有防禦式渲染
  隨服務註冊表帶出,UI 綁定精靈列為後續工作。
- **待跟進工作項**:
  - **doc-07 修訂回饋**:doc-07 §3 的 `RegisteredService` schema 應正式補上
    `service_client_id` 欄位與「audit-callback 綁定、admin-tier only、fail-closed」
    的語意說明,把本次 doc gap 收束回權威文件。
  - 服務註冊表治理 UI 增設綁定欄位(admin-tier 可見/可編)。

## 憲法檢核

- [x] 不違反 `00-product-constitution.md` §5 功能准入合約
- [x] 不落入 §6 凍結清單;若落入,已在本 ADR 明述例外理由
- [x] 不弱化安全不變量(card SSO / JWT / CSRF / RLS / SSRF guard / 單向閂鎖)——
      本決策為淨強化(fail-closed 綁定)
