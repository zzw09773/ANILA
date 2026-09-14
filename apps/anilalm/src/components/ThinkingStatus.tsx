import { ThinkingOrb } from 'thinking-orbs'

export function ThinkingStatus({
  state = 'working',
  size = 20,
  label = '思考中',
}: {
  state?: 'working' | 'searching' | 'solving' | 'listening' | 'connecting' | 'weaving' | 'composing' | 'breathing' | 'shaping'
  size?: 20 | 64
  label?: string
}) {
  return (
    <span role="status" aria-live="polite" aria-label={label} style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <ThinkingOrb state={state} size={size} theme="auto" />
      {label ? <span>{label}</span> : null}
    </span>
  )
}
