// 對話思考檔位（default / off / standard / deep）。
// 存的是檔位而不是後端 reasoning_effort 字串；換模型時檔位不變，由 CSP 重對映。

export const THINKING_TIERS = ["default", "off", "standard", "deep"];
export const THINKING_TIER_STORAGE_KEY = "anila.thinkingTier";
export const UNPROBED_REASON = "此模型尚未探測思考等級";

export function normalizeThinkingTier(value) {
  if (value == null || value === "") return "default";
  return THINKING_TIERS.includes(value) ? value : "default";
}

/** unprobed | binary（預設／關閉／開啟）| graded（四檔） */
export function thinkingPickerMode(levelsSupported) {
  if (!Array.isArray(levelsSupported) || levelsSupported.length === 0) {
    return "unprobed";
  }
  const nonNone = levelsSupported.filter((level) => level !== "none");
  return nonNone.length <= 1 ? "binary" : "graded";
}

export function thinkingPickerOptions(levelsSupported) {
  const mode = thinkingPickerMode(levelsSupported);
  if (mode === "unprobed") {
    return [
      { tier: "default", label: "依模型預設", enabled: true },
      { tier: "off", label: "關閉", enabled: false, reason: UNPROBED_REASON },
      { tier: "standard", label: "標準", enabled: false, reason: UNPROBED_REASON },
      { tier: "deep", label: "深入", enabled: false, reason: UNPROBED_REASON },
    ];
  }
  if (mode === "binary") {
    return [
      { tier: "default", label: "依模型預設", enabled: true },
      { tier: "off", label: "關閉", enabled: true },
      { tier: "standard", label: "開啟", enabled: true },
    ];
  }
  return [
    { tier: "default", label: "依模型預設", enabled: true },
    { tier: "off", label: "關閉", enabled: true },
    { tier: "standard", label: "標準", enabled: true },
    { tier: "deep", label: "深入", enabled: true },
  ];
}

export function thinkingTriggerLabel(tier, levelsSupported) {
  const normalized = normalizeThinkingTier(tier);
  if (
    thinkingPickerMode(levelsSupported) === "binary"
    && (normalized === "standard" || normalized === "deep")
  ) {
    return "開啟";
  }
  const match = thinkingPickerOptions(levelsSupported).find((opt) => opt.tier === normalized);
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
  return {
    routerModelId: serverRow?.router_model_id ?? null,
    routerModelName: serverRow?.router_model_name ?? null,
    routerSelectionVersion: serverRow?.router_selection_version ?? 0,
    thinkingTier: normalizeThinkingTier(serverRow?.thinking_tier),
  };
}
