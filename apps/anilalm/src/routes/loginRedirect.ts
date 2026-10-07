// 登入頁與知識庫走同一個對外 HTTPS 埠。相對網址會留住現在的埠。
// 用 hostname 重組成絕對網址會把 8443 送去 443。

export function cspLoginHref(loc: {
  pathname?: string
  search?: string
  hash?: string
} | null | undefined): string {
  const next = `${loc?.pathname || '/'}${loc?.search || ''}${loc?.hash || ''}`
  return `/login?next=${encodeURIComponent(next)}`
}
