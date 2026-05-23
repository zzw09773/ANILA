import { useEffect, useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { useWorkspaceStore } from '../store/workspace'
import { useArtifactStore } from '../store/artifacts'
import { Modal } from '../components/Modal'
import { Icon, type IconName } from '../components/Icon'
import { Spinner } from '../components/Spinner'
import { generateReport, generateSlides } from '../studio/generators'
import { createSlidesJob } from '../api/studio'
import { explainError } from '../api/client'
import { ThemePicker } from './ThemePicker'
import { StudioWizard } from './StudioWizard'
import type { ThemeId } from '../studio/themes'
import type { SlidesArtifact, StudioArtifact } from '../types'

export interface FormatSpec {
  // 製作台支援的 artifact kind。原本含 podcast / video / flashcards / quiz
  // 等 9 種,但內部部署場景(國軍 / 中科院)不需要那 4 種(air-gapped 音/影
  // 模型無解、軍方考核 SOP 不交給 AI、抽認卡是消費級個人學習文化),
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
    { l: '經典報告結構', d: '封面 → 大綱 → 內容 → 結論 · 12-15 張', tag: '推薦' },
    { l: 'Lightning Talk', d: '5 張投影片濃縮版' },
    { l: '教學投影片', d: '概念 + 範例 + 練習' },
  ],
}

export function CommandModal({ open, onClose, onGenerated, format }: CommandModalProps) {
  const { t } = useTheme()
  const collection = useWorkspaceStore((s) => s.collection)
  const docs = useWorkspaceStore((s) => s.docs)
  const indexedDocs = docs.filter((d) => d.doc.status === 'indexed').map((d) => d.doc)

  const [step, setStep] = useState(0)
  const [selected, setSelected] = useState(0)
  const [themeId, setThemeId] = useState<ThemeId>('auto')
  const [extra, setExtra] = useState('')
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

  const presets = format ? PRESETS[format.k] ?? [] : []
  const isSupported = format?.k === 'report' || format?.k === 'slides'
  // Slides gets an extra "theme picker" step between preset and extra
  // instructions, so its flow has 3 steps (0=preset, 1=theme, 2=extra).
  // Report has no visual theme concept → 2 steps (0=preset, 1=extra).
  const isSlides = format?.k === 'slides'
  const totalSteps = isSlides ? 3 : 2
  // The "補充指示" textarea is always the final step; the theme step is
  // only the middle step on the slides path.
  const extraStep = isSlides ? 2 : 1
  const themeStep = 1

  const reset = () => {
    setStep(0)
    setSelected(0)
    setThemeId('auto')
    setExtra('')
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
    setBusy(true)
    setErr(null)
    try {
      const presetName = presets[selected]?.l ?? '預設'
      if (format.k === 'report') {
        // Report path is sync: LLM emits Markdown, generateReport
        // already adds the artifact to the store and resolves once
        // generation completes. We can keep this awaited — the modal
        // already shows a spinner while the user waits 30-90 s for
        // markdown.
        const artifact = await generateReport({
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
        const job = await createSlidesJob({
          collectionId: collection.id,
          preset: presetName,
          extraInstructions: extra.trim() || undefined,
          themeOverride: themeId === 'auto' ? undefined : themeId,
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
          title: '鑄造中…',
          preset: presetName,
          slides: [],
          sourceCount: indexedDocs.length,
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
              {format.l} 還在路上
            </div>
            <div style={{ fontSize: 12, marginTop: 6 }}>
              MVP 只支援「深度報告」與「簡報」兩種輸出。其餘類型會在後端對應 endpoint
              （TTS / 影片合成等）就緒後解鎖。
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
              {isSlides ? '03' : '02'} · 補充指示（可略過）
            </div>
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
                  {indexedDocs.length} 份已索引文件
                  {format?.k === 'slides'
                    ? ' · 簡報走後端 pipeline（檢索 → LLM → 渲染 → 視覺檢查），需 60-120 秒'
                    : ' · 預估 30-90 秒'}
                </div>
              </div>
            </div>
          </>
        )}

        {err && (
          <div
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
                  disabled={busy}
                  style={{
                    padding: '7px 16px',
                    borderRadius: 8,
                    border: 'none',
                    background: t.accent,
                    color: '#fff',
                    fontSize: 12.5,
                    fontWeight: 500,
                    cursor: busy ? 'wait' : 'pointer',
                    fontFamily: 'inherit',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: 6,
                    boxShadow: `0 4px 14px -4px ${t.accent}`,
                    opacity: busy ? 0.7 : 1,
                  }}
                >
                  {busy ? <Spinner size={11} color="#fff" /> : <Icon name="sparkle" size={11} stroke="#fff" />}
                  {busy ? '生成中...' : '開始鑄造'}
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
