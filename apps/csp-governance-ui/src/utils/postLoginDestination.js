import {
  governanceHref,
  knowledgeHref,
  safeNextDestination,
  shellWorkbenchHref,
} from './appOrigins.js'

const GOVERNANCE_ROLES = new Set(['owner', 'admin', 'developer'])

export function isGovernanceRole(role) {
  return typeof role === 'string' && GOVERNANCE_ROLES.has(role)
}

/**
 * Where to send the browser after a successful login.
 * Regular users land on the task workbench. Admin / developer / owner may
 * stay in 治理中心. An explicit ?next= still wins (other SPAs bounce here).
 */
export function postLoginDestination(user, nextCandidate) {
  const fallback = isGovernanceRole(user?.role)
    ? governanceHref('/')
    : shellWorkbenchHref()
  return safeNextDestination(nextCandidate, fallback)
}

export function workbenchEntries() {
  return [
    {
      id: 'tasks',
      label: '任務中心',
      description: '用文字或語音查人事、採購、總務規範，立刻得到指引。',
      href: shellWorkbenchHref(),
    },
    {
      id: 'knowledge',
      label: '我的知識庫',
      description: '把常用規定與資料放進來，之後提問才能引用出處。',
      href: knowledgeHref('/'),
    },
    {
      id: 'outputs',
      label: '產出中心',
      description: '把查到的內容做成報告或簡報，帶著出處一起帶走。',
      href: knowledgeHref('/outputs'),
    },
    {
      id: 'projects',
      label: '專案入口',
      description: '打開已核准的院內作業系統。',
      href: `${shellWorkbenchHref()}?panel=services`,
    },
  ]
}
