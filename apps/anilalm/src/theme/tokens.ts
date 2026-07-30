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
    // WCAG AA: lighten on dark bg so 10.5–11px secondary text reaches ≥4.5:1.
    textSubtle: '#828A96',
    // WCAG AA: darkened from #7C7BFF so white button text on accent reaches
    // ≥4.5:1 for normal-size text in dark theme.
    // WCAG AA on near-black bg: lighten accent for text. White-on-accent
    // drops to ~4.25 (large/bold button text still clears 3:1 UI threshold;
    // dual 4.5:1 with white fill text is impossible on this bg without a
    // separate fill token).
    accent: '#6E6CE5',
    accentHover: '#7F7DF0',
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
    // WCAG AA: darken on light bg so 10.5–11px secondary text reaches ≥4.5:1.
    textSubtle: '#6B7280',
    accent: '#5957E8',
    accentHover: '#4A48D6',
    accentSoft: 'rgba(89,87,232,0.10)',
    accentBorder: 'rgba(89,87,232,0.28)',
    // WCAG AA: status colours as text on bg (was pastel / mid-bright).
    // warning kept a step past the 4.5 floor — bare-minimum amber still
    // reads faint by eye on #FAFAF7.
    success: '#1E7A4E',
    warning: '#7A5610',
    danger: '#C93B40',
    chipBg: '#F2F1EB',
  },
}
