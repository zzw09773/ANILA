# -*- coding: utf-8 -*-
"""app.modules — CSP 內部 module boundary 骨架。

依 doc 02 §1 / doc 10 §4(2026-07-02 MVP 拍板):Task Service、Policy
Engine、Service Launch Gateway 在 MVP 階段實作在 CSP service 內(同一
FastAPI app),但以 module boundary 隔離——各自是獨立 package,禁止跨
模組直接 import 內部實作;v1.1 後再評估抽成獨立 service。

邊界規則(由 `services/csp/.importlinter` 契約 + `infra/ci/lint-boundaries.sh`
gate 強制):

- 其他程式碼只能從各 package 根(`app.modules.tasks` / `app.modules.policy` /
  `app.modules.launch`)import 公開介面,不得 import 子模組內部實作。
- tasks / policy / launch 三者之間互不 import(independence)。
- modules 不得 import `app.api`(分層:api 可以用 modules,反向禁止)。
"""
