// One-way latch helpers for the classification flag.
//
// Security invariant (Wave B / anila_plan.md §Decision):
// Once any of these observers report classified:
//   - the conversation's prior classified flag
//   - the resolved agent's requiresEncryption
//   - the server's meta.classified
// the conversation AND its messages latch to classified=true forever. No code
// path may downgrade (flip back to false).

/**
 * Decide whether a conversation should be classified after observing a new
 * agent or server meta. Never returns false when the conversation was already
 * classified — this is the critical latch.
 *
 * @param {{classified?: boolean}} conversation
 * @param {{agentRequiresEncryption?: boolean, metaClassified?: boolean}} signals
 * @returns {boolean}
 */
export function computeConversationClassified(conversation, signals = {}) {
  const prior = Boolean(conversation?.classified);
  const fromAgent = Boolean(signals.agentRequiresEncryption);
  const fromMeta = signals.metaClassified === true;
  return prior || fromAgent || fromMeta;
}

/**
 * Append the "classified" tag exactly once to a tags array when classification
 * first engages. Returns the original array when the tag already exists.
 *
 * @param {string[] | undefined} tags
 * @returns {string[]}
 */
export function appendClassifiedTag(tags) {
  const list = tags || [];
  return list.includes("classified") ? list : [...list, "classified"];
}

// Four-level classification labels (zh-TW), low → high. Single source of truth
// for the level badge; mirrors the backend ClassificationLevel contract
// (services/csp/app/schemas/contracts/classification.py). The floor level
// 無機密 never gets a badge — it is the un-classified default.
export const CLASSIFICATION_LEVELS = [
  "無機密",
  "營業秘密",
  "密",
  "機密",
];

const CLASSIFICATION_FLOOR = "無機密";

/**
 * Decide the zh-TW level label to render as a badge for a conversation, or
 * null when nothing extra should show.
 *
 * Defensive by design: `classification_level` (snake_case from the backend
 * payload) / `classificationLevel` (mapped camelCase) is added by the
 * multi-level classification work (Slice 3). When the field is ABSENT — older
 * payloads that only carry the boolean `classified` latch — this returns null
 * so the caller falls back to the existing boolean indicator unchanged. The
 * floor level 無機密 and any unrecognised value also return null (fail toward
 * the boolean fallback rather than rendering an unknown badge).
 *
 * @param {{classification_level?: string, classificationLevel?: string}} conversation
 * @returns {string|null}
 */
export function classificationLevelBadge(conversation) {
  const level =
    conversation?.classificationLevel ?? conversation?.classification_level;
  if (typeof level !== "string") return null;
  const trimmed = level.trim();
  if (!trimmed || trimmed === CLASSIFICATION_FLOOR) return null;
  return CLASSIFICATION_LEVELS.includes(trimmed) ? trimmed : null;
}

/**
 * Pure reducer used by `applyMeta`: given the previous conversation and the
 * new meta payload, return the updated conversation object — or the original
 * when nothing needs to change. Never downgrades `classified`.
 *
 * @param {{id: string|number, classified?: boolean, tags?: string[]}} conversation
 * @param {{classified?: boolean}} meta
 */
export function latchConversationWithMeta(conversation, meta) {
  if (meta?.classified !== true) return conversation;
  if (conversation.classified) return conversation;
  return {
    ...conversation,
    classified: true,
    tags: appendClassifiedTag(conversation.tags),
  };
}
