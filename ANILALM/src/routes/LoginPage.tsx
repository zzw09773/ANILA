import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useTheme } from '../theme/ThemeContext'
import { useAuthStore } from '../store/auth'
import { explainError } from '../api/client'
import { register as registerApi } from '../api/auth'

// Terminal / CLI aesthetic — direct port of the LoginTerminal variant in
// _design/prototype.html. The UX rationale (and why we kept it instead of
// a split-panel hero): ANILA LM is targeting research/dev users who live
// in terminals, and the mono layout doubles as a soft brand cue ("this is
// a power tool, not a consumer chat-bot").
//
// Behaviour vs. the prototype:
//   - submit() now hits CSP /api/auth/login (real auth) instead of setTimeout.
//   - register tab POSTs /api/auth/register, then bounces back to login tab.
//   - error rendering is pulled from explainError() so backend ``detail``
//     strings (LDAP failures, account-pending, etc.) surface verbatim.

const MONO = `ui-monospace, "JetBrains Mono", "SF Mono", Menlo, monospace`

const ASCII_LOGO = ` █████╗ ███╗   ██╗██╗██╗      █████╗ ██╗     ███╗   ███╗
██╔══██╗████╗  ██║██║██║     ██╔══██╗██║     ████╗ ████║
███████║██╔██╗ ██║██║██║     ███████║██║     ██╔████╔██║
██╔══██║██║╚██╗██║██║██║     ██╔══██║██║     ██║╚██╔╝██║
██║  ██║██║ ╚████║██║███████╗██║  ██║███████╗██║ ╚═╝ ██║
╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝╚══════╝╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝`

