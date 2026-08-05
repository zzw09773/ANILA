/**
 * Per-turn retrieval for the chat panel — and the honesty rules that go
 * with it.
 *
 * A retrieval step has THREE outcomes, not two:
 *
 *   'hits'    — the search ran and returned chunks. Ground the answer.
 *   'empty'   — the search ran and nothing cleared the similarity floor.
 *               That is a true statement about the knowledge base.
 *   'failed'  — the search never produced an answer (backend down, 5xx,
 *               network). NOTHING is known about the knowledge base.
 *   'skipped' — no indexed documents, so no search was attempted.
 *
 * Collapsing 'failed' into 'empty' is the defect this module exists to
 * close. Telling the model 「本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的
 * 段落」 after the call threw hands it a false premise, which it will
 * faithfully reason from — and the user gets a fluent, confident,
 * ungrounded answer that looks exactly like a grounded one.
 *
 * Lives outside WSChat.tsx so the classification and the prompt copy can
 * be pinned by tests without mounting the whole chat panel.
 */
import { searchCollection, type SearchHit } from '../api/search'
import { COMMON_PREAMBLE } from '../generated/preamble'

// Top-K and min-score for the per-turn retrieval. 5 hits with cosine ≥ 0.3
// keeps the prompt under ~3KB even on chunky documents while filtering out
// the long tail of weakly-related neighbors that just dilute the LLM's
// attention. Dial up if users complain "the model didn't see X".
export const RAG_TOP_K = 5
export const RAG_MIN_SCORE = 0.3
// Trim chunk content before injection so a single 8KB chunk doesn't
// monopolise the prompt window. The model still gets enough to ground;
// users who want the full text click the citation card to drill in.
export const RAG_CONTENT_LIMIT = 1200
// ≈28k tokens（實測 ~1.6 chars/token）；gateway 對超過 32768 tokens 硬拒
// （ContextWindowExceededError）。整段截斷、不切斷單一 chunk。
export const MAX_SYSTEM_PROMPT_CHARS = 45000

export type RetrievalStatus = 'hits' | 'empty' | 'failed' | 'skipped'

export interface RetrievalOutcome {
  status: RetrievalStatus
  /** Empty for every status other than 'hits'. */
  hits: SearchHit[]
}

/**
 * Shown on the assistant bubble whenever the turn was produced without a
 * working search. Deliberately short and non-technical — no hostnames,
 * no status codes, no stack detail; the browser console keeps the cause
 * for whoever is debugging.
 */
export const UNGROUNDED_NOTICE =
  '⚠ 知識庫檢索失敗，這則回答沒有引用您的文件，請自行查證後再採用。'

/**
 * Key under which the ungrounded flag is persisted in a message's
 * `metadata`. csp accepts an arbitrary dict there by design, so nothing
 * server-side validates it: the writer and the reader are held together
 * by this constant and nothing else.
 *
 * Rename the *constant* freely. Changing the *string* is a wire-format
 * change — every already-stored ungrounded answer would start looking
 * grounded after a reload — which is why the tests deliberately keep the
 * literal instead of importing this, so that edit goes red.
 */
export const RETRIEVAL_FAILED_META_KEY = 'retrieval_failed'

/**
 * Run the per-turn search and classify the result.
 *
 * Never throws: a failed search must not kill the turn (a temporarily
 * down embedding service shouldn't block chat entirely). But it returns
 * `'failed'`, and every downstream consumer — prompt, bubble, persisted
 * metadata — is required to treat that differently from `'empty'`.
 */
export async function retrieveTurnContext(
  collectionId: number,
  query: string,
  signal?: AbortSignal,
): Promise<RetrievalOutcome> {
  try {
    const { data } = await searchCollection(collectionId, query, {
      topK: RAG_TOP_K,
      minScore: RAG_MIN_SCORE,
      signal,
    })
    const hits = data.results ?? []
    return { status: hits.length > 0 ? 'hits' : 'empty', hits }
  } catch (searchErr) {
    // Operator-facing detail stays here; the model and the user get the
    // sanitised copy above.
    // eslint-disable-next-line no-console
    console.warn('[anilalm] retrieval failed — answering ungrounded', searchErr)
    return { status: 'failed', hits: [] }
  }
}

export interface PromptContext {
  collectionName: string
  /** Number of documents in the collection that finished indexing. */
  indexedCount: number
}

/**
 * Build the system prompt for a turn.
 *
 * Four modes, one per outcome. `hits` may be a trimmed subset of
 * `outcome.hits` (the context-overflow retry drops chunks from the tail),
 * so it is passed explicitly rather than read off the outcome.
 *
 * The model is told to cite as `[N]` and only use the supplied chunks.
 * The citation cards in the UI map [N] → filename + chunk_key so the
 * user can verify provenance.
 */
