"""當前要職事實——防止模型憑過時訓練語料亂答人事。

⚠ 這個檔案是**唯一**要維護的地方：人事異動時改 ``OFFICEHOLDERS`` 與
``FACTS_AS_OF``，不動其他程式。名單刻意只列平台情境最常被問到的職位；
要加職位就加一列，前導文字會自動跟上。

⚠⚠ 就任資訊由撰寫時的公開資料填入（資訊時點見 ``FACTS_AS_OF``），
**部署前請院方核對一次**——特別是院長，異動頻率高於部長與總統。
模型端已附防護語句：未列出的職位不臆測、文件有更新以文件為準。
"""

from __future__ import annotations

#: 資訊時點（顯示在前導裡，提醒模型與讀者這是快照不是即時資料）。
FACTS_AS_OF = "2026年8月"

#: (職稱, 姓名, 補充說明或空字串)
OFFICEHOLDERS: tuple[tuple[str, str, str], ...] = (
    ("中華民國總統", "賴清德", "2024年5月20日就任"),
    ("國防部部長", "顧立雄", "2024年5月20日就任"),
    ("國家中山科學研究院院長", "李世強", "2024年起任職"),
)


def _render_officeholders() -> str:
    lines = []
    for title, name, note in OFFICEHOLDERS:
        suffix = f"（{note}）" if note else ""
        lines.append(f"- {title}：{name}{suffix}")
    return "\n".join(lines)


#: 組好的前導段落，由 common_preamble 併入 COMMON_PREAMBLE。
CURRENT_FACTS = f"""【當前要職（資訊時點：{FACTS_AS_OF}；異動以院內公告為準）】
{_render_officeholders()}
- 上列以外的職位與人事，若你不確定現任者，明說「建議查閱最新公告」，
  不要臆測人名；提供的文件或使用者給出更新的人事資訊時，以較新者為準。"""
