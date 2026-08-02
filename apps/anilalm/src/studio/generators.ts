import { chatComplete } from '../api/chat'
import { searchCollection, type SearchHit } from '../api/search'
import { COMMON_PREAMBLE } from '../generated/preamble'
import { useArtifactStore } from '../store/artifacts'
import {
  createReportJob,
  createMindmapJob,
  createInfographicJob,
  createDatatableJob,
} from '../api/studio'
import { createArtifactTask } from '../api/tasks'
import type {
  Collection,
  DatatableArtifact,
  InfographicArtifact,
  IngestionDocument,
  MindmapArtifact,
  ReportArtifact,
  SlidesArtifact,
} from '../types'

// Preset 中文 label → backend enum string mapping。跟 CommandModal 的對齊。
const PRESET_ENUM: Record<string, Record<string, string>> = {
  report: {
    深度技術綜述: 'deep_tech_review',
    重點摘要: 'key_summary',
    教學講義: 'teaching_handout',
    對外溝通文件: 'external_comms',
  },
  mindmap: {
    概念樹: 'concept_tree',
    任務拆解: 'task_breakdown',
    'SOP 流程': 'sop_flow',
    SOP流程: 'sop_flow',
    組織關係: 'org_relationships',
  },
  infographic: {
    '任務 Dashboard': 'mission_dashboard',
    任務Dashboard: 'mission_dashboard',
    數據簡報: 'stats_brief',
    比較矩陣: 'comparison_matrix',
    時間軸總覽: 'timeline_overview',
  },
  datatable: {
    關鍵指標彙整: 'key_figures',
    實體屬性表: 'entity_attributes',
    時間軸表: 'timeline_table',
    並排比較: 'comparison_table',
  },
}

function presetEnum(kind: keyof typeof PRESET_ENUM, label: string): string {
  return PRESET_ENUM[kind]?.[label] ?? label
}

const DEFAULT_MODEL =
  (import.meta.env.VITE_DEFAULT_CHAT_MODEL as string | undefined) ?? 'gpt-4o-mini'

// Studio retrieves more chunks than chat (top-K=12) because Report and
// Slides synthesise across the whole document set, not a single Q&A
// turn. We trim each chunk's content harder (800 chars) so 12 hits fit
// in the prompt budget alongside the structural instructions.
const STUDIO_TOP_K = 12
const STUDIO_MIN_SCORE = 0.25
const STUDIO_CONTENT_LIMIT = 800

// 共同前導改由 SSOT 供應（src/generated/preamble.ts，無 React 依賴）——
// 之前這裡與 WSChat 各養一份語言規則文字，已實際漂移過，不再複製。

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
 * Mirrors ``services/csp/app/api/studio.py:_extract_json_object``;
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

/**
 * Report v2:呼叫 anila-studio backend `/api/reports/jobs`,得 pending job,
 * 推進 artifactStore;由 WSStudio 的 polling effect 持續更新 state。
 *
 * legacy v1 函式簽名保留(回傳 ReportArtifact),但內容由 backend pipeline
 * 產出(HTML / PDF / DOCX 三檔在 backend 落地,artifact.downloadUrls 帶回)。
 */
