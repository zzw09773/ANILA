"""app.config 的 Settings 是 module-level 實體化,而 app.main 的 fail-loud
會在 import 時就檢查 token → env 必須在任何 app.* import 之前就位。
pytest 保證 conftest 先於 test module 載入,所以放這裡。
"""

import os

os.environ.setdefault("ASR_DECODER_TOKEN", "conftest-token")