export function buildSystemPrompt(
  outcome: { status: RetrievalStatus; hits: SearchHit[] },
  ctx: PromptContext,
): string {
  const collName = ctx.collectionName || '未指定'

  if (ctx.indexedCount === 0 || outcome.status === 'skipped') {
    return [
      COMMON_PREAMBLE,
      '',
      '你是 ANILA LM 的研究助理。',
      `知識庫名稱：「${collName}」。`,
      '使用者尚未上傳已完成索引的文件，請依使用者輸入直接作答，',
      '並提醒可上傳資料以獲得引用支撐的回答。',
    ].join('\n')
  }

  // 檢索「失敗」不等於「沒有命中」——搜尋根本沒跑成功，關於知識庫內容
  // 一無所知。這裡絕不能沿用下面那句「沒有命中相似度 ≥ 0.3 的段落」。
  if (outcome.status === 'failed') {
    return [
      COMMON_PREAMBLE,
      '',
      '你是 ANILA LM 的研究助理。',
      `當前知識庫：「${collName}」（共 ${ctx.indexedCount} 份已索引文件）。`,
      '本次知識庫檢索失敗——搜尋沒有成功執行，並不是知識庫裡沒有相關內容。',
      '你手上沒有任何文件段落。請：',
      '1) 開頭第一句就告訴使用者「知識庫檢索失敗，以下內容未經文件佐證」，',
      '2) 不得聲稱已查過知識庫，也不得說文件裡找不到資料，',
      '3) 若仍要作答，只能依你的領域知識，並明確標示這是未經佐證的推測，',
      '4) 建議使用者稍後重試。',
    ].join('\n')
  }

  if (outcome.hits.length === 0) {
    return [
      COMMON_PREAMBLE,
      '',
      '你是 ANILA LM 的研究助理。',
      `當前知識庫：「${collName}」（共 ${ctx.indexedCount} 份已索引文件）。`,
      '本次查詢在向量檢索中沒有命中相似度 ≥ 0.3 的段落。請：',
      '1) 先告知使用者「已搜尋但無高相似度命中」，',
      '2) 依你領域知識先給出嘗試性回答，並標註此回答未經文件支撐，',
      '3) 建議使用者改寫問題或上傳更相關文件。',
    ].join('\n')
  }

  const slabs = outcome.hits.map((h, i) => {
    const n = i + 1
    const trimmed =
      h.content.length > RAG_CONTENT_LIMIT
        ? h.content.slice(0, RAG_CONTENT_LIMIT) + '…'
        : h.content
    return `[${n}] 來源：${h.filename}（chunk ${h.chunk_key}，相似度 ${h.score.toFixed(3)}）\n${trimmed}`
  })

  // 從尾端整塊丟棄 chunk，直到 system prompt 不超過硬上限。
  let kept = slabs
  while (kept.length > 0) {
    const chunkBlock = kept.join('\n\n')
    const prompt = [
      COMMON_PREAMBLE,
      '',
      '你是 ANILA LM 的研究助理，以使用者知識庫的段落為依據作答。',
      `當前知識庫：「${collName}」。`,
      '',
      '以下是針對本次提問檢索到的相關段落（已依相似度排序）：',
      '',
      chunkBlock,
      '',
      '回答規則：',
      `1) 僅根據上方 ${kept.length} 個段落作答；不要編造段落中沒有的資訊。`,
      '2) 引用時用 [N] 標號（例如：「依據 [1]，...」），N 對應上方段落編號。',
      '3) 段落不足以回答時，明確說「目前段落沒有提供 X 資訊」，不要硬湊。',
      '4) 如使用者問的是檔案結構、條目順序之類的整體性問題，可彙整多個段落並交叉引用。',
      '',
      // 引用 few-shot：20B 級模型對格式的遵循靠範例不靠規則描述（設計文件 §4-4）。
      '引用示範（僅供格式參考，內容一律以上方實際段落為準）：',
      '問：測試結果有沒有達到規格要求？',
      '答：依據 [1]，本次測試成功率為 93.3%，高於 [2] 規定的 90% 下限，符合規格要求。',
      '',
      // 語言指令句尾重複：長 context 下小模型會忘記開頭指令（recency，設計文件 §6-4）。
      '請以繁體中文（台灣用語）回答。',
    ].join('\n')
    if (prompt.length <= MAX_SYSTEM_PROMPT_CHARS) return prompt
    kept = kept.slice(0, -1)
  }

  // 單段就超長時退回無段落模式說明（仍帶共同前導）。
  return [
    COMMON_PREAMBLE,
    '',
    '你是 ANILA LM 的研究助理。',
    `當前知識庫：「${collName}」。`,
    '檢索段落過長無法放入上下文，請依領域知識作答並提醒使用者縮小範圍。',
    '請以繁體中文（台灣用語）回答。',
  ].join('\n')
}
