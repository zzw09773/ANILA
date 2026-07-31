"""app.config 的 Settings 與 app.main 的 `app = create_app()` 都在 module
層執行,而 create_app 會 fail-loud 驗設定 → env 必須在任何 app.* import 之前
就位。pytest 保證 conftest 先於 test module 載入,所以放這裡。

這些值只為了讓 import 過得去;測試自己會建帶著明確 Settings 的 app。
"""

import os

os.environ.setdefault("ASR_DECODE_URL", "https://decoder.invalid:9000")
os.environ.setdefault("ASR_DECODER_TOKEN", "conftest-token")
