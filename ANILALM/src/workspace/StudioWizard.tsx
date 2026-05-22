import { useState } from 'react'
import { useTheme } from '../theme/ThemeContext'
import { Icon } from '../components/Icon'
import { findTheme, type ThemeId } from '../studio/themes'
import {
  recommendTheme,
  type Audience,
  type Tone,
  type Format,
  type Length,
  type WizardAnswers,
} from '../studio/themeMapping'

interface Props {
  // Called when the user accepts the recommended theme. `extra` carries
  // up to two implicit extra_instructions derived from the answers (B.4);
  // CommandModal appends these to whatever the user has already typed,
  // never overwriting. Empty array when nothing to add.
  onComplete: (themeId: ThemeId, extra: string[]) => void
  // Called when the user wants to leave the wizard and pick a theme by
  // hand → CommandModal switches to 'picker' mode (Phase A ThemePicker).
  onManualPick: () => void
}

// Each question is one step. Labels are zh-TW; values map 1:1 to the
// themeMapping union types.
interface Option<V extends string> {
  v: V
  l: string
}

const AUDIENCE_OPTS: Option<Audience>[] = [
  { v: 'colleagues', l: '同事' },
  { v: 'manager', l: '主管' },
  { v: 'client', l: '客戶' },
  { v: 'investor', l: '投資人' },
  { v: 'academic', l: '學術同行' },
  { v: 'self', l: '自己（私人筆記）' },
]

const TONE_OPTS: Option<Tone>[] = [
  { v: 'strict', l: '嚴謹' },
  { v: 'neutral', l: '中性' },
  { v: 'reflective', l: '反思' },
  { v: 'persuasive', l: '說服' },
  { v: 'minimal', l: '極簡' },
]

const FORMAT_OPTS: Option<Format>[] = [
  { v: 'tech-share', l: '技術分享' },
  { v: 'business-analysis', l: '業務分析' },
  { v: 'learning-log', l: '學習紀錄' },
  { v: 'pitch', l: '對外發表' },
  { v: 'briefing', l: '簡報 Briefing' },
  { v: 'research', l: '研究報告' },
]

const LENGTH_OPTS: Option<Length>[] = [
  { v: 'compact', l: '精簡' },
  { v: 'standard', l: '標準' },
  { v: 'extended', l: '詳盡' },
]

// B.4 — derive implicit extra_instructions from answers, max two, to
// avoid over-stuffing the prompt. Order = priority; we slice(0, 2).
function deriveInstructions(answers: WizardAnswers): string[] {
  const out: string[] = []
  if (answers.audience === 'client') {
    out.push('避免內部技術術語、加上業務價值說明')
  }
  if (answers.audience === 'investor') {
    out.push('突出 ROI / TAM / 成果數據')
  }
  if (answers.length === 'compact') {
    out.push('簡報以 ≤ 10 張為目標')
  }
  return out.slice(0, 2)
}

interface Question {
  key: keyof WizardAnswers
  title: string
  hint: string
  // Erased to a generic Option[]; the value still matches the union at the
  // setAnswers call site below.
  opts: Option<string>[]
}

const QUESTIONS: Question[] = [
  { key: 'audience', title: '這份簡報主要給誰看？', hint: '讀者決定了語氣與深度。', opts: AUDIENCE_OPTS },
  { key: 'tone', title: '你想要什麼調性？', hint: '正式報告或軟性分享，氣質很不一樣。', opts: TONE_OPTS },
  { key: 'format', title: '這比較像哪一種內容？', hint: '依內容性質挑最接近的格式。', opts: FORMAT_OPTS },
  { key: 'length', title: '預期篇幅？', hint: '精簡偏結論導向、詳盡含完整脈絡。', opts: LENGTH_OPTS },
]

const TOTAL = QUESTIONS.length // 4 question steps; index === TOTAL is the result page