export async function generateReport({
  collection,
  docs,
  preset,
  extraInstructions,
}: GenerateReportInput): Promise<ReportArtifact> {
  // Slice 8b: bind a CSP Task before launching (degrades to null on failure).
  const binding = await createArtifactTask({
    title: `${collection.name} · 深度報告`,
    outputType: 'report',
    collectionIds: [collection.id],
  })
  const status = await createReportJob({
    collectionId: collection.id,
    preset: presetEnum('report', preset) as never,
    extraInstructions,
    documentIds: docs.map((d) => d.id),
    binding: binding ?? undefined,
  })
  const artifact: ReportArtifact = {
    id: newId(),
    kind: 'report',
    collectionId: collection.id,
    title: status.title ?? `${collection.name} · 深度報告`,
    preset,
    sourceCount: docs.length,
    createdAt: new Date().toISOString(),
    state: 'pending',
    jobId: status.job_id,
    step: status.step ?? null,
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
    COMMON_PREAMBLE,
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

// ── Mindmap / Infographic / Datatable ─────────────────────────────────
//
// 三種都走相同 pattern:呼叫 backend create*Job → pending artifact 推進
// store → WSStudio polling effect 接手狀態更新。
// 沒前端 LLM call(完全 backend 處理)。

export interface GenerateMindmapInput {
  collection: Collection
  docs: IngestionDocument[]
  preset: string
  extraInstructions?: string
  maxDepth?: number
}

export async function generateMindmap({
  collection,
  docs,
  preset,
  extraInstructions,
  maxDepth,
}: GenerateMindmapInput): Promise<MindmapArtifact> {
  const binding = await createArtifactTask({
    title: `${collection.name} · 心智圖`,
    outputType: 'mindmap',
    collectionIds: [collection.id],
  })
  const status = await createMindmapJob({
    collectionId: collection.id,
    preset: presetEnum('mindmap', preset) as never,
    extraInstructions,
    documentIds: docs.map((d) => d.id),
    maxDepth,
    binding: binding ?? undefined,
  })
  const artifact: MindmapArtifact = {
    id: newId(),
    kind: 'mindmap',
    collectionId: collection.id,
    title: status.title ?? `${collection.name} · 心智圖`,
    preset,
    sourceCount: docs.length,
    createdAt: new Date().toISOString(),
    state: 'pending',
    jobId: status.job_id,
    step: status.step ?? null,
  }
  useArtifactStore.getState().add(artifact)
  return artifact
}

export interface GenerateInfographicInput {
  collection: Collection
  docs: IngestionDocument[]
  preset: string
  extraInstructions?: string
}

export async function generateInfographic({
  collection,
  docs,
  preset,
  extraInstructions,
}: GenerateInfographicInput): Promise<InfographicArtifact> {
  const binding = await createArtifactTask({
    title: `${collection.name} · 資訊圖表`,
    outputType: 'infographic',
    collectionIds: [collection.id],
  })
  const status = await createInfographicJob({
    collectionId: collection.id,
    preset: presetEnum('infographic', preset) as never,
    extraInstructions,
    documentIds: docs.map((d) => d.id),
    binding: binding ?? undefined,
  })
  const artifact: InfographicArtifact = {
    id: newId(),
    kind: 'infographic',
    collectionId: collection.id,
    title: status.title ?? `${collection.name} · 資訊圖表`,
    preset,
    sourceCount: docs.length,
    createdAt: new Date().toISOString(),
    state: 'pending',
    jobId: status.job_id,
    step: status.step ?? null,
  }
  useArtifactStore.getState().add(artifact)
  return artifact
}

export interface GenerateDatatableInput {
  collection: Collection
  docs: IngestionDocument[]
  preset: string
  extraInstructions?: string
  targetColumns?: string[]
}

export async function generateDatatable({
  collection,
  docs,
  preset,
  extraInstructions,
  targetColumns,
}: GenerateDatatableInput): Promise<DatatableArtifact> {
  const binding = await createArtifactTask({
    title: `${collection.name} · 資料表`,
    outputType: 'datatable',
    collectionIds: [collection.id],
  })
  const status = await createDatatableJob({
    collectionId: collection.id,
    preset: presetEnum('datatable', preset) as never,
    extraInstructions,
    documentIds: docs.map((d) => d.id),
    targetColumns,
    binding: binding ?? undefined,
  })
  const artifact: DatatableArtifact = {
    id: newId(),
    kind: 'datatable',
    collectionId: collection.id,
    title: status.title ?? `${collection.name} · 資料表`,
    preset,
    sourceCount: docs.length,
    createdAt: new Date().toISOString(),
    state: 'pending',
    jobId: status.job_id,
    step: status.step ?? null,
  }
  useArtifactStore.getState().add(artifact)
  return artifact
}
