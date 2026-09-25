// 對話思考檔位（default / off / standard / deep）。
// 存的是檔位而不是後端 reasoning_effort 字串；換模型時檔位不變，由 CSP 重對映。

export const THINKING_TIERS = ["default", "off", "standard", "deep"];
export const THINKING_TIER_STORAGE_KEY = "anila.thinkingTier";
export const UNPROBED_REASON = "此模型尚未探測思考等級";
export const GLM_OFF_LABEL = "關閉（不顯示思考）";
export const GLM_OFF_REASON = "不顯示思考過程；模型仍可能消耗思考 tokens";

export function normalizeThinkingTier(value) {
  if (value == null || value === "") return "default";
  return THINKING_TIERS.includes(value) ? value : "default";
}

/** Registry name leaf starts with glm — not display_name. */
export function isGlmFamily(modelOrName) {
  const raw = typeof modelOrName === "string"
    ? modelOrName
    : String(modelOrName?.name || "");
  const leaf = raw.trim().toLowerCase().split("/").pop() || "";
  return leaf.startsWith("glm");
}

/** This reply asked to hide thinking. Do not read the conversation row. */
export function isThinkingDisplayOff(applied) {
  return normalizeThinkingTier(applied?.tier) === "off";
}

/** Stamp the display setting for the reply being sent, before meta arrives. */
export function outgoingThinkingApplied({ oneShotDeep, thinkingTier } = {}) {
  if (oneShotDeep) return { tier: "deep", source: "turn" };
  const normalized = normalizeThinkingTier(thinkingTier);
  if (normalized === "default") return null;
  return { tier: normalized, source: "conversation" };
}

/** unprobed | binary（預設／關閉／開啟）| graded（四檔） */
export function thinkingPickerMode(levelsSupported) {
  if (!Array.isArray(levelsSupported) || levelsSupported.length === 0) {
    return "unprobed";
  }
  const nonNone = levelsSupported.filter((level) => level !== "none");
  return nonNone.length <= 1 ? "binary" : "graded";
}

function offOption({ enabled, model }) {
  if (isGlmFamily(model)) {
    return {
      tier: "off",
      label: GLM_OFF_LABEL,
      enabled,
      reason: enabled ? GLM_OFF_REASON : UNPROBED_REASON,
    };
  }
  return {
    tier: "off",
    label: "關閉",
    enabled,
    ...(enabled ? {} : { reason: UNPROBED_REASON }),
  };
}

export function thinkingPickerOptions(levelsSupported, model = null) {
  const mode = thinkingPickerMode(levelsSupported);
  if (mode === "unprobed") {
    return [
      { tier: "default", label: "依模型預設", enabled: true },
      offOption({ enabled: false, model }),
      { tier: "standard", label: "標準", enabled: false, reason: UNPROBED_REASON },
      { tier: "deep", label: "深入", enabled: false, reason: UNPROBED_REASON },
    ];
  }
  if (mode === "binary") {
    return [
      { tier: "default", label: "依模型預設", enabled: true },
      offOption({ enabled: true, model }),
      { tier: "standard", label: "開啟", enabled: true },
    ];
  }
  return [
    { tier: "default", label: "依模型預設", enabled: true },
    offOption({ enabled: true, model }),
    { tier: "standard", label: "標準", enabled: true },
    { tier: "deep", label: "深入", enabled: true },
  ];
}

/** 這一題深入想寫在回覆 meta 上時，重試要重放同一則覆寫。 */
export function shouldReplayOneShotDeep(thinkingApplied) {
  return thinkingApplied?.source === "turn" && thinkingApplied?.tier === "deep";
}

/** 回覆列檔位文案：關閉／標準／深入。default 不標。off 折疊本身會被藏。 */
export function thinkingAppliedTierLabel(tier, model = null) {
  const normalized = normalizeThinkingTier(tier);
  if (normalized === "default") return null;
  const match = thinkingPickerOptions(["none", "low", "medium", "xhigh"], model).find(
    (opt) => opt.tier === normalized,
  );
  return match?.label || null;
}

export function thinkingTriggerLabel(tier, levelsSupported, model = null) {
  const normalized = normalizeThinkingTier(tier);
  if (
    thinkingPickerMode(levelsSupported) === "binary"
    && (normalized === "standard" || normalized === "deep")
  ) {
    return "開啟";
  }
  const match = thinkingPickerOptions(levelsSupported, model).find((opt) => opt.tier === normalized);
  return match?.label || "依模型預設";
}

export function isThinkingOptionActive(optionTier, value, levelsSupported) {
  const normalized = normalizeThinkingTier(value);
  if (thinkingPickerMode(levelsSupported) === "binary" && optionTier === "standard") {
    return normalized === "standard" || normalized === "deep";
  }
  return optionTier === normalized;
}

export function readStoredThinkingTier() {
  try {
    if (typeof window === "undefined" || !window.localStorage) return "default";
    return normalizeThinkingTier(window.localStorage.getItem(THINKING_TIER_STORAGE_KEY));
  } catch {
    return "default";
  }
}

export function persistThinkingTierPreference(tier) {
  try {
    if (typeof window === "undefined" || !window.localStorage) return;
    window.localStorage.setItem(THINKING_TIER_STORAGE_KEY, normalizeThinkingTier(tier));
  } catch {
    // 私人模式或配額不足時略過；下一則新對話退回預設。
  }
}

export function conversationSelectionFromServer(serverRow) {
  const next = {
    routerModelId: serverRow?.router_model_id ?? null,
    routerModelName: serverRow?.router_model_name ?? null,
    routerSelectionVersion: serverRow?.router_selection_version ?? 0,
  };
  // PUT /router-model 只回模型三欄。缺 thinking_tier 時不可寫成 default，
  // 否則換模型會把對話檔位蓋掉，選單像被重設。
  if (serverRow && Object.prototype.hasOwnProperty.call(serverRow, "thinking_tier")) {
    next.thinkingTier = normalizeThinkingTier(serverRow.thinking_tier);
  }
  // 缺欄位的舊回應不能把「已不能用」洗掉；有欄位才跟著伺服器。
  if (serverRow && Object.prototype.hasOwnProperty.call(serverRow, "router_model_unavailable_reason")) {
    next.routerModelUnavailableReason = serverRow.router_model_unavailable_reason || null;
  }
  if (serverRow && Object.prototype.hasOwnProperty.call(serverRow, "router_model_display_name")) {
    next.routerModelDisplayName = serverRow.router_model_display_name || null;
  }
  return next;
}