export function LoginPage() {
  const { theme, toggle: toggleTheme } = useTheme()
  const navigate = useNavigate()
  const location = useLocation()
  const login = useAuthStore((s) => s.login)
  const accessToken = useAuthStore((s) => s.accessToken)

  const [tab, setTab] = useState<'login' | 'register'>('login')
  const [u, setU] = useState('')
  const [p, setP] = useState('')
  const [email, setEmail] = useState('')
  const [focused, setFocused] = useState<'u' | 'p' | 'e'>('u')
  const [err, setErr] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [time, setTime] = useState(() => new Date())

  useEffect(() => {
    const id = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(id)
  }, [])

  // ``fromPath`` is read fresh from the latest location on each commit,
  // but we don't want it as an effect dep — that would re-fire navigate()
  // every time the location updates (e.g. when navigate() itself fires
  // and replaces the URL with a new state object). Stuffing it into a ref
  // means the effect deps shrink to just ``accessToken``, and a fired
  // ref guards against any redundant double-fire.
  const fromPathRef = useRef('/')
  fromPathRef.current =
    (location.state as { from?: { pathname?: string } } | null)?.from?.pathname ?? '/'
  const navigatedRef = useRef(false)

  useEffect(() => {
    if (accessToken && !navigatedRef.current) {
      navigatedRef.current = true
      navigate(fromPathRef.current, { replace: true })
    }
  }, [accessToken, navigate])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setErr(null)
    setInfo(null)
    if (!u.trim() || u.length < 3) {
      setErr('username 至少 3 個字元')
      return
    }
    if (!p || p.length < 6) {
      setErr('password 至少 6 個字元')
      return
    }

    setLoading(true)
    try {
      if (tab === 'register') {
        await registerApi(u, email, p)
        setInfo('註冊已送出，等候 admin 核准後即可登入。')
        setTab('login')
      } else {
        await login(u, p)
        // The accessToken-watching effect above will navigate; we still
        // call navigate here as a fallback for the case where login()
        // resolved but the effect's render hasn't run yet (rare but
        // observable on slow devices). The ref prevents double-fire.
        if (!navigatedRef.current) {
          navigatedRef.current = true
          navigate(fromPathRef.current, { replace: true })
        }
      }
    } catch (anyErr) {
      setErr(explainError(anyErr))
    } finally {
      setLoading(false)
    }
  }

  // Terminal palette — locked to a darker variant than the rest of the app
  // so the "this is a TTY" feel reads even in light mode.
  const bg = theme === 'dark' ? '#08090C' : '#F5F4EE'
  const ink = theme === 'dark' ? '#E6E8EA' : '#1A1A1A'
  const dim = theme === 'dark' ? '#6B7280' : '#8B919C'
  const ok = '#3DD68C'
  const accent = theme === 'dark' ? '#7C7BFF' : '#5957E8'
  const line = theme === 'dark' ? '#1E2128' : '#E0DDD3'
  const danger = '#FF6B6B'

  const ts = time.toTimeString().slice(0, 8)
  const dateStr = time.toLocaleDateString('zh-TW')

  return (
    <div
      style={{
        minHeight: '100vh',
        background: bg,
        color: ink,
        fontFamily: MONO,
        display: 'flex',
        flexDirection: 'column',
        backgroundImage:
          theme === 'dark'
            ? 'radial-gradient(circle at 20% 0%, rgba(124,123,255,0.05), transparent 50%)'
            : 'radial-gradient(circle at 20% 0%, rgba(89,87,232,0.04), transparent 50%)',
      }}
    >
      {/* Top bar — terminal chrome */}
      <div
        style={{
          height: 32,
          padding: '0 14px',
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          borderBottom: `1px solid ${line}`,
          background: theme === 'dark' ? '#0C0E13' : '#EFEDE5',
        }}
      >
        <div style={{ display: 'flex', gap: 6 }}>
          {['#FF5F56', '#FFBD2E', '#27C93F'].map((c) => (
            <span
              key={c}
              style={{ width: 11, height: 11, borderRadius: '50%', background: c }}
            />
          ))}
        </div>
        <span style={{ fontSize: 11, color: dim }}>~/anilalm — auth — 80×24</span>
        <button
          onClick={toggleTheme}
          title="切換主題"
          style={{
            marginLeft: 'auto',
            padding: '3px 9px',
            border: `1px solid ${line}`,
            background: 'transparent',
            color: dim,
            fontSize: 10.5,
            fontFamily: MONO,
            cursor: 'pointer',
            borderRadius: 0,
            letterSpacing: 0.5,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 6,
            transition: 'all 150ms',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.color = ink
            e.currentTarget.style.borderColor = accent
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.color = dim
            e.currentTarget.style.borderColor = line
          }}
        >
          <span style={{ color: accent }}>$</span> theme --set{' '}
          <span style={{ color: ink }}>{theme === 'dark' ? 'light' : 'dark'}</span>
        </button>
        <span style={{ fontSize: 11, color: dim, marginLeft: 10 }}>{ts}</span>
      </div>

      {/* Body */}
      <div style={{ flex: 1, display: 'grid', placeItems: 'center', padding: 24 }}>
        <div
          style={{
            width: '100%',
            maxWidth: 620,
            fontSize: 13.5,
            lineHeight: 1.85,
          }}
        >
          {/* ASCII logo */}
          <pre
            style={{
              margin: 0,
              color: accent,
              fontSize: 14,
              lineHeight: 1.05,
              fontWeight: 700,
              letterSpacing: 0,
              fontFamily: MONO,
            }}
          >
            {ASCII_LOGO}
          </pre>
          <div style={{ marginTop: 8, color: dim, fontSize: 11.5 }}>
            v0.1.0 · 把你的文件變成會聊天的知識庫
          </div>

          <div style={{ marginTop: 32, color: dim }}>
            <span style={{ color: ok }}>●</span> 連線到{' '}
            <span style={{ color: ink }}>{import.meta.env.VITE_CSP_BACKEND ?? '/api'}</span>{' '}
            ... <span style={{ color: ok }}>OK</span>
          </div>
          <div style={{ color: dim }}>
            <span style={{ color: ok }}>●</span> TLS 1.3 · ingestion-worker ready · LLM proxy
            online
          </div>

          <div style={{ marginTop: 28 }}>
            <span style={{ color: dim }}>$</span>{' '}
            <span style={{ color: ink }}>auth {tab} --help</span>
            <div style={{ color: dim, marginTop: 4, paddingLeft: 14, fontSize: 12 }}>
              username 長度 ≥ 3，password 長度 ≥ 6
              {tab === 'register' && '；email 可空，admin 核准後生效'}
            </div>
          </div>

          {/* Tab switcher — minimal */}
          <div style={{ marginTop: 24, display: 'flex', gap: 0 }}>
            {(
              [
                ['login', '登入'],
                ['register', '註冊'],
              ] as const
            ).map(([k, l], i) => (
              <button
                key={k}
                type="button"
                onClick={() => {
                  setTab(k)
                  setErr(null)
                  setInfo(null)
                }}
                style={{
                  padding: '6px 14px',
                  border: 'none',
                  cursor: 'pointer',
                  fontFamily: MONO,
                  background: 'transparent',
                  color: tab === k ? accent : dim,
                  fontSize: 12.5,
                  fontWeight: tab === k ? 600 : 400,
                  borderBottom: `2px solid ${tab === k ? accent : 'transparent'}`,
                  transition: 'all 150ms',
                }}
              >
                <span style={{ opacity: 0.5, marginRight: 6 }}>0{i + 1}</span>
                {l}
              </button>
            ))}
          </div>

          <form
            onSubmit={submit}
            style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 4 }}
          >
            <TerminalField
              id="term-u"
              label="username"
              value={u}
              onChange={setU}
              placeholder="zzw"
              focused={focused === 'u'}
              onFocus={() => setFocused('u')}
              accent={accent}
              ink={ink}
              dim={dim}
              disabled={loading}
            />

            {tab === 'register' && (
              <TerminalField
                id="term-e"
                label="email"
                value={email}
                onChange={setEmail}
                placeholder="you@example.com"
                focused={focused === 'e'}
                onFocus={() => setFocused('e')}
                accent={accent}
                ink={ink}
                dim={dim}
                type="email"
                disabled={loading}
              />
            )}

            <TerminalField
              id="term-p"
              label="password"
              value={p}
              onChange={setP}
              placeholder="••••••••"
              type="password"
              focused={focused === 'p'}
              onFocus={() => setFocused('p')}
              accent={accent}
              ink={ink}
              dim={dim}
              disabled={loading}
            />

            {err && (
              <div
                style={{
                  marginTop: 10,
                  padding: '8px 10px',
                  borderLeft: `2px solid ${danger}`,
                  background:
                    theme === 'dark' ? 'rgba(255,107,107,0.08)' : 'rgba(229,72,77,0.06)',
                  color: danger,
                  fontSize: 12.5,
                }}
              >
                <span style={{ color: danger }}>[error]</span> {err}
              </div>
            )}

            {info && (
              <div
                style={{
                  marginTop: 10,
                  padding: '8px 10px',
                  borderLeft: `2px solid ${ok}`,
                  background:
                    theme === 'dark' ? 'rgba(61,214,140,0.08)' : 'rgba(43,182,115,0.06)',
                  color: ok,
                  fontSize: 12.5,
                }}
              >
                <span>[info]</span> {info}
              </div>
            )}

            {/* Submit row */}
            <div
              style={{
                marginTop: 20,
                display: 'flex',
                alignItems: 'center',
                gap: 12,
              }}
            >
              <button
                type="submit"
                disabled={loading}
                style={{
                  padding: '8px 18px',
                  border: `1px solid ${accent}`,
                  background:
                    theme === 'dark'
                      ? 'rgba(124,123,255,0.12)'
                      : 'rgba(89,87,232,0.08)',
                  color: accent,
                  fontSize: 13,
                  fontFamily: MONO,
                  fontWeight: 500,
                  cursor: loading ? 'wait' : 'pointer',
                  borderRadius: 0,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 8,
                  letterSpacing: 0.3,
                }}
              >
                {loading
                  ? '[ ⠼ 驗證中... ]'
                  : `[ ↵ ${tab === 'login' ? 'execute login' : 'execute register'} ]`}
              </button>
              <span style={{ fontSize: 11.5, color: dim }}>
                按{' '}
                <span
                  style={{
                    color: ink,
                    padding: '1px 5px',
                    border: `1px solid ${line}`,
                  }}
                >
                  Enter
                </span>{' '}
                執行
              </span>
            </div>
          </form>

          {/* Status footer */}
          <div
            style={{
              marginTop: 36,
              paddingTop: 14,
              borderTop: `1px dashed ${line}`,
              display: 'flex',
              justifyContent: 'space-between',
              color: dim,
              fontSize: 11,
            }}
          >
            <span>NORMAL · auth.tsx · UTF-8 · LF</span>
            <span>
              session: <span style={{ color: ok }}>● secure</span> · {dateStr}
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}

interface TerminalFieldProps {
  id: string
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  type?: 'text' | 'email' | 'password'
  focused: boolean
  onFocus: () => void
  accent: string
  ink: string
  dim: string
  disabled?: boolean
}

function TerminalField({
  id,
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  focused,
  onFocus,
  accent,
  ink,
  dim,
  disabled,
}: TerminalFieldProps) {
  return (
    <div
      onClick={() => {
        onFocus()
        document.getElementById(id)?.focus()
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '8px 0',
        cursor: 'text',
      }}
    >
      <span style={{ color: focused ? accent : dim, width: 12 }}>
        {focused ? '▸' : ' '}
      </span>
      <span style={{ color: dim, minWidth: 80 }}>{label}:</span>
      <input
        id={id}
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={onFocus}
        placeholder={placeholder}
        disabled={disabled}
        autoComplete={
          type === 'password'
            ? 'current-password'
            : label === 'email'
              ? 'email'
              : 'username'
        }
        style={{
          flex: 1,
          border: 'none',
          outline: 'none',
          background: 'transparent',
          color: ink,
          fontSize: 13.5,
          fontFamily: MONO,
          caretColor: accent,
        }}
      />
    </div>
  )
}
