/**
 * UI mirror of the CSP agent-reply observation.
 *
 * The backend deliberately reports only a short observed completion.  This
 * sentence must not infer why the reply was short or what the remote service
 * did.  Missing and unknown metadata stay silent for older messages.
 */

export const AGENT_REPLY_OBSERVATION_KEY = "agent_reply_observation";

export const AGENT_REPLY_SHORT_NOTICE =
  "平台觀察到這則代理回覆異常簡短，無法僅由回覆長度判斷原因。";

export function agentReplyNotice(meta) {
  const observation =
    meta && typeof meta === "object"
      ? meta[AGENT_REPLY_OBSERVATION_KEY]
      : null;
  if (!observation || typeof observation !== "object") return null;
  if (observation.short_reply !== true) return null;
  if (
    !Number.isInteger(observation.completion_tokens) ||
    observation.completion_tokens < 0
  ) {
    return null;
  }
  if (observation.usage_source !== "reported" && observation.usage_source !== "estimated") {
    return null;
  }
  return AGENT_REPLY_SHORT_NOTICE;
}
