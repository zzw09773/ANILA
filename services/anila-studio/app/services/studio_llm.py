"""Studio LLM helpers — prompt building + csp chat-completion calls.

Extracted from ``api/studio.py`` as part of the god-module split. These are
the *foundation* the layout / vision-QA modules build on: they all reach the
LLM through :func:`call_llm_chat` (and the two adapter classes that wrap it),
so this module must import cleanly without touching ``app.api.studio`` — that
is what breaks the circular dependency the later extractions would otherwise
hit.

Contents:
  * ``build_generation_prompt`` — composes the (system, user) prompt pair
    (uses the module-private ``_PRESET_COUNT`` / ``_count_hint`` mapping).
  * ``call_llm_chat`` — the csp ``/v1/chat/completions`` proxy call, with
    csp's typed errors mapped to the HTTP statuses studio's callers expect.
  * ``StudioLLMAdapter`` — adapts ``call_llm_chat`` to the rewriter's
    ``complete(system, user)`` interface (FLUX Layer A).
  * ``Gemma4VlmGate`` — gemma4-backed VLM for the Stage 2 quality gate
    (FLUX Layer C).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import HTTPException

from app.clients.csp_client import (
    CspClientError,
    CspForbiddenError,
    CspNotFoundError,
    CspServerError,
    CspUnauthorizedError,
    proxy_chat_completions,
)
from app.generated_preamble import ERA_RULES, NATIONAL_TERMINOLOGY
from app.services.llm_json import extract_json_object
from app.services.retrieval_status import RETRIEVAL_FAILED_PROMPT_NOTE
from app.services.studio_config import SLIDES_LLM_MODEL, VISION_LLM_MODEL
from app.services.studio_model_primary import resolve_model_name

logger = logging.getLogger(__name__)


# Preset name → (count hint, min slides). The frontend's CommandModal
# shows these ranges as hints in the picker; without mapping them on
# the backend the prompt stays at "8-12" regardless of preset, which
# is why "經典報告結構" decks always came out at 10 instead of the
# advertised 12-15. min_slides drives the section_break-frequency rule
# below: a 5-slide Lightning Talk shouldn't be forced to insert a
# mid-deck section break.
_PRESET_COUNT: dict[str, tuple[str, int]] = {
    "經典報告結構":     ("12-15 張投影片", 12),
    "Lightning Talk":   ("5 張投影片，重點濃縮、視覺優先", 5),
    "教學投影片":       ("8-12 張投影片", 8),
}


def _count_hint(preset: str) -> tuple[str, int]:
    """Resolve a preset name to (human range, min count). Falls back to
    8-12 / min=8 for unknown / extra_instructions-only flows."""
    return _PRESET_COUNT.get(preset.strip(), ("8-12 張投影片", 8))


def build_generation_prompt(
    collection_name: str,
    preset: str,
    extra_instructions: str | None,
    chunks: list[dict[str, Any]],
    images: list[dict[str, Any]] | None = None,
    *,
    retrieval_failed: bool,
    illustrations_enabled: bool = True,
) -> tuple[str, str]:
    """Compose (system, user) prompts for the slide-deck LLM call.

    ``illustrations_enabled=False`` (no FLUX provider resolvable in this
    deployment) removes every instruction about generated illustrations
    (``image_prompt`` / ``image_kind='illustration'``): the hydration layer
    would only drop those fields again, and on 2026-09-02 that produced a
    one-line "流程圖" slide because the model spent the slide on a picture
    that could never exist. Graphviz diagrams stay — they need no FLUX.

    ``retrieval_failed=True`` means the retrieval call errored rather
    than returning nothing. An empty ``chunks`` list then does NOT mean
    "the knowledge base had no match", and the prompt must not say so —
    see ``app.services.retrieval_status``.

    Required keyword, deliberately without a default: ``False`` is the
    value that reinstates the original defect, so a caller who forgets
    it must fail at the call rather than quietly tell the model that the
    search ran and found nothing.

    Phase 3 expands the prompt with:
      * theme selection (5 options, tone-based; palette deprecated)
      * per-slide layout_kind (6 variants)
      * icon_rows.concept whitelist (80+ keywords grouped by domain;
        the LLM is asked to pick a domain first, then a concept from
        that domain — see Phase 6 Fix 4)

    The hard rule we communicate to the LLM is **bullets[] is always
    required** even when a non-standard layout_kind is chosen, because
    the renderer falls back to standard rendering of bullets if the
    layout-specific payload is malformed. This means the LLM can
    aspirationally choose a fancy layout AND still ship a usable slide
    if the layout-specific fields don't pan out.
    """
    count_hint, min_slides = _count_hint(preset)
    system = "\n".join(
        [
            "You are a JSON-only slide-deck generator. Output is parsed",
            "by a strict JSON parser, NOT by a human.",
            "",
            "Output rules (any violation = automatic rejection):",
            '- The very first character of your response MUST be "{".',
            '- The very last character of your response MUST be "}".',
            "- Do NOT include the word 'thought', 'reasoning', 'analysis',",
            "  or any commentary before or after the JSON.",
            "- Do NOT wrap in ```json or ``` code fences.",
            "- Use straight double quotes only — never single quotes ', ",
            "  curly quotes “”, or fullwidth 「」 for keys and string values.",
            "",
            "── 頂層欄位 ──",
            'Required: title (string), slides (list).',
            'Required: theme — 依文件 tone 而非主題類別挑選：',
            '  "official"         法規、行政、制度、給長官看的正式簡報（預設；院內文件多半是這種）',
            '  "corporate_navy"   嚴謹的技術／業務報告；給同事或主管看的工作產出',
            '  "academic_paper"   研究發表、論文摘要、學術會議；多量化與引用',
            '  "warm_journal"     第一人稱學習心得、回顧、softer 反思內容',
            '  "executive_brief"  給高層的 briefing、結論導向、極簡、≤ 10 張',
            '  "startup_pitch"    對外發表、產品介紹、需要視覺衝擊與情緒煽動',
            '',
            '選擇依據（在 chunks_text 中尋找這些 tone 訊號）：',
            '  - 第一人稱主觀詞（我、我的、我們、心得、反思、學到、感受）',
            '    → warm_journal',
            '  - 量化結果（百分比、N=...、F1、p-value）+ 方法論 + 引用',
            '    → academic_paper',
            '  - 「問題 / 解法 / 價值」結構 + 中性語氣 + 技術細節',
            '    → corporate_navy',
            '  - 強 call-to-action、願景語言、產品名稱反覆出現',
            '    → startup_pitch',
            '  - 只有結論沒有過程、總頁數 ≤ 10、給 C-level 看',
            '    → executive_brief',
            '訊號衝突時取最強的；無明確訊號用 official。',
            '（注意：若 title 含「心得／反思／論文／募資」等明確 framing 詞，'
            '後端會強制 override；你可不必額外處理。）',
            '整份簡報只能挑一個 theme；不要在 slides 內切換。',
            '',
            '（舊欄位名 palette 仍接受但已 deprecated，請用 theme。）',
            "",
            "── 每張投影片欄位 ──",
            'Required: title, bullets (1-6 items), speaker_notes',
            'Required: layout_kind — 從以下挑一個：',
            '  "standard"          一般內容頁（最常用，沒事就用這個）',
            '  "section_break"     章節過渡頁；title 是章節名，bullets 用 1-2 句副標',
            '  "stat_callout"      強調單一數字／百分比；要附 stat 物件',
            '  "quote"             引用名言或客戶證言；要附 quote 物件',
            '  "two_column"        對照／互補（before/after, pros/cons）；要附 columns',
            '  "icon_rows"         3-5 個並列要點，每個有 icon；要附 icon_rows',
            '  "figure"            圖：要附 figure。kind="timeline"（期限、時序）或 "org"（機關層級、',
            '                      隸屬）由系統照 items 畫；kind="svg" 你自己畫（架構、關係、對照圖）。',
            '  "process"           先後步驟／流程／程序（2-6 步）；要附 steps。**凡是流程、步驟、',
            '                      作業程序、救濟途徑這種有先後順序的內容，一律用這個，不要用 bullet。**',
            '  "table"             多個對象各有多個屬性的對照（種類×身分、方案×條件）；要附 table。',
            '                      **兩個以上對象、兩個以上屬性的比較用 table，不要用 two_column。**',
            "",
            "Layout-specific 物件（以下是欄位形狀，**不要把這些範例值複製到輸出**）：",
            '  stat 形狀：{"value": "47%", "label": "...", "supporting": "..."',
            '              , "baseline": "30%", "baseline_label": "..."}',
            '             value 是大字數值；**supporting 必填 20-100 字脈絡**；',
            '             baseline / baseline_label 為選填對照基準（有就觸發左右比較版型）。',
            '  quote 形狀：{"text": "...", "attribution": "..."}',
            '             attribution 為選填來源。',
            '  columns 形狀：[{"heading": "...", "bullets": ["...", "...", "..."]},',
            '                 {"heading": "...", "bullets": ["...", "...", "..."]}]',
            '             固定 2 個元素的陣列；多於 2 會被忽略、少於 2 會降級為 standard。',
            '             **每欄 bullets 2-3 條**（schema 最低 2 條；湊不到 2 條的對照結構,',
            '             改用 icon_rows，保留並列感、視覺更輕）。',
            '  icon_rows 形狀：[{"concept": "...", "heading": "...", "description": "..."}]',
            '             3-5 個元素；concept 必須來自下方白名單。',
            '  steps 形狀：[{"heading": "申訴", "description": "向管轄機關提出，30 日內"}, ...]',
            '             2-6 個元素，照先後順序；heading ≤ 12 字，description 一句話。',
            '  table 形狀：{"columns": ["身分", "共同懲罰", "特有懲罰"],',
            '              "rows": [["軍官", "撤職、降階", "—"], ["士兵", "記過", "禁足、罰站"]]}',
            '             2-5 欄、1-10 列，每格是短字串；第一欄是對象名稱。',
            '  figure 形狀：{"kind": "timeline", "items": [{"label": "處分送達", "note": "第 0 日"}, ...]}',
            '              {"kind": "org", "items": [{"label": "國防部"}, {"label": "服役機關", "parent": "國防部"}]}',
            '              {"kind": "svg", "svg": "<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 1200 675\'>…</svg>"}',
            '             svg 規則：只用 rect / circle / line / path / text；字型 font-family="Noto Sans CJK TC"；',
            '             每個標籤 ≤ 8 字、字級 ≥ 28；元素 ≤ 12 個；顏色用 #1E2761（深藍）、#B8922E（金）、',
            '             #6B7280（灰）、#FFFFFF；不要 script、不要外部連結、不要圖片。',
            '  （結尾的「資料來源」頁與目錄頁由系統自動加上，你不用寫。）',
            "",
            "重要：bullets 任何 layout 都要填（renderer 在 layout-specific 欄位",
            "缺漏時會回退用 bullets 渲染，不要省）。",
            "",
            "bullets 階層標記(standard / two_column 在 renderer 解析,其他 layout 忽略):",
            "  每個 bullet 開頭可加 Unicode marker 表示縮排階層,marker 跟內文中間 1 個空白。",
            "  marker 會被 renderer 剝掉(不雙重符號),並換成正確 indent + dot:",
            "    ● level 0 主要點(預設,沒 marker 也視為 level 0)",
            "    ◦ level 1 子點(縮排,父點下方延伸)",
            "    ▪ level 2 子子點(更深縮排,通常別用超過 2 層)",
            "  使用時機:當條目間自然有「主-從」關係(主項列舉 → 各項展開),用階層讓視覺",
            "  讀者一眼看出結構。沒從屬時所有條目維持 level 0,不要硬塞階層湊深度。",
            "  範例(bullets list 欄位的字串值):",
            '    "● Advanced RAG: hybrid retrieval + reranking"',
            '    "◦ BM25 + dense 加權"',
            '    "◦ Cross-encoder rerank top-100"',
            '    "● Agentic RAG: 動態決定檢索時機"',
            '    "◦ Router node 評估必要性"',
            "  schema 仍是 list[str](不是 nested),只是 renderer 看 leading marker 決定 indent。",
            "",
            "── icon_rows.concept 必須從以下白名單挑（未列出的會 fallback",
            "   為「不畫 icon」，所以不要自創；先想 domain，再從該 domain 挑）──",
            "[generic 資料/運算] data_storage data_pipeline dataset",
            "                automation integration deployment",
            "[generic 人/角色]   user team customer",
            "[generic 溝通]     chat email notification broadcast",
            "[generic 分析/結果] insight metrics comparison search",
            "[generic 時間]     schedule deadline history",
            "[generic 品質/安全] security validation error success achievement",
            "[generic 系統]     settings server cloud network",
            "[generic 文件/學習] document book learning",
            "[industrial 工業/製造] machine factory sensor defect",
            "                  quality_control calibration anomaly",
            "                  production_line inspection yield_rate",
            "[ml_ai 機器學習]   model training inference embedding",
            "                  classification regression overfitting",
            "                  generalization feature_extraction imbalance",
            "                  fine_tuning agent reasoning retrieval",
            "                  prompt evaluation prediction",
            "[system_arch 架構] supervisor worker orchestration",
            "                  hierarchy vertical_split fanout",
            "                  pipeline_stage module",
            "[process 流程]    perception cognition action step_one",
            "                  alert iteration decision monitoring",
            "[outcome 結果]    improvement reduction breakthrough limitation",
            "                  cost_saving risk",
            "[law 法規/行政]   law regulation procedure process court appeal penalty",
            "                  record announcement payroll organization personnel",
            "                  approval rights identity timeline plan goal report archive",
            "",
            "icon 規則：",
            "- **先選 domain，再從 domain 內挑 concept**：技術內容（ML/工業）",
            "  從 ml_ai / industrial / system_arch / process / outcome 挑；",
            "  一般商業/通用內容才從 generic 挑。",
            '- 同一張 icon_rows 的 concept 抽象層級要一致（全部「功能」或全部',
            "  「角色」之類），不要混。最好同 domain 內挑。",
            "- 不要硬套陳腔：success≠創新、network≠成長、achievement≠任何進步；",
            "  挑該行真正在表達的概念。",
            "",
            "── 設計心法（重要：這幾條會決定簡報專業感） ──",
            "1. **Less is more**：能少就少。寧可 3 個 bullet 寫得清楚，不要 6 個 bullet",
            "   每個 1 行勉強塞滿。每個元素都要有它存在的理由。",
            "2. **不要 data slop**：不要硬塞數字、不要為了「看起來有資訊量」而捏",
            "   進百分比或統計。數據只在「真的關鍵」時才放，這時改用 stat_callout。",
            "3. **整份簡報要有節奏**：用 section_break 切章節（每 3-5 張用一張過渡），",
            "   用 stat_callout / quote / icon_rows 在文字頁之間製造對比。**全部用",
            "   standard 是 AI slop 的標誌**。",
            "4. **layout 服務於內容、不為花俏而花俏**：選 stat_callout 因為這個數字",
            "   是這張的核心訊息；選 two_column 因為內容真的天然有對照關係；不要為了",
            "   「我用過這幾個 layout 顯得很努力」而硬塞。",
            "5. **承諾或從簡**（commit fully or keep simple）：要花俏就整份花俏；",
            "   要簡潔就整份簡潔。一張花俏配一張無聊是最差的配對。",
            "",
            "── 最重要的硬規則（違反 = deck 不合格） ──",
            f"**規則 0 / 投影片數量**：本次 preset 要求 **{count_hint}**。"
            f"少於 {min_slides} 張視為違反規則，請務必達到下限；"
            f"上限可彈性放寬以容納所有重點。",
            "**規則 1 / 第一張投影片必須是 section_break**：以簡報主題作為 title，",
            "  bullets 第 0 條寫一句副標說明。這是整份 deck 的封面，沒有它整份簡報",
            "  讀起來像流水帳。**不要把第一張做成 standard layout**，直接 layout_kind",
            "  填 'section_break' 即可。**這是規則第 1 條，不是建議**。",
            (
                "**規則 2 / 至少再有 1 張 section_break**：放在簡報三分之一或一半處"
                "作為章節分隔（例：「方法」、「實驗結果」、「結論」）。"
                "沒有章節隔段的長簡報是 AI slop 的標誌。"
            ) if min_slides >= 8 else (
                "**規則 2 / Lightning Talk 不需中段 section_break**：5 張的短簡報"
                "已被首張封面 + 內容流自然分節，不要硬塞額外 section_break。"
            ),
            "**規則 3 / standard 不可超過 60%**：技術內容穿插 icon_rows，"
            "章節穿插 section_break，數字穿插 stat_callout。",
            "**規則 4 / 數據必須有 stat_callout 至少 1 張**：若下方 chunks 出現",
            "  **任何百分比、實驗數值、KPI、提升幅度、F1/Recall/Accuracy 數字、",
            "  樣本數 N=...、誤差降幅** 之類，**必須**挑最關鍵的那一個做 stat_callout，",
            "  把該數字大字呈現。例：「MAPE 降低 88.73%」、「F1-score 0.92」、",
            "  「N=10,000」。**沒有 stat_callout 的數據型 deck = 視覺陽春**。",
            "**規則 5 / 對照型內容必須 two_column**：若內容有「A vs B」",
            "  （例：原始 vs 融合、本研究 vs 既有方法、有無 data augmentation、",
            "  Cross-machine 之間比較），用 1 張 two_column 拆成兩欄。",
            "**規則 6 / 若可用圖清單非空，必須至少 1 張 image_focus**：把「相關性",
            "  最高的那張」做 image_focus（layout_kind='image_focus' + 設 image_ref）。",
            "  論文 / 技術文件的圖（架構圖、實驗結果圖）幾乎都比文字描述更有說服力。",
            *(
                [
                    "  **後備規則 / 即時生成（Studio Fix 2 拆兩種）**：若「可用圖」清單為空、",
                    "  或全部都不夠相關，但該 slide 主題明顯需要視覺輔助，依內容選一種模式：",
                    "    (A) 情境插畫、無文字 → image_kind='illustration' + image_prompt",
                    "        （英文 50-500 字，主體/場景/構圖/風格），走 FLUX。",
                    "    (B) 含 label 的圖示（架構/流程/ER）→ image_kind='diagram' + diagram_dot",
                    "        （Graphviz DOT，最多 3000 字），走 graphviz。**FLUX 畫不出可讀文字**。",
                    "  **每張 slide 只能設 image_ref / illustration / diagram 其一，三者互斥**。",
                ]
                if illustrations_enabled
                else [
                    "  **後備規則**：可用圖為空時，只有含 label 的架構／流程圖可以即時生成：",
                    "  image_kind='diagram' + diagram_dot（Graphviz DOT，最多 3000 字）。",
                    "  本部署沒有插畫產生器，**不要**要求即時插畫；",
                    "  沒有圖可放的主題就老實用 standard / icon_rows，把內容寫滿。",
                ]
            ),
            "",
            "── 引用「圖片描述」段落（這是 deck 變具體的關鍵） ──",
            "下方檢索段落中可能含「圖片描述：...」的段落 — 那是文件原圖的",
            "VLM 描述（含軸標、數值、座標、組件等具體資訊）。**bullet 必須優先",
            "從這些段落取材**，例如「Figure 3 雙分支架構顯示左 RGB / 右 Tsallis」、",
            "「圖 4 結果柱狀圖：CT350 機台達到 88.73% 改善」這種具體寫法，",
            "而非抽象的「本研究透過資訊融合提升效能」。**沒有具體 = bullet 失敗**。",
            "",
            "── 言之有物、不密密麻麻（最重要的品質規則） ──",
            "- 每張內容頁都要有 **key_message**：一句 ≤ 40 字的結論，讀者只看這句也知道這頁在說什麼。",
            "  例：「申訴要在處分送達次日起 30 日內提出，逾期不受理。」不是「本頁介紹申訴」。",
            "- 每張 **3-4 條 bullet、每條 ≤ 35 字**；一張只講一件事。內容多就拆兩張，不要塞。",
            "- bullet 要有具體資訊：條號、期限、對象、金額、機關名稱。沒有具體資訊的句子不要寫。",
            "- 有「時間、期限、先後」的內容用 figure timeline 或 process；有「隸屬、層級」用 figure org；",
            "  有「架構、關係」你自己畫 svg。一份簡報至少 2 張圖（figure / process / table 任一）。",
            "",
            "── 整體內容規則 ──",
            "- 使用**台灣繁體中文**（不只字符繁體、用詞也要台灣本土）。",
            f"- 投影片數量：{count_hint}（首張固定為 section_break，規則 1）。",
            "- 每張 3-6 個 bullet（layout 不需要 bullet 也要填 1-2 句保險用）。",
            "- speaker_notes 寫 2-4 句講者口述稿。",
            "- standard slide 的 title 不可重複（section_break 例外、可重複）。",
            "",
            NATIONAL_TERMINOLOGY,
            "",
            ERA_RULES,
            "",
            "── 其他 layout 條件選用 ──",
            "- **stat_callout**：文件含量化結果（百分比、實驗數值、KPI、提升幅度）",
            "  時，挑最關鍵的 1 個做 stat_callout。stat.value 是大字數字，",
            "  stat.label 是該數字代表什麼，stat.supporting 是補充細節。",
            "  **必填 supporting：寫 20-100 字的數字脈絡**（baseline、樣本數、",
            "  實驗條件、結果意義）；**不可只寫「重要突破」「顯著進步」這類空話**。",
            "  若有對照基準，**強烈建議**填 stat.baseline + stat.baseline_label，",
            "  renderer 會自動切成左右對比版型（baseline ← → value），視覺更有力。",
            "  範例：{value:\"95%\", label:\"CT350 機臺鐵屑覆蓋率偵測率\",",
            "         supporting:\"雙分支架構相比單分支 ResNet18 基準的 78%，提升 17 個百分點；",
            "         測試集為 10 個機臺切換批次，N=2,400\",",
            "         baseline:\"78%\", baseline_label:\"單分支基準\"}",
            "- **two_column**：內容天然有對照（before/after、本研究 vs 既有方法、",
            "  兩種模型架構比較）時用 1 張。columns 必須 **2 個元素**、各填 heading + bullets。",
            "  **每欄 2-3 條 bullet**（schema 最低 2 條），讓兩欄視覺密度對稱、不留大片空白；",
            "  湊不到 2 條的對照結構,改用 icon_rows（保留並列感、視覺更輕）。",
            "- **quote**：有名言、客戶證言、概念金句時用。",
            "- **icon_rows**：3-5 個並列要點各有 icon。concept 從白名單挑。",
            "- **image_focus**（Phase 5 新增）：文件原檔有相關插圖時用。例：",
            "  論文的 architecture diagram、實驗結果柱狀圖、流程圖。設",
            "  layout_kind='image_focus' 並把使用者訊息「可用圖」清單中相對應的",
            "  image_id 填到 Slide.image_ref。bullets 仍要寫 2-4 條，描述圖之外的",
            "  補充資訊；圖會佔投影片左半，bullets 在右半。一張圖只應出現在一張投影片。",
            "",
            "  ── image_focus 即時生成（Studio Fix 2，2026-05-18）──",
            "  若可用圖清單為空或都不合用，可即時生成：",
            "",
            "  **自動規則 — 觸發 diagram path**：若 slide 的 title 含「架構、拓撲、",
            "  拓樸、流程、Workflow、Pipeline、Topology」其中一個關鍵字，且該 slide",
            "  主題自然需要視覺輔助（例如「Multi-Agent Supervisor 拓撲設計」、",
            "  「Agentic Workflow 三階段」、「RAG 系統架構」），**必須**設",
            "  layout_kind='image_focus' + image_kind='diagram' + diagram_dot",
            "  （Graphviz DOT）。",
            "",
            *(
                [
                    "  (A) **illustration** — 情境插畫、概念意象、**無文字**的視覺輔助。",
                    "      設 image_kind='illustration' + image_prompt（**英文** 50-500 字，",
                    "      含主體 / 場景 / 構圖 / 風格）。走 FLUX.2-dev 即時生成。",
                    "      適合：主題情境（如「山地戰術部隊」「無人機巡邏」）、抽象概念、",
                    "      氣氛圖。**注意：FLUX 無法畫出可讀的文字**，所以不要叫它畫架構圖。",
                    "",
                ]
                if illustrations_enabled
                else []
            ),
            "  (B) **diagram** — 含 label 的圖示（架構圖、流程圖、Venn、決策樹、ER）。",
            "      設 image_kind='diagram' + diagram_dot（**Graphviz DOT** 語法，",
            "      最多 3000 字元）。走 graphviz `dot -Tpng` 渲染，label 清晰可讀。",
            "      適合：系統架構圖、Multi-Agent 拓撲、資料流、實體關係、決策樹。",
            "",
            "      DOT 範例（Multi-Agent Supervisor 架構）：",
            "        digraph G {",
            "          rankdir=TB;",
            "          fontname=\"Noto Sans CJK TC\";",
            "          node [fontname=\"Noto Sans CJK TC\", shape=box, style=rounded];",
            "          Supervisor -> \"Worker A\";",
            "          Supervisor -> \"Worker B\";",
            "          Supervisor -> \"Worker C\";",
            "        }",
            "",
            *(
                [
                    "  **每張 slide 只能選一種模式**：image_ref / image_kind='illustration' /",
                    "  image_kind='diagram'，三者互斥。含 label 的圖示**一定走 diagram**，",
                    "  不要丟給 FLUX 畫，否則 label 會變亂碼。",
                ]
                if illustrations_enabled
                else [
                    "  **每張 slide 只能選一種**：image_ref 或 image_kind='diagram'。",
                ]
            ),
            "- **commit fully**：選了豐富版型就把欄位填好；不要半途而廢。",
            "- **layout_kind 拼寫精確**：'standard' / 'section_break' / 'stat_callout' /",
            "  'quote' / 'two_column' / 'icon_rows' / 'image_focus'。",
            "  其他寫法會被歸類為 standard。",
            "",
            "── 台灣用語對映（簡中用詞 → 台灣慣用詞，務必使用右邊） ──",
            "  視頻 → 影片        軟件 → 軟體        硬件 → 硬體",
            "  網絡 → 網路        激光 → 雷射        信息 → 資訊",
            "  數據 → 資料        默認 → 預設        登錄 → 登入",
            "  內存 → 記憶體      分辨率 → 解析度    打印 → 列印",
            "  鼠標 → 滑鼠        優化 → 最佳化      屏幕 → 螢幕",
            "  服務器 → 伺服器    單擊 → 點擊        集成 → 整合",
            "**法規／行政文本的本義詞不要改**：程序（procedure）、項目、文件、質量、",
            "  支持、應用 在原文怎麼寫就怎麼保留，不要套上面的表。",
            "**注意**：上面只是樣本，請整體用台灣慣用語；輸出後系統會跑自動轉換做",
            "兜底，但你寫對的話品質更高。",
            "- 不可使用 placeholder（lorem ipsum / TBD / TODO / <insert ...>）。",
            "",
            "若使用者訊息提供了檢索到的段落，請以那些段落為事實依據；",
            "bullets 可在末尾用 (參 [N]) 標註來源。",
        ]
    )

    parts = [
        f"知識庫名稱：{collection_name}",
        f"風格 preset：{preset}",
    ]
    if chunks:
        parts.append("")
        parts.append("以下是從知識庫檢索到的相關段落（已依相似度排序）：")
        parts.append("")
        for i, c in enumerate(chunks, start=1):
            parts.append(
                f"[{i}] 來源：{c['filename']}（chunk {c['chunk_key']}，"
                f"相似度 {c['score']:.3f}）"
            )
            parts.append(c["content"])
            parts.append("")
    elif retrieval_failed:
        parts.append(RETRIEVAL_FAILED_PROMPT_NOTE.format(where="speaker_notes"))
    else:
        parts.append(
            "（本次未檢索到相關段落；請依使用者輸入直接發揮，"
            "並在末尾的 speaker_notes 內提醒「本草稿未取得文件支撐」。）"
        )
    if images:
        parts.append("")
        parts.append("── 可用圖（從文件原始嵌入圖中依與本主題的相似度檢索） ──")
        parts.append(
            "若某張投影片用以下任一張圖更具說服力，請設 layout_kind='image_focus' "
            "並把該行的 image_id 填到 Slide.image_ref。一張圖只應被一張投影片引用；"
            "若全部圖都不夠相關，請忽略這份清單、不要硬塞。"
            + (
                "若該 slide 需要圖但此清單無合適現有圖，layout_kind='image_focus' 下兩種模式擇一："
                "（A）image_kind='illustration' + image_prompt（英文 50-500 字描述，FLUX 即時生成情境插畫）；"
                "（B）image_kind='diagram' + diagram_dot（Graphviz DOT，最多 3000 字，graphviz 渲染含 label 的架構/流程圖）。"
                "image_ref / illustration / diagram 三者互斥，一張 slide 只設其一；含文字 label 的圖一律走 diagram。"
                if illustrations_enabled
                else
                "若該 slide 需要圖但此清單無合適現有圖，只能用 image_kind='diagram' + diagram_dot"
                "（Graphviz DOT，最多 3000 字，適合含 label 的架構/流程圖）；image_ref 與 diagram 二擇一。"
            )
        )
        parts.append("")
        for i, im in enumerate(images, start=1):
            cap = (im.get("caption") or "").replace("\n", " ")[:240]
            page = im.get("page")
            parts.append(
                f"[img_{i}] image_id={im['image_id']} "
                f"page={page if page is not None else '?'} "
                f"score={im.get('score', 0):.3f}"
            )
            parts.append(f"  caption: {cap}")
        parts.append("")

    if extra_instructions:
        parts.append("")
        parts.append(f"使用者補充指示：\n{extra_instructions}")

    return system, "\n".join(parts)


async def call_llm_chat(
    bearer: str,
    model_name: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.4,
    max_tokens: int | None = None,
) -> str:
    """Invoke csp's ``/v1/chat/completions`` and return content.

    The csp proxy owns the ``model_registry`` lookup, the upstream
    routing decision (vLLM / Ollama / external), token-usage metering,
    and per-department billing. anila-studio's role here is to pass
    the model name + messages and the caller's bearer; csp does the
    rest — usage rows still appear in the same dashboards as user chat
    because the bearer carries the same identity claims.

    Returns the assistant ``content`` string. Raises:
      * ``HTTPException(502)`` when csp returns an unexpected shape;
      * ``HTTPException(401/403/404)`` when csp surfaces a typed
        ``CspClientError`` subclass (token expired, no access, model
        not registered).
    """
    # 主簡報模型旋鈕：呼叫端沒特別指定（傳的是預設名）就用 csp 指定的那顆。
    model_name = await resolve_model_name(model_name, SLIDES_LLM_MODEL)
    try:
        response = await proxy_chat_completions(
            model=model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            bearer=bearer,
        )
    except CspNotFoundError as exc:
        # csp's proxy returns 404 when the requested model_name is not
        # in model_registry — preserve the 503 surfaced by the legacy
        # implementation so existing callers / dashboards don't see a
        # status-code regression.
        raise HTTPException(
            status_code=503,
            detail=f"Studio LLM '{model_name}' not registered or inactive.",
        ) from exc
    except CspUnauthorizedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except CspForbiddenError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (CspServerError, CspClientError) as exc:
        raise HTTPException(
            status_code=502,
            detail=f"csp proxy failed for model '{model_name}': {exc}",
        ) from exc

    try:
        return str(response["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as e:
        raise HTTPException(
            status_code=502,
            detail=f"LLM '{model_name}' returned an unexpected payload shape.",
        ) from e


class StudioLLMAdapter:
    """Adapts ``call_llm_chat`` to the ``complete(system, user)`` interface
    the FLUX prompt rewriter (Layer A) expects.

    The rewriter is deliberately decoupled from CSP internals
    (ModelRegistry, usage metering) — it only needs "give me one completion
    for this system+user pair". This thin wrapper binds the bearer+model
    context so rewriter calls still flow through csp's
    ``/v1/chat/completions`` and land in the same token-usage
    dashboards as every other Studio LLM call.
    """

    def __init__(self, bearer: str, model_name: str = SLIDES_LLM_MODEL) -> None:
        self._bearer = bearer
        self._model_name = model_name

    async def complete(self, *, system: str, user: str) -> str:
        return await call_llm_chat(
            self._bearer,
            self._model_name,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # Low temp: the rewriter wants a stable, deterministic visual
            # description, not creative variance (variance comes from the
            # FLUX seed, not the prompt).
            temperature=0.2,
        )


class Gemma4VlmGate:
    """gemma4-backed VLM for the Stage 2 quality gate (Layer C).

    gemma4 is already deployed and multimodal; ``_inspect_slide_visually``
    above proves the OpenAI vision message shape (``image_url`` with a
    ``data:image/png;base64,...`` URL) reaches it through
    ``call_llm_chat`` -> csp proxy. This adapter reuses that exact path
    for the gate's semantic + text check, so no separate VLM is deployed
    (per the confirmed Stage 2 premise).

    Exposes ``async check(png_bytes, *, concept) -> dict`` — the Vlm
    Protocol the gate expects.
    """

    def __init__(self, bearer: str, model_name: str = VISION_LLM_MODEL) -> None:
        self._bearer = bearer
        self._model_name = model_name

    async def check(self, png_bytes: bytes, *, concept: str) -> dict:
        b64 = base64.b64encode(png_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        system_prompt = (
            "You are an image quality gate for slide illustrations. "
            "Answer with JSON only — first char {, last char }, no ```json "
            "fence, no preamble."
        )
        user_text = (
            f"Does this image depict an abstract, text-free illustration of: "
            f"{concept}?\n"
            "Does it contain ANY letters, characters, digits, logos, or "
            "readable signage?\n"
            "Also rate 0.0-1.0 how cleanly and aptly it depicts the concept "
            "(1.0 = excellent, on-concept, no text or artifacts).\n"
            'Answer JSON only: {"match": bool, "has_text": bool, '
            '"score": <0.0-1.0>, "reason": "<short>"}'
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        raw = await call_llm_chat(
            self._bearer, self._model_name, messages, temperature=0.1,
        )
        try:
            parsed = json.loads(extract_json_object(raw))
        except (ValueError, json.JSONDecodeError):
            # Unparseable verdict — fail CLOSED for the gate (treat as a
            # reject) so a flaky VLM response doesn't slip an unverified
            # image through. The retry loop / fallback handles it.
            logger.warning(
                "VLM gate returned unparseable response; treating as reject."
            )
            return {"match": False, "has_text": True, "score": 0.0, "reason": "unparseable"}
        score_raw = parsed.get("score", 0.0)
        try:
            score = max(0.0, min(1.0, float(score_raw)))
        except (TypeError, ValueError):
            score = 0.0
        return {
            "match": bool(parsed.get("match")),
            "has_text": bool(parsed.get("has_text")),
            "score": score,
            "reason": str(parsed.get("reason", "")),
        }
