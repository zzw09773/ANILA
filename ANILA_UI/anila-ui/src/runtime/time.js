// Human-readable zh-TW relative time. Pass the ISO or epoch of the event;
// calling without an argument treats "now" as the reference point (so freshly
// created conversations read "剛剛" until the next sidebar tick advances them).
export function relativeLabel(when) {
  if (when == null) return "剛剛";
  const then = typeof when === "number" ? when : new Date(when).getTime();
  if (Number.isNaN(then)) return "剛剛";
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 60) return "剛剛";
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `${mins} 分鐘前`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} 小時前`;
  const days = Math.floor(hrs / 24);
  if (days < 30) return `${days} 天前`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months} 個月前`;
  return `${Math.floor(months / 12)} 年前`;
}
