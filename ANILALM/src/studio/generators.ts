import { chatComplete } from '../api/chat'
import { searchCollection, type SearchHit } from '../api/search'
import { useArtifactStore } from '../store/artifacts'
import type {
  Collection,
  IngestionDocument,
  ReportArtifact,
  SlidesArtifact,
} from '../types'

const DEFAULT_MODEL =
  (import.meta.env.VITE_DEFAULT_CHAT_MODEL as string | undefined) ?? 'gpt-4o-mini'

// Studio retrieves more chunks than chat (top-K=12) because Report and
// Slides synthesise across the whole document set, not a single Q&A
// turn. We trim each chunk's content harder (800 chars) so 12 hits fit
// in the prompt budget alongside the structural instructions.
const STUDIO_TOP_K = 12
const STUDIO_MIN_SCORE = 0.25
const STUDIO_CONTENT_LIMIT = 800

// Hard language directive prepended to every Studio system prompt. Same
// rules as WSChat.ZHTW_DIRECTIVE — duplicated locally instead of imported
// to keep generators.ts free of cross-feature React deps.
const ZHTW_DIRECTIVE = [
  '【語言規則・最高優先】',
  '- 一律以繁體中文（zh-TW，台灣慣用語）輸出。',
  '- 即使檢索到的段落或使用者輸入是英文 / 簡體中文 / 其他語言，仍以繁體中文撰寫。',
  '- 程式碼、API 名稱、技術專有名詞可保留原文，說明文字一律繁體中文。',
  '- 引用簡體中文原文時，於引用後加上繁體中文翻譯或對照。',
  '- 絕不在輸出中混用簡體字。',
].join('\n')

function newId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

/**
 * Extract a top-level JSON object from a raw model response.
 *
 * Handles, in order:
 *   1. ``<think>...</think>`` reasoning blocks (gemma / qwen / oss-style)
 *   2. ```` ```json ```` code fences
 *   3. ``thought ANILA LM should …\n{...}`` chain-of-thought preambles
 *   4. ``{...}\n\nNote: ...`` trailing commentary
 *   5. ``{...}`` happy path
 *
 * Mirrors ``myCSPPlatform/backend/app/api/studio.py:_extract_json_object``;
 * keep both in sync.
 */
export function extractJsonObject(raw: string): string {
  const deThought = raw.replace(/<think(?:ing)?>[\s\S]*?<\/think(?:ing)?>/gi, '')
  const noFences = deThought
    .replace(/```(?:json|JSON)?/g, '')
    .replace(/```/g, '')
    .trim()
  const start = noFences.indexOf('{')
  const end = noFences.lastIndexOf('}')
  if (start === -1 || end === -1 || end < start) {
    throw new Error(
      `Model response contained no JSON object. Got ${raw.length} chars; ` +
        `first 80: ${raw.slice(0, 80).replace(/\n/g, '⏎')}`,
    )
  }
  return noFences.slice(start, end + 1)
}

/**
 * ``JSON.parse`` plus a one-shot single-quote-to-double-quote repair.
 * gemma4 (and friends) sometimes emit Python-dict-style output with
 * single quotes; this is the same trick the backend uses in
 * ``_loads_lenient``.
 */