export function StudioWizard({ onComplete, onManualPick }: Props) {
  const { t } = useTheme()
  const [stepIdx, setStepIdx] = useState(0)
  const [answers, setAnswers] = useState<Partial<WizardAnswers>>({})

  const onResult = stepIdx >= TOTAL
  const current = onResult ? null : QUESTIONS[stepIdx]
  const currentValue = current ? answers[current.key] : undefined

  const setAnswer = (key: keyof WizardAnswers, v: string) => {
    setAnswers((prev) => ({ ...prev, [key]: v }))
  }

  const allAnswered = QUESTIONS.every((q) => answers[q.key] != null)

  const labelStyle = {
    fontSize: 11,
    fontWeight: 600 as const,
    color: t.textMuted,
    textTransform: 'uppercase' as const,
    letterSpacing: 1,
    marginBottom: 10,
  }

  // -------- Result page --------
  if (onResult && allAnswered) {
    const full = answers as WizardAnswers
    const recommended = recommendTheme(full)
    const theme = findTheme(recommended)
    const extra = deriveInstructions(full)
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
        <div style={labelStyle}>推薦結果</div>
        <div
          style={{
            padding: 16,
            borderRadius: 12,
            background: t.surface2,
            border: `1px solid ${t.border}`,
            display: 'flex',
            flexDirection: 'column',
            gap: 12,
          }}
        >
          <div style={{ fontSize: 13.5, color: t.text, lineHeight: 1.6 }}>
            依據你的選擇，建議使用「
            <span style={{ fontWeight: 600, color: t.accent }}>{theme.name}</span>
            」風格。
          </div>
          <div style={{ fontSize: 11.5, color: t.textMuted, lineHeight: 1.55 }}>
            {theme.description}
          </div>
          {/* Swatch colour preview */}
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            {(['bar', 'accent', 'ink', 'bg'] as const).map((k) => (
              <div
                key={k}
                title={k}
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: 7,
                  background: theme.swatches[k],
                  border: `1px solid ${t.border}`,
                }}
              />
            ))}
            <div style={{ fontSize: 10.5, color: t.textSubtle, marginLeft: 4 }}>配色預覽</div>
          </div>
          {extra.length > 0 && (
            <div
              style={{
                fontSize: 11,
                color: t.textMuted,
                lineHeight: 1.55,
                paddingTop: 4,
                borderTop: `1px dashed ${t.border}`,
              }}
            >
              <span style={{ fontWeight: 500, color: t.text }}>自動補充指示：</span>
              {extra.join('；')}
            </div>
          )}
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            onClick={() => onComplete(recommended, extra)}
            style={{
              flex: 1,
              minWidth: 140,
              padding: '9px 16px',
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
              justifyContent: 'center',
              gap: 6,
            }}
          >
            <Icon name="check" size={12} stroke="#fff" /> 採用這個風格
          </button>
          <button
            type="button"
            onClick={onManualPick}
            style={{
              padding: '9px 16px',
              borderRadius: 8,
              border: `1px solid ${t.border}`,
              background: t.surface,
              color: t.text,
              fontSize: 12.5,
              fontWeight: 500,
              cursor: 'pointer',
              fontFamily: 'inherit',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
            }}
          >
            <Icon name="edit" size={12} stroke={t.textMuted} /> 我要手動選
          </button>
        </div>

        <button
          type="button"
          onClick={() => setStepIdx(TOTAL - 1)}
          style={{
            alignSelf: 'flex-start',
            padding: '4px 2px',
            border: 'none',
            background: 'transparent',
            color: t.textSubtle,
            fontSize: 11.5,
            cursor: 'pointer',
            fontFamily: 'inherit',
          }}
        >
          ← 重新調整答案
        </button>
      </div>
    )
  }

  // -------- Question page --------
  if (!current) return null
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={labelStyle}>精靈 · 問題 {stepIdx + 1} / {TOTAL}</div>
      <div>
        <div style={{ fontSize: 14, fontWeight: 500, color: t.text, marginBottom: 4 }}>
          {current.title}
        </div>
        <div style={{ fontSize: 11.5, color: t.textMuted, lineHeight: 1.55 }}>{current.hint}</div>
      </div>

      <select
        value={(currentValue as string) ?? ''}
        onChange={(e) => setAnswer(current.key, e.target.value)}
        style={{
          width: '100%',
          padding: '11px 12px',
          borderRadius: 10,
          background: t.surface2,
          border: `1px solid ${t.border}`,
          color: t.text,
          fontSize: 13,
          fontFamily: 'inherit',
          outline: 'none',
          cursor: 'pointer',
        }}
      >
        <option value="" disabled>
          請選擇…
        </option>
        {current.opts.map((o) => (
          <option key={o.v} value={o.v}>
            {o.l}
          </option>
        ))}
      </select>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
        {stepIdx > 0 ? (
          <button
            type="button"
            onClick={() => setStepIdx(stepIdx - 1)}
            style={{
              padding: '7px 14px',
              borderRadius: 8,
              border: `1px solid ${t.border}`,
              background: t.surface,
              color: t.text,
              fontSize: 12.5,
              fontWeight: 500,
              cursor: 'pointer',
              fontFamily: 'inherit',
            }}
          >
            上一題
          </button>
        ) : (
          <span />
        )}

        <button
          type="button"
          disabled={currentValue == null}
          onClick={() => setStepIdx(stepIdx + 1)}
          style={{
            padding: '7px 16px',
            borderRadius: 8,
            border: 'none',
            background: currentValue == null ? t.border : t.accent,
            color: currentValue == null ? t.textSubtle : '#fff',
            fontSize: 12.5,
            fontWeight: 500,
            cursor: currentValue == null ? 'not-allowed' : 'pointer',
            fontFamily: 'inherit',
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          {stepIdx === TOTAL - 1 ? '看推薦結果' : '下一題'}
          <Icon name="arrowR" size={11} stroke={currentValue == null ? t.textSubtle : '#fff'} />
        </button>
      </div>

      <button
        type="button"
        onClick={onManualPick}
        style={{
          alignSelf: 'flex-start',
          padding: '4px 2px',
          border: 'none',
          background: 'transparent',
          color: t.textSubtle,
          fontSize: 11.5,
          cursor: 'pointer',
          fontFamily: 'inherit',
        }}
      >
        我已經知道想要哪個風格 → 直接手動選
      </button>
    </div>
  )
}
