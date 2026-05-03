/**
 * Icon resolver — concept → Heroicons SVG → PNG buffer.
 *
 * The Studio LLM emits icon_rows[].concept as a *semantic keyword*
 * (e.g. "data_pipeline", "security"). We deliberately don't let the
 * LLM pick raw react-icons names because:
 *   1. It hallucinates names that don't exist (`FaInnovation`,
 *      `HiGrowth`).
 *   2. It can mix icon families across a deck (`HiUser` next to
 *      `FaUser`) which destroys visual consistency.
 *   3. Concept→icon mapping is a place WE want to refine over time
 *      based on what looks good — putting it in this file means we
 *      change one row instead of redoing the prompt.
 *
 * We commit to a single icon family: **Heroicons (hi2)**. Reason:
 *   - Clean modern look, neutral enough for technical & business decks.
 *   - Available in `react-icons/hi2` which we vendored for offline use.
 *   - Path data is plain SVG; no external CDN at runtime.
 *
 * Pipeline:
 *   1. Parse `react-icons/hi2/index.js` once at startup, extract every
 *      icon's GenIcon descriptor (a small JSON tree of <svg><path/></svg>).
 *   2. On request, pick the descriptor for the concept's mapped name,
 *      synthesise an SVG string with the requested colour & size,
 *      run it through `sharp` to get a PNG buffer, return as base64.
 *   3. Unknown concepts return null — caller (server.js) skips drawing
 *      the icon for that row, leaving heading + description in place.
 */

const fs = require('node:fs')
const path = require('node:path')
const sharp = require('sharp')

// Source file we parse for icon descriptors. The vendored react-icons
// has stable shape ("module.exports.HiName = function HiName (props) {
// return GenIcon({...descriptor...})(props); }") and we'd rather regex
// it once than depend on a private API the package didn't promise.
const HI2_INDEX = path.join(
  __dirname,
  'node_modules',
  'react-icons',
  'hi2',
  'index.js',
)

// ── Concept → Heroicons name mapping ────────────────────────────────
//
// Single source of truth on what semantic keyword maps to which icon.
// Adding a new concept means: (1) add a row here, (2) add it to the
// whitelist in api/studio.py's _build_generation_prompt. Do both or
// the LLM won't know it can be used.
//
// Picks intentionally lean toward concrete, non-cliché icons:
//   - insight → Sparkles (avoids the lightbulb cliché)
//   - growth_metric → ArrowTrendingUp ONLY when truly numeric — most
//     "growth" should be expressed via metrics or stat_callout layout
//     instead, hence no `growth` concept in the table
//   - network maps to GlobeAlt only because the prompt restricts its
//     use to "真的講國際" — generic "connection" is `integration`.
const CONCEPT_MAP = Object.freeze({
  // 資料/運算
  data_storage: 'HiCircleStack',
  data_pipeline: 'HiArrowsRightLeft',
  dataset: 'HiTableCells',
  automation: 'HiBolt',
  integration: 'HiPuzzlePiece',
  deployment: 'HiRocketLaunch',

  // 人/角色
  user: 'HiUser',
  team: 'HiUserGroup',
  customer: 'HiUsers',

  // 溝通
  chat: 'HiChatBubbleLeftRight',
  email: 'HiEnvelope',
  notification: 'HiBell',
  broadcast: 'HiSpeakerWave',

  // 分析/結果
  insight: 'HiSparkles',
  metrics: 'HiChartBar',
  comparison: 'HiScale',
  search: 'HiMagnifyingGlass',

  // 時間
  schedule: 'HiCalendarDays',
  deadline: 'HiClock',
  history: 'HiArrowPath',

  // 品質/安全
  security: 'HiShieldCheck',
  validation: 'HiCheckBadge',
  error: 'HiExclamationTriangle',
  success: 'HiCheckCircle',
  achievement: 'HiTrophy',

  // 系統
  settings: 'HiCog6Tooth',
  server: 'HiServer',
  cloud: 'HiCloud',
  network: 'HiGlobeAlt',

  // 文件/學習
  document: 'HiDocumentText',
  book: 'HiBookOpen',
  learning: 'HiAcademicCap',
})

// Lazy-built map of icon name → GenIcon descriptor.
let DESCRIPTORS_CACHE = null

