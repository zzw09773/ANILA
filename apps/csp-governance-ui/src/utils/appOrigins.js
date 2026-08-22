// Cross-app origins — never rewrite localhost ↔ 127.0.0.1, never drop the port.
//
// Production (nginx 443): the three SPAs share one origin
//   / = 治理中心, /anila = 任務中心, /anilalm = 知識庫／產出中心
// Vite `npm run dev`: three ports on the SAME hostname
//   5173 治理中心, 5174 知識庫, 5175 任務中心
//
// Cookie jar is host-keyed. Mixing localhost and 127.0.0.1 looks like a
// role flip on refresh. Always reuse window.location.hostname.

const DEV_PORTS = {
  governance: '5173',
  shell: '5175',
  knowledge: '5174',
}

const PROD_PREFIX = {
  governance: '',
  shell: '/anila',
  knowledge: '/anilalm',
}

function trimSlash(value) {
  return String(value || '').replace(/\/+$/, '')
}

function ensurePath(path) {
  if (!path) return '/'
  return path.startsWith('/') ? path : `/${path}`
}

function currentLocation() {
  if (typeof window === 'undefined' || !window.location) {
    return { protocol: 'http:', hostname: 'localhost', origin: '', port: '' }
  }
  return window.location
}

function isViteDevRuntime() {
  return Boolean(import.meta.env?.DEV)
}

function configuredOrigin(which) {
  const key = {
    governance: 'VITE_GOVERNANCE_ORIGIN',
    shell: 'VITE_SHELL_ORIGIN',
    knowledge: 'VITE_KNOWLEDGE_ORIGIN',
  }[which]
  const raw = key ? import.meta.env?.[key] : ''
  return raw ? trimSlash(raw) : ''
}

export function appOrigin(which) {
  const explicit = configuredOrigin(which)
  if (explicit) return explicit

  const loc = currentLocation()
  if (isViteDevRuntime()) {
    const port = DEV_PORTS[which] || loc.port
    const host = loc.hostname || 'localhost'
    const protocol = loc.protocol || 'http:'
    return trimSlash(`${protocol}//${host}:${port}`)
  }
  return trimSlash(loc.origin || '')
}

export function appHref(which, path = '/') {
  const origin = appOrigin(which)
  const prefix = isViteDevRuntime() ? '' : PROD_PREFIX[which]
  const suffix = ensurePath(path)
  if (prefix && suffix === '/') return `${origin}${prefix}/`
  if (prefix && suffix.startsWith(prefix + '/')) return `${origin}${suffix}`
  if (prefix && suffix === prefix) return `${origin}${prefix}/`
  return `${origin}${prefix}${suffix}`
}

export function loginHref(nextHref) {
  const url = `${appOrigin('governance')}/login`
  if (!nextHref) return url
  return `${url}?next=${encodeURIComponent(nextHref)}`
}

export function shellWorkbenchHref() {
  return isViteDevRuntime() ? appHref('shell', '/app') : appHref('shell', '/app')
}

export function knowledgeHref(path = '/') {
  return appHref('knowledge', path)
}

export function governanceHref(path = '/') {
  return appHref('governance', path)
}

/** Same-host destination from a next= candidate. Rejects open redirects. */
export function safeNextDestination(candidate, fallback) {
  const loc = currentLocation()
  if (typeof candidate !== 'string' || !candidate) return fallback
  const trimmed = candidate.trim()
  if (!trimmed || trimmed.includes('\\') || /[\u0000-\u001F]/.test(trimmed)) {
    return fallback
  }

  const base = loc.origin || `${loc.protocol || 'http:'}//${loc.hostname || 'localhost'}`
  try {
    const url = new URL(trimmed, base)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return fallback
    if (url.hostname !== loc.hostname) return fallback
    if (loc.protocol && url.protocol !== loc.protocol) return fallback
    return url.toString()
  } catch {
    return fallback
  }
}
