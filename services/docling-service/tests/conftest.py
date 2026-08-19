"""app.config 的 Settings 是 module-level 實體化,而 app.main 的 fail-loud
會在 import 時就檢查 token → env 必須在任何 app.* import 之前就位。
pytest 保證 conftest 先於 test module 載入,所以放這裡。

與 services/asr-decoder/tests/conftest.py 同構:出貨啟動字串是
`uvicorn app.main:app`(模組層級 `app = create_app()`),import 即需 token。
test module 用 create_app() 注入 fake converter,但 import app.main 的那
一刻、模組層級那行 `app = create_app()` 就已經跑了——token 不在就是
RuntimeError。
"""

import os

os.environ.setdefault("DOCLING_SERVICE_TOKEN", "conftest-token-0123456789abcdef0123456789abcdef")
