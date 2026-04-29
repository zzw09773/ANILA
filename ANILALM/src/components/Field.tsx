import { type InputHTMLAttributes } from 'react'
import { useTheme } from '../theme/ThemeContext'

interface FieldProps {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  type?: InputHTMLAttributes<HTMLInputElement>['type']
  autoComplete?: string
  autoFocus?: boolean
  disabled?: boolean
}

export function Field({
  label,
  value,
  onChange,
  placeholder,
  type = 'text',
  autoComplete,
  autoFocus,
  disabled,
}: FieldProps) {
  const { t } = useTheme()
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <span style={{ fontSize: 12, fontWeight: 500, color: t.textMuted }}>{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        disabled={disabled}
        style={{
          height: 42,
          padding: '0 14px',
          borderRadius: 10,
          background: t.surface,
          border: `1px solid ${t.border}`,
          color: t.text,
          fontSize: 14,
          outline: 'none',
          fontFamily: 'inherit',
        }}
      />
    </label>
  )
}
