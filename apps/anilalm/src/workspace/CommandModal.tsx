import { useEffect, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { useArtifactStore } from '../store/artifacts'
import { Modal } from '../components/Modal'
import { Icon, type IconName } from '../components/Icon'
import { Spinner } from '../components/Spinner'
import {
  generateReport,
  generateSlides,
  generateMindmap,
  generateInfographic,
  generateDatatable,
} from '../studio/generators'
import { createSlidesJob } from '../api/studio'
import { createArtifactTask } from '../api/tasks'
import { explainError } from '../api/client'
import { ThemePicker } from './ThemePicker'
import { StudioWizard } from './StudioWizard'
import type { ThemeId } from '../studio/themes'
import type { SlidesArtifact, StudioArtifact } from '../types'

export interface FormatSpec {
  // 製作台支援的 artifact kind。原本含 podcast / video / flashcards / quiz
  // 等 9 種,但內部部署場景(中科院內網)不需要那 4 種(air-gapped 音/影
  // 模型無解、內網考核 SOP 不交給 AI、抽認卡是消費級個人學習文化),
  // 已從製作台移除。
  k: 'report' | 'slides' | 'mindmap' | 'infographic' | 'datatable'
  l: string
  i: IconName
  c: string
  cat: 'visual' | 'doc'
  hint: string
  comingSoon?: boolean
}

interface CommandModalProps {
  open: boolean
  onClose: () => void
  onGenerated: (a: StudioArtifact) => void
  format: FormatSpec | null
}

interface Preset {
  l: string
  d: string
  tag?: string
}

const PRESETS: Record<string, Preset[]> = {
  report: [
    { l: '深度技術綜述', d: '嚴謹學術風格、含完整章節結構', tag: '推薦' },
    { l: '重點摘要', d: '1-2 頁等量精華筆記，列點為主' },
    { l: '教學講義', d: '概念 + 範例 + 練習題的學習導向格式' },
    { l: '對外溝通文件', d: '客觀中立、適合分享給非技術讀者' },
  ],
  slides: [
    { l: '詳細簡報', d: '完整論證、分頁講清楚 · 12-15 張', tag: '推薦' },
    { l: '口講用短頁', d: '一頁一句，適合口講（TED 節奏）· 5 張' },
  ],
  mindmap: [
    { l: '概念樹', d: '從根概念展開子概念與相關項目', tag: '推薦' },
    { l: '任務拆解', d: 'WBS 結構;任務 → 子任務 → 步驟' },
    { l: 'SOP 流程', d: '線性流程圖,從觸發到完成' },
    { l: '組織關係', d: '人/單位/角色之間的關係圖' },
  ],
  infographic: [
    { l: '任務 Dashboard', d: '關鍵指標 + 進度 + 比較', tag: '推薦' },
    { l: '資料簡報', d: '從資料中擷取具體數字與圖表' },
    { l: '比較矩陣', d: '並排對照 X vs Y 的特徵' },
    { l: '時間軸總覽', d: '從早到晚事件列表 + 視覺強調' },
  ],
  datatable: [
    { l: '關鍵指標彙整', d: '從文件抽 KPI / 數字到表格', tag: '推薦' },
    { l: '實體屬性表', d: '各對象的多欄位屬性對照' },
    { l: '時間軸表', d: '日期 / 事件 / 變化 三欄' },
    { l: '並排比較', d: '對照 X vs Y(同欄位橫向比較)' },
  ],
}

// Preset 中文 label → backend enum 對應(送 backend 時用)
const PRESET_ENUM_MAP: Record<string, Record<string, string>> = {
  report: {
    深度技術綜述: 'deep_tech_review',
    重點摘要: 'key_summary',
    教學講義: 'teaching_handout',
    對外溝通文件: 'external_comms',
  },
  mindmap: {
    概念樹: 'concept_tree',
    任務拆解: 'task_breakdown',
    SOP流程: 'sop_flow',
    'SOP 流程': 'sop_flow',
    組織關係: 'org_relationships',
  },
  infographic: {
    '任務 Dashboard': 'mission_dashboard',
    任務Dashboard: 'mission_dashboard',
    資料簡報: 'stats_brief',
    比較矩陣: 'comparison_matrix',
    時間軸總覽: 'timeline_overview',
  },
  datatable: {
    關鍵指標彙整: 'key_figures',
    實體屬性表: 'entity_attributes',
    時間軸表: 'timeline_table',
    並排比較: 'comparison_table',
  },
}

export function presetEnumFor(kind: string, label: string): string {
  return PRESET_ENUM_MAP[kind]?.[label] ?? label
}

const AUDIENCES = ['院內同仁', '主管', '對外'] as const
const NO_INDEXED = '先上傳或等索引完成'

export function CommandModal({ open, onClose, onGenerated, format }: CommandModalProps) {
  const { t } = useTheme()
  const collection = useWorkspaceStore((s) => s.collection)
  const docs = useWorkspaceStore((s) => s.docs)
  const indexedDocs = docs.filter((d) => d.doc.status === 'indexed').map((d) => d.doc)

  const [step, setStep] = useState(0)
  const [selected, setSelected] = useState(0)
  const [themeId, setThemeId] = useState<ThemeId>('auto')
  const [extra, setExtra] = useState('')
  const [selectedDocIds, setSelectedDocIds] = useState<number[]>([])
  const [audience, setAudience] = useState<(typeof AUDIENCES)[number]>('院內同仁')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Wizard (Phase B) vs picker (Phase A) for the theme step. Default to
  // wizard so first-time users get the lowest-friction guided flow;
  // remember the user's last choice across sessions.
  const [mode, setMode] = useState<'wizard' | 'picker'>(
    () => (localStorage.getItem('studio.theme-mode') as 'wizard' | 'picker') ?? 'wizard'
  )

  useEffect(() => {
    localStorage.setItem('studio.theme-mode', mode)
  }, [mode])

  useEffect(() => {
    if (!open) return
    setSelectedDocIds(
      docs.filter((d) => d.doc.status === 'indexed').map((d) => d.doc.id),
    )
    setAudience('院內同仁')
  }, [open, format?.k, docs])

  const presets = format ? PRESETS[format.k] ?? [] : []
  // 5 種 artifact 都已實作(report v2 backend + slides + mindmap + infographic + datatable)
  const isSupported = format?.k === 'report' || format?.k === 'slides'
    || format?.k === 'mindmap' || format?.k === 'infographic' || format?.k === 'datatable'
  // 只有 slides 有「視覺主題」概念,需要多一個 theme picker step。
  // 其他 4 種 artifact 沒視覺主題,直接 preset → 補充指示(2 steps)。
  const isSlides = format?.k === 'slides'
  const totalSteps = isSlides ? 3 : 2
  const extraStep = isSlides ? 2 : 1
  const themeStep = 1

  const reset = () => {
    setStep(0)
    setSelected(0)
    setThemeId('auto')
    setExtra('')
    setSelectedDocIds(indexedDocs.map((d) => d.id))
    setAudience('院內同仁')
    setErr(null)
  }

  // Wizard finished: adopt the recommended theme, fold the (≤2) implicit
  // B.4 instructions into whatever the user already typed without
  // clobbering it, then advance to the supplementary-instructions step.
  const onWizardComplete = (id: ThemeId, wizardExtra: string[]) => {
    setThemeId(id)
    if (wizardExtra.length > 0) {
      setExtra((prev) => {
        const existing = prev.trim()
        // Skip lines that are already present so re-running the wizard
        // doesn't duplicate them.
        const additions = wizardExtra.filter((line) => !existing.includes(line))
        if (additions.length === 0) return prev
        return existing ? `${existing}\n${additions.join('\n')}` : additions.join('\n')
      })
    }
    setStep(extraStep)
  }

  const close = () => {
    if (busy) return
    reset()
    onClose()
  }

  const submit = async () => {
    if (!format || !collection || !isSupported) return
    const picked = isSlides
      ? indexedDocs.filter((d) => selectedDocIds.includes(d.id))
      : indexedDocs
    if (picked.length === 0) {
      setErr(NO_INDEXED)
      return
    }
    setBusy(true)
    setErr(null)
    try {
      const presetName = presets[selected]?.l ?? '預設'
      if (format.k === 'report') {
        // Report v2 是 backend job(同 mindmap / infographic / datatable
        // pattern):POST /api/reports/jobs → pending → WSStudio polling 接手。
        const artifact = await generateReport({
          collection,
          docs: indexedDocs,
          preset: presetName,
          extraInstructions: extra.trim() || undefined,
        })
        onGenerated(artifact)
        reset()
        onClose()
      } else if (format.k === 'mindmap') {
        const artifact = await generateMindmap({
          collection,
          docs: indexedDocs,
          preset: presetName,
          extraInstructions: extra.trim() || undefined,
        })
        onGenerated(artifact)
        reset()
        onClose()
      } else if (format.k === 'infographic') {
        const artifact = await generateInfographic({
          collection,
          docs: indexedDocs,
          preset: presetName,
          extraInstructions: extra.trim() || undefined,
        })
        onGenerated(artifact)
        reset()
        onClose()
      } else if (format.k === 'datatable') {
        const artifact = await generateDatatable({
          collection,
          docs: indexedDocs,
          preset: presetName,
          extraInstructions: extra.trim() || undefined,
        })
        onGenerated(artifact)
        reset()
        onClose()
      } else {
        // Slides path is async (job-based). The flow:
        //   1. POST /api/studio/slides/jobs returns in <50 ms with a
        //      JobStatus(state="pending", job_id="j_...").
        //   2. We immediately drop a "pending" artifact into the store
        //      and close the modal — the user can continue chatting
        //      while the pipeline runs (60-180 s).
        //   3. WSStudio's polling effect (driven by the artifact's
        //      `state === 'pending'`) drives transitions to
        //      "done" / "failed" and triggers the .pptx download once
        //      it's ready.
        //
        // No download or completion handling lives in this modal
        // anymore — that's strictly WSStudio's responsibility now.
        // Slice 8b: bind a CSP Task before launching (degrades to null).
        const binding = await createArtifactTask({
          title: `${collection.name} · 簡報`,
          outputType: 'slides',
          collectionIds: [collection.id],
        })
        const job = await createSlidesJob({
          collectionId: collection.id,
          preset: presetName,
          extraInstructions: extra.trim() || undefined,
          documentIds: picked.map((d) => d.id),
          audience,
          themeOverride: themeId === 'auto' ? undefined : themeId,
          binding: binding ?? undefined,
        })
        const artifact: SlidesArtifact = {
          id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
          kind: 'slides',
          collectionId: collection.id,
          // Record the chosen theme so the sidebar can badge the deck.
          // undefined for auto → no badge (matches backend auto path).
          theme: themeId === 'auto' ? undefined : themeId,
          // Title and slide_count aren't known yet; the polling effect
          // will fill these in as soon as the LLM finishes step 4-5.
          // Use a placeholder so the timeline card has something to
          // render until then.
          title: '產出中…',
          preset: presetName,
          slides: [],
          sourceCount: picked.length,
          createdAt: new Date().toISOString(),
          state: 'pending',
          jobId: job.job_id,
          step: job.step,
        }
        useArtifactStore.getState().add(artifact)
        onGenerated(artifact)
        reset()
        onClose()
        // Keep the legacy generateSlides import alive for any caller
        // that still wants the client-only JSON path; lint would
        // otherwise drop it.
        void generateSlides
      }
    } catch (e) {
      setErr(explainError(e))
    } finally {
      setBusy(false)
    }
  }

  if (!format) return null

  return (
    <Modal open={open} onClose={close} width={560}>
      {/* Header */}
      <div
        style={{
          padding: '14px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          borderBottom: `1px solid ${t.border}`,
        }}
      >
        <div
          style={{
            width: 26,
            height: 26,
            borderRadius: 7,
            background: `${format.c}22`,
            display: 'grid',
            placeItems: 'center',
            border: `1px solid ${format.c}33`,
          }}
        >
          <Icon name={format.i} size={12} stroke={format.c} />
        </div>
        <div style={{ fontSize: 13, fontWeight: 500, color: t.text }}>建立 {format.l}</div>
        {isSupported && (
          <span style={{ fontSize: 11, color: t.textSubtle }}>· 步驟 {step + 1} / {totalSteps}</span>
        )}
        <button
          onClick={close}
          disabled={busy}
          style={{
            marginLeft: 'auto',
            width: 24,
            height: 24,
            borderRadius: 6,
            border: 'none',
            background: 'transparent',
            cursor: busy ? 'not-allowed' : 'pointer',
            display: 'grid',
            placeItems: 'center',
            color: t.textMuted,
          }}
        >
          <Icon name="x" size={13} stroke={t.textMuted} />
        </button>
      </div>

      <div style={{ flex: 1, overflow: 'auto', padding: '18px 16px 8px' }}>
        {!isSupported ? (
          <div
            style={{
              padding: 22,
              borderRadius: 12,
              background: t.surface2,
              border: `1px solid ${t.border}`,
              textAlign: 'center',
              color: t.textMuted,
            }}
          >
            <Icon name="sparkle" size={32} stroke={t.accent} />
            <div style={{ fontSize: 14, fontWeight: 500, marginTop: 10, color: t.text }}>
              {format.l} 尚未開放
            </div>
            <div style={{ fontSize: 12, marginTop: 6 }}>
              此輸出類型尚未開放;待後端對應 endpoint 就緒後解鎖。
            </div>
          </div>
        ) : step === 0 ? (
          <>
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: t.textMuted,
                textTransform: 'uppercase',
                letterSpacing: 1,
                marginBottom: 10,
              }}
            >
              01 · 選擇風格
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {presets.map((p, i) => (
                <button
                  key={p.l}
                  onClick={() => setSelected(i)}
                  style={{
                    textAlign: 'left',
                    padding: '12px 14px',
                    borderRadius: 10,
                    cursor: 'pointer',
                    background: selected === i ? t.accentSoft : t.surface2,
                    border: `1px solid ${selected === i ? t.accentBorder : t.border}`,
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 11,
                    fontFamily: 'inherit',
                  }}
                >
                  <div
                    style={{
                      width: 16,
                      height: 16,
                      borderRadius: '50%',
                      flexShrink: 0,
                      marginTop: 2,
                      background: selected === i ? t.accent : 'transparent',
                      border: `1.5px solid ${selected === i ? t.accent : t.borderStrong}`,
                      display: 'grid',
                      placeItems: 'center',
                    }}
                  >
                    {selected === i && (
                      <div
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: '50%',
                          background: '#fff',
                        }}
                      />
                    )}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 7,
                        marginBottom: 3,
                      }}
                    >
                      <span style={{ fontSize: 13, fontWeight: 500, color: t.text }}>{p.l}</span>
                      {p.tag && (
                        <span
                          style={{
                            fontSize: 9.5,
                            fontWeight: 600,
                            color: t.accent,
                            padding: '1px 6px',
                            background: t.accentSoft,
                            borderRadius: 4,
                            letterSpacing: 0.4,
                            border: `1px solid ${t.accentBorder}`,
                          }}
                        >
                          {p.tag.toUpperCase()}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: 11.5, color: t.textMuted, lineHeight: 1.5 }}>
                      {p.d}
                    </div>
                  </div>
                </button>
              ))}
            </div>
          </>
        ) : isSlides && step === themeStep ? (
          <>
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: t.textMuted,
                textTransform: 'uppercase',
                letterSpacing: 1,
                marginBottom: 10,
              }}
            >
              02 · 選擇視覺風格
            </div>
            {/* Wizard / picker mode toggle */}
            <div
              style={{
                display: 'inline-flex',
                gap: 2,
                padding: 2,
                borderRadius: 8,
                background: t.surface2,
                border: `1px solid ${t.border}`,
                marginBottom: 12,
              }}
            >
              {(['wizard', 'picker'] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  aria-pressed={mode === m}
                  style={{
                    padding: '5px 12px',
                    borderRadius: 6,
                    border: 'none',
                    background: mode === m ? t.accent : 'transparent',
                    color: mode === m ? '#fff' : t.textMuted,
                    fontSize: 12,
                    fontWeight: 500,
                    cursor: 'pointer',
                    fontFamily: 'inherit',
                  }}
                >
                  {m === 'wizard' ? '精靈模式' : '進階模式'}
                </button>
              ))}
            </div>
            {mode === 'wizard' ? (
              <StudioWizard
                onComplete={onWizardComplete}
                onManualPick={() => setMode('picker')}
              />
            ) : (
              <>
                <div style={{ fontSize: 11.5, color: t.textMuted, lineHeight: 1.55, marginBottom: 12 }}>
                  選「自動偵測」由系統依文件 title 與內容判斷；或直接挑一個你想要的風格。
                </div>
                <ThemePicker selected={themeId} onSelect={setThemeId} />
              </>
            )}
          </>
        ) : (
          <>
            <div
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: t.textMuted,
                textTransform: 'uppercase',
                letterSpacing: 1,
                marginBottom: 10,
              }}
            >
              {isSlides ? '03' : '02'} · {isSlides ? '來源、聽眾與補充指示' : '補充指示（可略過）'}
            </div>
            {isSlides && (
              <div style={{ marginBottom: 14 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: t.text, marginBottom: 8 }}>
                  要用哪些來源
                </div>
                {indexedDocs.length === 0 ? (
                  <div className="yuan-card" style={{ padding: 14, fontSize: 13, color: t.textMuted }}>
                    {NO_INDEXED}
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {indexedDocs.map((doc) => {
                      const on = selectedDocIds.includes(doc.id)
                      return (
                        <button
                          key={doc.id}
                          type="button"
                          onClick={() =>
                            setSelectedDocIds((prev) =>
                              on ? prev.filter((id) => id !== doc.id) : [...prev, doc.id],
                            )
                          }
                          style={{
                            textAlign: 'left',
                            padding: '8px 12px',
                            borderRadius: 8,
                            border: `1px solid ${on ? t.accentBorder : t.border}`,
                            background: on ? t.accentSoft : t.surface2,
                            color: t.text,
                            cursor: 'pointer',
                            fontFamily: 'inherit',
                            fontSize: 12.5,
                          }}
                        >
                          {on ? '已選 · ' : ''}
                          {doc.filename}
                        </button>
                      )
                    })}
                  </div>
                )}
                <div style={{ fontSize: 12, fontWeight: 600, color: t.text, margin: '14px 0 8px' }}>
                  聽眾
                </div>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
                  {AUDIENCES.map((item) => (
                    <button
                      key={item}
                      type="button"
                      onClick={() => setAudience(item)}
                      style={{
                        padding: '5px 10px',
                        borderRadius: 8,
                        border: `1px solid ${audience === item ? t.accentBorder : t.border}`,
                        background: audience === item ? t.accentSoft : t.surface2,
                        color: t.text,
                        cursor: 'pointer',
                        fontFamily: 'inherit',
                        fontSize: 12,
                      }}
                    >
                      {item}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <textarea
              value={extra}
              onChange={(e) => setExtra(e.target.value)}
              placeholder="例：聚焦在第 4 節的數學評估、避免提及商業競爭..."
              rows={5}
              disabled={busy}
              style={{
                width: '100%',
                padding: 13,
                borderRadius: 10,
                background: t.surface2,
                border: `1px solid ${t.border}`,
                color: t.text,
                fontSize: 13,
                fontFamily: 'inherit',
                lineHeight: 1.55,
                outline: 'none',
                resize: 'vertical',
              }}
            />
            <div
              style={{
                marginTop: 14,
                padding: '11px 13px',
                borderRadius: 10,
                background: t.accentSoft,
                border: `1px solid ${t.accentBorder}`,
                fontSize: 11.5,
                color: t.text,
                lineHeight: 1.55,
                display: 'flex',
                gap: 9,
                alignItems: 'flex-start',
              }}
            >
              <Icon
                name="sparkle"
                size={13}
                stroke={t.accent}
                style={{ marginTop: 1, flexShrink: 0 }}
              />
              <div>
                <div style={{ fontWeight: 500, marginBottom: 2 }}>
                  已選擇：{presets[selected]?.l ?? '—'}
                </div>
                <div style={{ color: t.textMuted }}>
                  {isSlides
                    ? `${selectedDocIds.length} 份已選來源 · ${audience}`
                    : `${indexedDocs.length} 份已索引文件`}
                  {format?.k === 'slides'
                    ? ' · 簡報在背景製作，完成後可預覽、重做單頁、下載可編輯 PPTX'
                    : ' · 預估 30-90 秒'}
                </div>
              </div>
            </div>
          </>
        )}

        {err && (
          <div
            role="alert"
            style={{
              marginTop: 14,
              padding: '10px 12px',
              borderRadius: 9,
              background: `${t.danger}22`,
              color: t.danger,
              fontSize: 12.5,
              border: `1px solid ${t.danger}33`,
            }}
          >
            {err}
          </div>
        )}
      </div>

      {/* Footer */}
      <div
        style={{
          padding: '12px 16px',
          borderTop: `1px solid ${t.border}`,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        {isSupported ? (
          <>
            <div style={{ display: 'flex', gap: 4 }}>
              {Array.from({ length: totalSteps }, (_, i) => (
                <div
                  key={i}
                  style={{
                    width: step >= i ? 18 : 6,
                    height: 4,
                    borderRadius: 2,
                    background: step >= i ? t.accent : t.border,
                    transition: 'all 200ms',
                  }}
                />
              ))}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              {step > 0 && (
                <button
                  onClick={() => setStep(step - 1)}
                  disabled={busy}
                  style={{
                    padding: '7px 14px',
                    borderRadius: 8,
                    border: `1px solid ${t.border}`,
                    background: t.surface,
                    color: t.text,
                    fontSize: 12.5,
                    fontWeight: 500,
                    cursor: busy ? 'not-allowed' : 'pointer',
                    fontFamily: 'inherit',
                  }}
                >
                  上一步
                </button>
              )}
              {/* Hide the generic "繼續" on the theme step while the wizard
                  is active — the wizard drives its own advancement via
                  onComplete. The picker still needs this button. */}
              {step < extraStep && !(isSlides && step === themeStep && mode === 'wizard') && (
                <button
                  onClick={() => setStep(step + 1)}
                  style={{
                    padding: '7px 16px',
                    borderRadius: 8,
                    border: 'none',
                    background: t.accent,
                    color: '#fff',
                    fontSize: 12.5,
                    fontWeight: 500,
                    cursor: 'pointer',
                    fontFamily: 'inherit',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    boxShadow: `0 4px 14px -4px ${t.accent}`,
                  }}
                >
                  繼續 <Icon name="arrowR" size={11} stroke="#fff" />
                </button>
              )}
              {step === extraStep && (
                <button
                  onClick={() => void submit()}
                  disabled={
                    busy ||
                    indexedDocs.length === 0 ||
                    (isSlides && selectedDocIds.length === 0)
                  }
                  title={
                    indexedDocs.length === 0 || (isSlides && selectedDocIds.length === 0)
                      ? NO_INDEXED
                      : ''
                  }
                  style={{
                    padding: '7px 16px',
                    borderRadius: 8,
                    border: 'none',
                    background: t.accent,
                    color: '#fff',
                    fontSize: 12.5,
                    fontWeight: 500,
                    cursor: busy
                      ? 'wait'
                      : indexedDocs.length === 0 || (isSlides && selectedDocIds.length === 0)
                        ? 'not-allowed'
                        : 'pointer',
                    fontFamily: 'inherit',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    boxShadow: `0 4px 14px -4px ${t.accent}`,
                    opacity:
                      busy ||
                      indexedDocs.length === 0 ||
                      (isSlides && selectedDocIds.length === 0)
                        ? 0.55
                        : 1,
                  }}
                >
                  {busy ? <Spinner size={11} color="#fff" /> : <Icon name="sparkle" size={11} stroke="#fff" />}
                  {busy ? '製作中…' : '開始製作'}
                </button>
              )}
            </div>
          </>
        ) : (
          <button
            onClick={close}
            style={{
              marginLeft: 'auto',
              padding: '7px 16px',
              borderRadius: 8,
              border: 'none',
              background: t.accent,
              color: '#fff',
              fontSize: 12.5,
              fontWeight: 500,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            知道了
          </button>
        )}
      </div>
    </Modal>
  )
}
