import { useMemo, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import type { MindmapTreeNode, MindmapTreeSpec } from '../api/studio'

// 互動式橫向心智圖(NotebookLM 式):根在左、子節點向右展開,
// 節點可逐層展開/收合到最深層;點節點本體即把該主題組成問題
// 送進對話(經 onAsk 回呼 → workspace store pendingAsk → WSChat)。
//
// 連接線是純 CSS:每列 alignItems:center 讓節點盒垂直置中於自身子樹,
// 因此「50%」永遠對準節點中心 — 水平短線放 top:50%,垂直幹線首/末子
// 各截半,毋須量測節點高度。

interface MindmapTreeProps {
  tree: MindmapTreeSpec
  onAsk: (question: string) => void
}

const STUB_W = 20 // 水平連接線長度(px)

function collectIds(node: MindmapTreeNode, acc: string[] = []): string[] {
  acc.push(node.id)
  for (const c of node.children) collectIds(c, acc)
  return acc
}

/** 點擊節點時組出送進對話的問題(帶上層脈絡,RAG 檢索更準)。 */
function buildQuestion(pathLabels: string[], node: MindmapTreeNode): string {
  const scope =
    pathLabels.length > 0 ? `在「${pathLabels.join(' › ')}」的脈絡下,` : ''
  return `${scope}請根據知識庫內容詳細說明「${node.label}」,包含定義、重點與相關細節。`
}

export function MindmapTree({ tree, onAsk }: MindmapTreeProps) {
  const { t } = useTheme()
  const allIds = useMemo(() => collectIds(tree.root), [tree])
  // 預設只展開根節點(顯示第一圈分支);其餘由使用者逐層點開。
  const [expanded, setExpanded] = useState<Set<string>>(
    () => new Set([tree.root.id]),
  )

  const toggle = (id: string) =>
    setExpanded((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* 工具列 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <button
          onClick={() => setExpanded(new Set(allIds))}
          style={toolBtnStyle(t)}
        >
          全部展開
        </button>
        <button
          onClick={() => setExpanded(new Set([tree.root.id]))}
          style={toolBtnStyle(t)}
        >
          全部收合
        </button>
        <span style={{ fontSize: 11.5, color: t.textSubtle }}>
          點「⊕」展開分支 · 點節點文字直接向 AI 提問
        </span>
      </div>

      {/* 樹本體(橫向,寬了可捲動) */}
      <div style={{ overflowX: 'auto', paddingBottom: 8 }}>
        <NodeRow
          node={tree.root}
          depth={0}
          pathLabels={[]}
          expanded={expanded}
          toggle={toggle}
          onAsk={onAsk}
        />
      </div>
    </div>
  )
}

interface NodeRowProps {
  node: MindmapTreeNode
  depth: number
  pathLabels: string[]
  expanded: Set<string>
  toggle: (id: string) => void
  onAsk: (question: string) => void
}

function NodeRow({ node, depth, pathLabels, expanded, toggle, onAsk }: NodeRowProps) {
  const { t } = useTheme()
  const hasChildren = node.children.length > 0
  const isOpen = hasChildren && expanded.has(node.id)

  return (
    <div style={{ display: 'flex', alignItems: 'center' }}>
      <NodeBox
        node={node}
        depth={depth}
        onClick={() => onAsk(buildQuestion(pathLabels, node))}
        hasChildren={hasChildren}
        isOpen={isOpen}
        onToggle={() => toggle(node.id)}
      />

      {isOpen && (
        <>
          {/* 父節點 → 子群的水平引線 */}
          <div style={{ width: STUB_W, height: 1, background: t.border, flexShrink: 0 }} />
          <div style={{ display: 'flex', flexDirection: 'column' }}>
            {node.children.map((child, i) => {
              const isFirst = i === 0
              const isLast = i === node.children.length - 1
              const only = isFirst && isLast
              return (
                <div
                  key={child.id}
                  style={{
                    position: 'relative',
                    paddingLeft: only ? 0 : STUB_W,
                    paddingTop: 3,
                    paddingBottom: 3,
                  }}
                >
                  {!only && (
                    <>
                      {/* 垂直幹線:首子從中點往下、末子往上、中間全高 */}
                      <div
                        style={{
                          position: 'absolute',
                          left: 0,
                          width: 1,
                          background: t.border,
                          top: isFirst ? '50%' : 0,
                          bottom: isLast ? '50%' : 0,
                        }}
                      />
                      {/* 對準子節點中心的水平短線 */}
                      <div
                        style={{
                          position: 'absolute',
                          left: 0,
                          top: '50%',
                          width: STUB_W,
                          height: 1,
                          background: t.border,
                        }}
                      />
                    </>
                  )}
                  <NodeRow
                    node={child}
                    depth={depth + 1}
                    pathLabels={[...pathLabels, node.label]}
                    expanded={expanded}
                    toggle={toggle}
                    onAsk={onAsk}
                  />
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

interface NodeBoxProps {
  node: MindmapTreeNode
  depth: number
  hasChildren: boolean
  isOpen: boolean
  onClick: () => void
  onToggle: () => void
}

function NodeBox({ node, depth, hasChildren, isOpen, onClick, onToggle }: NodeBoxProps) {
  const { t } = useTheme()
  const [hover, setHover] = useState(false)
  const isRoot = depth === 0

  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        flexShrink: 0,
      }}
    >
      <button
        onClick={onClick}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        title={`點擊向 AI 提問${node.note ? `\n${node.note}` : ''}`}
        style={{
          padding: isRoot ? '9px 16px' : '7px 12px',
          borderRadius: 10,
          border: `1px solid ${hover ? t.accent : isRoot ? t.accentBorder : t.border}`,
          background: hover ? t.accentSoft : isRoot ? t.accentSoft : t.surface2,
          color: t.text,
          fontSize: isRoot ? 14 : 12.5,
          fontWeight: isRoot ? 600 : 500,
          cursor: 'pointer',
          whiteSpace: 'pre-line', // label 內 \n 是後端允許的手動換行
          textAlign: 'left',
          maxWidth: 260,
          fontFamily: 'inherit',
          transition: 'border-color 120ms, background 120ms',
        }}
      >
        {node.label}
      </button>
      {hasChildren && (
        <button
          onClick={onToggle}
          title={isOpen ? '收合分支' : `展開 ${node.children.length} 個子節點`}
          style={{
            minWidth: 22,
            height: 22,
            borderRadius: 999,
            border: `1px solid ${t.border}`,
            background: t.surface,
            color: t.textMuted,
            fontSize: 11,
            fontWeight: 600,
            cursor: 'pointer',
            display: 'grid',
            placeItems: 'center',
            padding: '0 5px',
            flexShrink: 0,
            fontFamily: 'inherit',
          }}
        >
          {isOpen ? '−' : `+${node.children.length}`}
        </button>
      )}
    </div>
  )
}

function toolBtnStyle(t: ReturnType<typeof useTheme>['t']) {
  return {
    padding: '4px 10px',
    borderRadius: 7,
    border: `1px solid ${t.border}`,
    background: t.surface,
    color: t.textMuted,
    fontSize: 11.5,
    fontWeight: 500,
    cursor: 'pointer' as const,
    fontFamily: 'inherit',
  }
}