/**
 * Parse the vendored react-icons/hi2/index.js once and cache a map of
 * `{HiName: descriptor}`. The file is ~1 MB and we only need to do it
 * at startup (or first call); subsequent loads are O(1) lookups.
 *
 * The format we extract:
 *   module.exports.HiUser = function HiUser (props) {
 *     return GenIcon({"tag":"svg","attr":{...},"child":[...]})(props);
 *   }
 *
 * The descriptor inside the GenIcon call is well-formed JSON. We
 * isolate it via regex anchored on the literal text we control
 * (we vendored this file; if upstream changes their codegen we'd
 * notice on rebuild).
 */
function loadDescriptors() {
  if (DESCRIPTORS_CACHE) return DESCRIPTORS_CACHE
  const text = fs.readFileSync(HI2_INDEX, 'utf8')
  // /s flag → "." matches newlines so we can absorb line breaks if
  // codegen ever pretty-prints. Greedy quantifiers would match too far,
  // so we use a non-greedy capture and anchor on `})(props);`.
  const re =
    /module\.exports\.(\w+)\s*=\s*function\s+\w+\s*\(props\)\s*\{\s*return GenIcon\((\{.+?\})\)\(props\);/gs
  const map = {}
  for (const m of text.matchAll(re)) {
    try {
      const name = m[1]
      const descriptor = JSON.parse(m[2])
      map[name] = descriptor
    } catch (e) {
      // Skip malformed rows silently. If a non-trivial portion of the
      // catalog fails to parse we'd notice in logs once we try to use
      // them; we don't fail the whole renderer over one bad icon.
      console.warn(`[icons] failed to parse descriptor for ${m[1]}:`, e.message)
    }
  }
  DESCRIPTORS_CACHE = map
  return map
}

/**
 * Synthesize an SVG XML string from a GenIcon descriptor.
 *
 * The descriptor is a tree like:
 *   {
 *     tag: "svg",
 *     attr: { viewBox, fill, "aria-hidden" },
 *     child: [
 *       { tag: "path", attr: { d, fillRule, clipRule }, child: [] },
 *       ...
 *     ]
 *   }
 *
 * Heroicons use `fill="currentColor"` so we replace currentColor with
 * the requested colour. Width/height go on the root `<svg>` so
 * downstream sharp doesn't have to compute viewport scaling.
 */
function descriptorToSvg(descriptor, { color = '#1A1A1A', size = 256 } = {}) {
  const viewBox = descriptor.attr?.viewBox || '0 0 24 24'

  /** Recurse the descriptor tree into XML text. */
  const renderNode = (node) => {
    const attrs = Object.entries(node.attr || {})
      .map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`)
      .join(' ')
    const children = (node.child || []).map(renderNode).join('')
    return `<${node.tag} ${attrs}>${children}</${node.tag}>`
  }
  // Replace any `currentColor` references with the actual hex; covers
  // both the root `fill` attr and any path-level overrides.
  const inner = (descriptor.child || []).map(renderNode).join('')
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" ` +
    `width="${size}" height="${size}" fill="${color}">${inner}</svg>`
  return svg.replace(/currentColor/g, color)
}

/**
 * Resolve a concept keyword to a PNG buffer (via SVG → sharp).
 * Returns `null` if the concept isn't in CONCEPT_MAP or the resolved
 * Heroicons name doesn't exist in the catalog. Caller should treat
 * null as "skip drawing this icon" — the surrounding text still renders.
 *
 * `color` should be a hex string with a leading `#` ("#1E2761"). `size`
 * is the PNG side-length in pixels at 96 dpi; pptxgenjs sizes images
 * by inches, so a 200-px PNG embedded at 0.5" wide stays sharp on
 * screen and slightly oversampled for projection.
 */
async function renderIconPng(concept, { color = '#1A1A1A', size = 256 } = {}) {
  if (!concept || typeof concept !== 'string') return null
  const heroName = CONCEPT_MAP[concept]
  if (!heroName) return null

  const map = loadDescriptors()
  const descriptor = map[heroName]
  if (!descriptor) {
    // CONCEPT_MAP points at an icon name we couldn't parse — not the
    // LLM's fault, log so we notice when upstream changes.
    console.warn(
      `[icons] concept "${concept}" → "${heroName}" missing from catalog`,
    )
    return null
  }

  const svg = descriptorToSvg(descriptor, { color, size })
  return sharp(Buffer.from(svg)).png().toBuffer()
}

module.exports = {
  CONCEPT_MAP,
  renderIconPng,
  // Exposed for tests / introspection — not used by server.js.
  _internal: { loadDescriptors, descriptorToSvg },
}
