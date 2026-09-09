# 版本定義（2026-09-02 擁有者裁定）

- **v1.0.0 ＝ 這條線（restart/from-redesign）2026-09-02 收工的狀態**，也是 `main` 的新起點。
  上線交付包必須從乾淨 tag 建：`MANIFEST.txt` 的 `Ref` 要是 `v1.0.0` 這種形狀，
  `INTRANET-LOAD.sh` 才會當出貨包載入；`v1.0.0-3-gabcdef0` 或 `-dirty` 都是演練包，會拒載。
- **舊線是 legacy**：2026-07-28 以前的 `main`（tag `attic/2026-07-28/main`、分支 `legacy/main-2026-07-28`）
  與舊 tag `v1.0.0`（2026-06-14 內網版）、`v1.1.0`、`v1.2.0`、`v2.0.0`、`v2.0.1`（全部改名為 `legacy/<原名>`）。
  舊線也有一個 v1.0.0，號碼相同或比較大都是歷史，不代表比較新；**不要拿 legacy 線的東西回來合**。
- 之後照語意版本：修 bug 升 patch（v1.0.1）、加功能升 minor（v1.1.0）；每次出貨都打 tag 再建包。
- **v1.2.2**（2026-09-09）：A01 空 `allowed_origins` fail-closed、F6 deploy verify 失敗傳播、CSP 映像補 curl。正式 image bundle 的 compose project 是 `anila`，不再用 `anila-restart`。
