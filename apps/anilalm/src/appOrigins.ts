// Cross-app origins — never rewrite localhost ↔ 127.0.0.1, never drop the port.
// See apps/csp-governance-ui/src/utils/appOrigins.js for the same rules.

const DEV_PORTS = {
  governance: '5173',
  shell: '5175',
  knowledge: '5174',
} as const

const PROD_PREFIX = {
  governance: '',
  shell: '/anila',
  knowledge: '/anilalm',
} as const

type AppName = keyof typeof DEV_PORTS

function trimSlash(value: string) {
  return String(value || '').replace(/\/+$/, '')
}

function ensurePath(path: string) {
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

function configuredOrigin(which: AppName) {
  const key = {
    governance: 'VITE_GOVERNANCE_ORIGIN',
    shell: 'VITE_SHELL_ORIGIN',
    knowledge: 'VITE_KNOWLEDGE_ORIGIN',
  }[which]
  const raw = key ? (import.meta.env as Record<string, string | undefined>)[key] : ''
  return raw ? trimSlash(raw) : ''
}

export function appOrigin(which: AppName) {
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

export function appHref(which: AppName, path = '/') {
  const origin = appOrigin(which)
  const prefix = isViteDevRuntime() ? '' : PROD_PREFIX[which]
  const suffix = ensurePath(path)
  if (prefix && suffix === '/') return `${origin}${prefix}/`
  if (prefix && suffix.startsWith(`${prefix}/`)) return `${origin}${suffix}`
  if (prefix && suffix === prefix) return `${origin}${prefix}/`
  return `${origin}${prefix}${suffix}`
}

export function loginHref(nextHref?: string) {
  const url = `${appOrigin('governance')}/login`
  if (!nextHref) return url
  return `${url}?next=${encodeURIComponent(nextHref)}`
}

export function shellWorkbenchHref() {
  return appHref('shell', '/app')
}

export function knowledgeHref(path = '/') {
  return appHref('knowledge', path)
}

export function governanceHref(path = '/') {
  return appHref('governance', path)
}
