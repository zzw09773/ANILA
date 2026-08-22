// Cross-app origins — never rewrite localhost ↔ 127.0.0.1, never drop the port.
// See apps/csp-governance-ui/src/utils/appOrigins.js for the same rules.

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
  if (prefix && suffix.startsWith(`${prefix}/`)) return `${origin}${suffix}`
  if (prefix && suffix === prefix) return `${origin}${prefix}/`
  return `${origin}${prefix}${suffix}`
}

export function loginHref(nextHref) {
  const url = `${appOrigin('governance')}/login`
  if (!nextHref) return url
  return `${url}?next=${encodeURIComponent(nextHref)}`
}

export function governanceHref(path = '/') {
  return appHref('governance', path)
}

export function knowledgeHref(path = '/') {
  return appHref('knowledge', path)
}

export function shellWorkbenchHref() {
  return appHref('shell', '/app')
}
