import { ref, watch } from 'vue'

const STORAGE_KEY = 'anila.theme'

function readInitial() {
  if (typeof window === 'undefined') return 'dark'
  const stored = window.localStorage?.getItem(STORAGE_KEY)
  if (stored === 'dark' || stored === 'light') return stored
  // Mirror what the inline pre-mount script did in index.html.
  if (window.matchMedia?.('(prefers-color-scheme: light)').matches) return 'light'
  return 'dark'
}

const theme = ref(readInitial())

function apply(value) {
  if (typeof document === 'undefined') return
  document.documentElement.setAttribute('data-theme', value)
}

apply(theme.value)

watch(theme, (value) => {
  apply(value)
  try {
    window.localStorage?.setItem(STORAGE_KEY, value)
  } catch {
    // Ignore quota / privacy-mode failures — theme still works for the session.
  }
})

export function useTheme() {
  return {
    theme,
    isDark: () => theme.value === 'dark',
    isLight: () => theme.value === 'light',
    setTheme(value) {
      if (value !== 'dark' && value !== 'light') return
      theme.value = value
    },
    toggleTheme() {
      theme.value = theme.value === 'dark' ? 'light' : 'dark'
    },
  }
}
