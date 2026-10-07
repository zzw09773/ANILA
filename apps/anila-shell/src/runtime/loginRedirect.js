// 登入頁與這個 SPA 走同一個對外 HTTPS 埠。用「以 / 開頭」的相對網址，
// 瀏覽器會留在現在的 host 與埠。不要用 hostname 重組成絕對網址：
// hostname 不含埠，8443（或 8444–8450）會被送去 443，那裡可能是另一台 nginx。

export function cspLoginHref(loc, { carryNext = true } = {}) {
  if (!carryNext) return "/login";
  const next = `${loc?.pathname || "/"}${loc?.search || ""}${loc?.hash || ""}`;
  return `/login?next=${encodeURIComponent(next)}`;
}
