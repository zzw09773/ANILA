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
