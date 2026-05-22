// Wizard Phase B — scenario → theme mapping.
//
// Phase A let users who KNOW which theme they want pick a card directly
// (ThemePicker). Phase B is for users who DON'T: instead of asking about
// aesthetics ("do you want navy or warm?"), we ask about the concrete
// occasion (who's the audience / what tone / what format / how long) and
// recommend a theme. The user can accept the recommendation or fall back
// to the Phase A picker.
//
// recommendTheme() is a pure function so the recommendation is trivially
// testable and has no UI / React dependency.

import type { ThemeId } from './themes'

export type Audience = 'colleagues' | 'manager' | 'client' | 'investor' | 'academic' | 'self'
export type Tone = 'strict' | 'neutral' | 'reflective' | 'persuasive' | 'minimal'
export type Format = 'tech-share' | 'business-analysis' | 'learning-log' | 'pitch' | 'briefing' | 'research'
export type Length = 'compact' | 'standard' | 'extended'

export interface WizardAnswers {
  audience: Audience
  tone: Tone
  format: Format
  length: Length
}

/**
 * Map wizard answers to a recommended theme.
 *
 * Priority logic (first match wins):
 * 1. format='learning-log' OR tone='reflective'     → warm_journal
 * 2. format='research' OR audience='academic'       → academic_paper
 * 3. audience='investor' OR format='pitch'          → startup_pitch
 * 4. audience='client' AND length='compact'         → executive_brief
 * 5. audience='manager' AND tone='minimal'          → executive_brief
 * 6. default                                        → corporate_navy
 */
export function recommendTheme(answers: WizardAnswers): ThemeId {
  if (answers.format === 'learning-log' || answers.tone === 'reflective') {
    return 'warm_journal'
  }
  if (answers.format === 'research' || answers.audience === 'academic') {
    return 'academic_paper'
  }
  if (answers.audience === 'investor' || answers.format === 'pitch') {
    return 'startup_pitch'
  }
  if (answers.audience === 'client' && answers.length === 'compact') {
    return 'executive_brief'
  }
  if (answers.audience === 'manager' && answers.tone === 'minimal') {
    return 'executive_brief'
  }
  return 'corporate_navy'
}
