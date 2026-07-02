import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { TOKENS, type ThemeName, type ThemeTokens } from './tokens'

interface ThemeContextValue {
  theme: ThemeName
  t: ThemeTokens
  toggle: () => void
  setTheme: (name: ThemeName) => void
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: 'dark',
  t: TOKENS.dark,
  toggle: () => {},
  setTheme: () => {},
})

const STORAGE_KEY = 'anilalm:theme'

const loadStoredTheme = (): ThemeName => {
  if (typeof window === 'undefined') return 'dark'
  const v = window.localStorage.getItem(STORAGE_KEY)
  return v === 'light' || v === 'dark' ? v : 'dark'
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(loadStoredTheme)

  useEffect(() => {
    document.body.classList.toggle('light', theme === 'light')
    window.localStorage.setItem(STORAGE_KEY, theme)
  }, [theme])

  const setTheme = useCallback((name: ThemeName) => setThemeState(name), [])
  const toggle = useCallback(
    () => setThemeState((prev) => (prev === 'dark' ? 'light' : 'dark')),
    [],
  )

  const value = useMemo<ThemeContextValue>(
    () => ({ theme, t: TOKENS[theme], toggle, setTheme }),
    [theme, toggle, setTheme],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export const useTheme = () => useContext(ThemeContext)
