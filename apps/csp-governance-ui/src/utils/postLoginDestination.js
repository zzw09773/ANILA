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
      description: '提問、指派助手、追蹤進行中的工作。',
      href: shellWorkbenchHref(),
    },
    {
      id: 'knowledge',
      label: '我的知識庫',
      description: '整理個人與專案資料，供提問與產出引用。',
      href: knowledgeHref('/'),
    },
    {
      id: 'outputs',
      label: '產出中心',
      description: '從知識庫生成報告、簡報、心智圖與資料表。',
      href: knowledgeHref('/outputs'),
    },
    {
      id: 'projects',
      label: '專案入口',
      description: '開啟已核准的院內服務與專案工具。',
      href: `${shellWorkbenchHref()}?panel=services`,
    },
  ]
}
