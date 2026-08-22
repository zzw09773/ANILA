// Theme tokens — aliases onto apps/shared/tokens.css (DESIGN.md).
// Light is the product default. Screens consume these names.

export const NAMED = {
  ink: '#1B3A6B',
  signal: '#1A5BB8',
  canvas: '#F2F5F9',
  paper: '#FFFFFF',
  mute: '#5A6B7C',
  hairline: '#D7E0EA',
  steady: '#1F7A5C',
  signal40: '#9BB8DC',
  danger: '#B03636',
} as const

export interface ThemeTokens {
  bg: string
  surface: string
  surface2: string
  elevated: string
  border: string
  borderStrong: string
  text: string
  textMuted: string
  textSubtle: string
  accent: string
  accentHover: string
  accentSoft: string
  accentBorder: string
  success: string
  warning: string
  danger: string
  chipBg: string
}

export type ThemeName = 'dark' | 'light'

export const TOKENS: Record<ThemeName, ThemeTokens> = {
  dark: {
    bg: '#142033',
    surface: '#1B2B40',
    surface2: '#1A2838',
    elevated: '#1B2B40',
    border: '#2C3D52',
    borderStrong: '#3A5168',
    text: '#8FB4DE',
    textMuted: '#8A9AAB',
    textSubtle: '#8A9AAB',
    accent: '#5B8FD4',
    accentHover: '#8FB4DE',
    accentSoft: 'rgba(91,143,212,0.14)',
    accentBorder: 'rgba(91,143,212,0.32)',
    success: '#3D9A78',
    warning: '#D6A95F',
    danger: '#D67A82',
    chipBg: '#1A2838',
  },
  light: {
    bg: NAMED.canvas,
    surface: NAMED.paper,
    surface2: '#E8EEF5',
    elevated: NAMED.paper,
    border: NAMED.hairline,
    borderStrong: '#C3D0DD',
    text: NAMED.ink,
    textMuted: NAMED.mute,
    textSubtle: NAMED.mute,
    accent: NAMED.signal,
    accentHover: NAMED.ink,
    accentSoft: 'rgba(26,91,184,0.08)',
    accentBorder: 'rgba(26,91,184,0.28)',
    success: NAMED.steady,
    warning: '#9A6619',
    danger: NAMED.danger,
    chipBg: '#E8EEF5',
  },
}