export function parseJsonLenient<T = unknown>(text: string): T {
  try {
    return JSON.parse(text) as T
  } catch {
    const repaired = text
      .replace(/(?<=[\[\{,:\s])'/g, '"')
      .replace(/'(?=[\]\},:\s]|$)/g, '"')
    return JSON.parse(repaired) as T
  }
}

/**
 * Pull a thematic seed query from the preset / extra instructions so
 * Studio can do retrieval without needing the user to type a chat-style
 * question. Falls back to the collection name when the user gave no
 * extra context — at worst the model lands on a "general overview"
 * subset of chunks, which is fine for both Report and Slides.
 */
function buildSeedQuery(
  collection: Collection,
  preset: string,
  extraInstructions: string | undefined,
): string {
  const parts = [collection.name, preset, extraInstructions?.trim()].filter(
    (x): x is string => Boolean(x && x.length > 0),
  )
  return parts.join(' · ')
}

async function retrieveContext(
  collection: Collection,
  preset: string,
  extraInstructions: string | undefined,
): Promise<SearchHit[]> {
  try {
    const { data } = await searchCollection(
      collection.id,
      buildSeedQuery(collection, preset, extraInstructions),
      { topK: STUDIO_TOP_K, minScore: STUDIO_MIN_SCORE },
    )
    return data.results
  } catch {
    // Search failure (no indexed docs / embedder down) → fall through
    // to filename-only mode in summariseSources.
    return []
  }
}

function summariseSources(
  docs: IngestionDocument[],
  hits: SearchHit[],
): string {
  if (hits.length > 0) {
    const lines = hits.map((h, i) => {
      const trimmed =
        h.content.length > STUDIO_CONTENT_LIMIT
          ? h.content.slice(0, STUDIO_CONTENT_LIMIT) + '…'
          : h.content
      return `[${i + 1}] 來源：${h.filename}（chunk ${h.chunk_key}，相似度 ${h.score.toFixed(3)}）\n${trimmed}`
    })
    return [
      '以下是從知識庫檢索到的相關段落（已依相似度排序）。請以此為事實依據，',
      '在輸出中以 [N] 形式引用對應段落，N 為下方編號：',
      '',
      ...lines,
    ].join('\n\n')
  }
  if (docs.length === 0) {
    return '使用者尚未上傳已索引的文件，僅依使用者指示自由發揮。'
  }
  // Indexed docs but retrieval came back empty / failed: fall back to
  // filename-only mode so generation still produces something useful.
  const lines = docs.map(
    (d, i) => `  [${i + 1}] ${d.filename}（共 ${d.chunk_count ?? 0} 段）`,
  )
  return [
    '使用者已上傳以下文件，但本次檢索沒有命中相關段落（或 embedder 暫時不可用）：',
    ...lines,
    '',
    '請：',
    '1) 以檔名作為主題範圍的依據；',
    '2) 以你的領域知識做高品質擴寫；',
    '3) 在輸出末段註明「本草稿未取得文件片段支撐，正式版本應人工核對」。',
  ].join('\n')
}

// ── Report — markdown ─────────────────────────────────────────────────

export interface GenerateReportInput {
  collection: Collection
  docs: IngestionDocument[]
  preset: string
  extraInstructions?: string
}

const REPORT_PRESET_HINTS: Record<string, string> = {
  '深度技術綜述': '以嚴謹學術語氣撰寫，章節包含「研究背景 → 主要結論 → 方法 → 限制 → 後續方向」。',
  '重點摘要': '輸出 1-2 頁等量的精華筆記，採用列點為主、不超過 800 繁體中文字。',
  '教學講義': '結構：概念定義 → 範例 → 練習題（含解答）→ 延伸閱讀。',
  '對外溝通文件': '客觀中立、避免內部專有名詞，假設讀者為非技術背景。',
}

export async function generateReport({
  collection,
  docs,
  preset,
  extraInstructions,
}: GenerateReportInput): Promise<ReportArtifact> {
  const presetHint = REPORT_PRESET_HINTS[preset] ?? ''
  const hits = await retrieveContext(collection, preset, extraInstructions)
  const system = [
    ZHTW_DIRECTIVE,
    '',
    '你是 ANILA LM 的深度報告生成器。請輸出純 Markdown（不要包在 ```markdown 代碼塊內）。',
    '結構規範：',
    '- 第一行使用 # 標題',
    '- 接著一段 100-200 字的 TL;DR（粗體前置「TL;DR：」）',
    '- 至少 3 個 ## 段落，每段含 ### 子標題、列點與必要說明',
    '- 末段 ## 引用與限制：列出文件來源、本報告的限制',
    presetHint && `風格：${presetHint}`,
    '',
    '若下方使用者訊息提供了已檢索到的段落，請僅以這些段落為事實依據，',
    '在 Markdown 內以 [N] 形式引用，並在末段「## 引用與限制」中列出 N 對應的檔名。',
  ]
    .filter(Boolean)
    .join('\n')

  const user = [
    `知識庫名稱：${collection.name}`,
    `風格 preset：${preset}`,
    summariseSources(docs, hits),
    extraInstructions ? `\n使用者補充指示：\n${extraInstructions}` : '',
  ]
    .filter(Boolean)
    .join('\n')

  const markdown = await chatComplete({
    model: DEFAULT_MODEL,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ],
    temperature: 0.3,
  })

  const titleMatch = markdown.match(/^#\s+(.+)$/m)
  const artifact: ReportArtifact = {
    id: newId(),
    kind: 'report',
    collectionId: collection.id,
    title: (titleMatch?.[1] ?? `${collection.name} · 深度報告`).trim(),
    preset,
    markdown: markdown.trim(),
    sourceCount: docs.length,
    createdAt: new Date().toISOString(),
  }
  useArtifactStore.getState().add(artifact)
  return artifact
}

// ── Slides — JSON ─────────────────────────────────────────────────────

export interface GenerateSlidesInput {
  collection: Collection
  docs: IngestionDocument[]
  preset: string
  extraInstructions?: string
}

const SLIDE_COUNT_HINT: Record<string, string> = {
  '經典報告結構': '12-15 張投影片，封面 → 大綱 → 主體 → 結論。',
  'Lightning Talk': '5 張投影片，重點濃縮、視覺優先。',
  '教學投影片': '8-12 張，每張一個概念 + 範例。',
}

interface RawSlide {
  title: string
  bullets: string[]
  speakerNotes?: string
}

export async function generateSlides({
  collection,
  docs,
  preset,
  extraInstructions,
}: GenerateSlidesInput): Promise<SlidesArtifact> {
  const countHint = SLIDE_COUNT_HINT[preset] ?? '預設 10-12 張投影片。'
  const hits = await retrieveContext(collection, preset, extraInstructions)
  const system = [
    ZHTW_DIRECTIVE,
    '',
    '你是 ANILA LM 的簡報草稿生成器。',
    '',
    '【絕對規則】整個回應必須是、且只能是一個 JSON 物件：',
    '- 第一個字元必須是 {',
    '- 最後一個字元必須是 }',
    '- 不要在 JSON 之前寫任何 thought / reasoning / 前言',
    '- 不要在 JSON 之後寫任何 note / 解釋 / 後記',
    '- 不要用 ```json 或其他代碼塊包裹',
    '',
    '格式：',
    '{ "title": "簡報標題", "slides": [{"title": "...", "bullets": ["..."], "speakerNotes": "..."}] }',
    '',
    '每張投影片 3-6 個 bullet，speakerNotes 寫成 2-4 句講者口述稿。',
    `數量：${countHint}`,
    '',
    '若使用者訊息提供了已檢索到的段落，請以那些段落為事實依據；bullets 可在末尾用 (參 [N]) 標註來源。',
  ].join('\n')

  const user = [
    `知識庫名稱：${collection.name}`,
    `風格 preset：${preset}`,
    summariseSources(docs, hits),
    extraInstructions ? `\n使用者補充指示：\n${extraInstructions}` : '',
  ]
    .filter(Boolean)
    .join('\n')

  const raw = await chatComplete({
    model: DEFAULT_MODEL,
    messages: [
      { role: 'system', content: system },
      { role: 'user', content: user },
    ],
    response_format: { type: 'json_object' },
    temperature: 0.4,
  })

  let parsed: { title?: string; slides?: RawSlide[] }
  try {
    parsed = parseJsonLenient<{ title?: string; slides?: RawSlide[] }>(
      extractJsonObject(raw),
    )
  } catch (parseErr) {
    throw new Error(
      `模型回傳不是合法 JSON：${
        parseErr instanceof Error ? parseErr.message : String(parseErr)
      }。請重試或加上補充指示。`,
    )
  }

  const slides = (parsed.slides ?? []).map((s) => ({
    title: String(s.title ?? '未命名'),
    bullets: Array.isArray(s.bullets) ? s.bullets.map(String) : [],
    speakerNotes: s.speakerNotes ? String(s.speakerNotes) : undefined,
  }))

  if (slides.length === 0) {
    throw new Error('模型沒有輸出任何投影片，請再試一次或加上補充指示。')
  }

  const artifact: SlidesArtifact = {
    id: newId(),
    kind: 'slides',
    collectionId: collection.id,
    title: parsed.title?.trim() || `${collection.name} · 簡報草稿`,
    preset,
    slides,
    sourceCount: docs.length,
    createdAt: new Date().toISOString(),
  }
  useArtifactStore.getState().add(artifact)
  return artifact
}
