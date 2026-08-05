"""Retrieval outcome vocabulary shared by the RAG-backed pipelines.

A retrieval step has **three** outcomes, not two:

1. **hits** — the search ran and returned chunks. Ground the answer.
2. **zero hits** — the search ran and the corpus had nothing above the
   similarity floor. That is a legitimate statement about the corpus and
   the existing prompt copy already says so.
3. **failed** — the search never produced an answer (csp down, token
   expired mid-job, network). Nothing is known about the corpus.

Collapsing (3) into (2) is the defect these constants exist to close:
telling the model "本次未檢索到相關段落" after an exception hands it a
false premise, and it will reason confidently from it. The report
pipeline (``report_runner``) refuses to run at all in case (3); the
slide / datatable / infographic pipelines still ship an artifact (that
is the product contract — ``skip_retrieval`` is a supported mode), so
they must *declare* the degradation instead: honest prompt copy for the
model, ``warning`` on the job status for the user.

Both strings are user- and model-facing, so they carry no hostnames, no
exception text, no stack detail — the exception itself goes to the
operator log at WARNING.
"""

from __future__ import annotations

# Soft warning surfaced on JobStatus.warning (the same channel the LLM
# fallback deck already uses). Coexists with state="done": the artifact
# is downloadable, it is just not grounded in the user's documents.
RETRIEVAL_FAILED_WARNING = (
    "知識庫檢索失敗，本次內容未依據您的文件生成，請查證後再使用或稍後重試。"
)

# Prompt copy replacing the "本次未檢索到相關段落" line when the search
# errored. ``where`` names the per-pipeline field the disclosure belongs
# in (speaker_notes / takeaway / notes).
RETRIEVAL_FAILED_PROMPT_NOTE = (
    "（重要:本次知識庫檢索**失敗**——搜尋沒有成功執行,並不是知識庫裡"
    "沒有相關內容。你手上沒有任何文件依據,不得聲稱已查過知識庫,也不得"
    "說文件裡找不到資料。請依使用者輸入產出保守草稿,並在 {where} 明確"
    "寫出「本次未能讀取知識庫,內容未經文件佐證」。）"
)
