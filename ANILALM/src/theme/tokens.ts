// Theme tokens — direct port of the prototype's design language.
// Two themes: dark (default) and light. All colors live here so screens
// only consume tokens, never literal hex values.

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
    bg: '#0B0D10',
    surface: '#13161B',
    surface2: '#191D24',
    elevated: '#20242C',
    border: '#262B34',
    borderStrong: '#323844',
    text: '#E8EAED',
    textMuted: '#9AA3AE',
    textSubtle: '#6B7280',
    accent: '#7C7BFF',
    accentHover: '#8E8DFF',
    accentSoft: 'rgba(124,123,255,0.14)',
    accentBorder: 'rgba(124,123,255,0.32)',
    success: '#3DD68C',
    warning: '#F4B740',
    danger: '#FF6B6B',
    chipBg: '#1A1F27',
  },
  light: {
    bg: '#FAFAF7',
    surface: '#FFFFFF',
    surface2: '#F5F5F0',
    elevated: '#FFFFFF',
    border: '#E8E6DF',
    borderStrong: '#D4D2CB',
    text: '#1A1A1A',
    textMuted: '#5C6470',
    textSubtle: '#8B919C',
    accent: '#5957E8',
    accentHover: '#4A48D6',
    accentSoft: 'rgba(89,87,232,0.10)',
    accentBorder: 'rgba(89,87,232,0.28)',
    success: '#2BB673',
    warning: '#D89B1F',
    danger: '#E5484D',
    chipBg: '#F2F1EB',
  },
}
