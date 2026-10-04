// 桌面側欄寬度。只算夾限，不寫入任何儲存。
export const SIDEBAR_DEFAULT = 272;
export const SIDEBAR_MIN = 220;
export const SIDEBAR_HARD_MAX = 420;
export const SIDEBAR_STEP = 16;
export const SIDEBAR_RESERVE = 360;

export function sidebarWidthBounds(viewportWidth) {
  const viewport = Number(viewportWidth);
  const room = Number.isFinite(viewport)
    ? Math.floor(viewport - SIDEBAR_RESERVE)
    : SIDEBAR_HARD_MAX;
  const max = Math.min(SIDEBAR_HARD_MAX, Math.max(SIDEBAR_MIN, room));
  return { min: SIDEBAR_MIN, max };
}

export function clampSidebarWidth(width, viewportWidth) {
  const { min, max } = sidebarWidthBounds(viewportWidth);
  const raw = Number(width);
  const next = Number.isFinite(raw) ? raw : SIDEBAR_DEFAULT;
  return Math.min(max, Math.max(min, Math.round(next)));
}

export function sidebarWidthFromKey(key, width, viewportWidth) {
  const { min, max } = sidebarWidthBounds(viewportWidth);
  const current = clampSidebarWidth(width, viewportWidth);
  if (key === "ArrowLeft") return Math.max(min, current - SIDEBAR_STEP);
  if (key === "ArrowRight") return Math.min(max, current + SIDEBAR_STEP);
  if (key === "Home") return min;
  if (key === "End") return max;
  return null;
}
