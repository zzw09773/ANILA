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
    bg: '#161A22',
    surface: '#1C212B',
    surface2: '#232936',
    elevated: '#2B3240',
    border: '#2A303C',
    borderStrong: '#3A4353',
    text: '#DBE1EC',
    textMuted: '#9AA3B4',
    textSubtle: '#828A98',
    accent: '#6F8FC2',
    accentHover: '#8AA5D1',
    accentSoft: 'rgba(111,143,194,0.12)',
    accentBorder: 'rgba(111,143,194,0.32)',
    success: '#6BBF8F',
    warning: '#D6A95F',
    danger: '#D67A82',
    chipBg: '#232936',
  },
  light: {
    bg: '#F7F8FA',
    surface: '#FFFFFF',
    surface2: '#EEF1F5',
    elevated: '#FFFFFF',
    border: '#DFE3EA',
    borderStrong: '#C3CAD6',
    text: '#1B2230',
    textMuted: '#48505F',
    textSubtle: '#686F7E',
    accent: '#2B4C7E',
    accentHover: '#1F3A63',
    accentSoft: 'rgba(43,76,126,0.08)',
    accentBorder: 'rgba(43,76,126,0.28)',
    success: '#2C7A54',
    warning: '#9A6619',
    danger: '#B03636',
    chipBg: '#EEF1F5',
  },
}
