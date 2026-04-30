/** @type {import('tailwindcss').Config} */
export default {
  content: [
    './index.html',
    './src/**/*.{vue,js,ts,jsx,tsx}',
  ],
  // Theme is driven by [data-theme="dark"|"light"] on <html>, so we surface
  // semantic tokens to Tailwind utilities (`bg-bg`, `text-fg-2`, `border-c-border`)
  // and let CSS variables do the swap at runtime — no class-based dark: variants.
  theme: {
    extend: {
      fontFamily: {
        mono: ['var(--font-mono)'],
        sans: ['var(--font-mono)'],
      },
      fontSize: {
        '2xs': ['var(--t-2xs)', { lineHeight: 'var(--lh-tight)' }],
        xs:    ['var(--t-xs)',  { lineHeight: 'var(--lh-tight)' }],
        sm:    ['var(--t-sm)',  { lineHeight: 'var(--lh-base)' }],
        base:  ['var(--t-base)',{ lineHeight: 'var(--lh-base)' }],
        md:    ['var(--t-md)',  { lineHeight: 'var(--lh-base)' }],
        lg:    ['var(--t-lg)',  { lineHeight: 'var(--lh-base)' }],
        xl:    ['var(--t-xl)',  { lineHeight: 'var(--lh-tight)' }],
        '2xl': ['var(--t-2xl)', { lineHeight: 'var(--lh-tight)' }],
        '3xl': ['var(--t-3xl)', { lineHeight: 'var(--lh-tight)' }],
        '4xl': ['var(--t-4xl)', { lineHeight: 'var(--lh-tight)' }],
      },
      letterSpacing: {
        caps: 'var(--tracking-caps)',
        tight: 'var(--tracking-tight)',
      },
      borderRadius: {
        none: 'var(--r-sharp)',
        sm: 'var(--r-soft)',
        DEFAULT: 'var(--r-soft)',
      },
      colors: {
        bg:        'var(--c-bg)',
        'surface-1': 'var(--c-surface-1)',
        'surface-2': 'var(--c-surface-2)',
        'surface-3': 'var(--c-surface-3)',
        overlay:   'var(--c-overlay)',
        'fg-1': 'var(--c-fg-1)',
        'fg-2': 'var(--c-fg-2)',
        'fg-3': 'var(--c-fg-3)',
        'fg-mute': 'var(--c-fg-mute)',
        accent: {
          DEFAULT: 'var(--c-accent)',
          strong:  'var(--c-accent-strong)',
          soft:    'var(--c-accent-soft)',
          fg:      'var(--c-accent-fg)',
        },
        info:    'var(--c-info)',
        'info-soft': 'var(--c-info-soft)',
        warn:    'var(--c-warn)',
        'warn-soft': 'var(--c-warn-soft)',
        danger:  'var(--c-danger)',
        'danger-soft': 'var(--c-danger-soft)',
        ok:      'var(--c-ok)',
        'ok-soft': 'var(--c-ok-soft)',
        llm:       'var(--c-llm)',
        vlm:       'var(--c-vlm)',
        embedding: 'var(--c-embedding)',
        agent:     'var(--c-agent)',
        // Border tokens — Tailwind's `border-` utility looks for these.
        'c-border':         'var(--c-border)',
        'c-border-strong':  'var(--c-border-strong)',
        'c-border-accent':  'var(--c-border-accent)',
      },
      borderColor: theme => ({
        DEFAULT: 'var(--c-border)',
        ...theme('colors'),
      }),
      ringColor: theme => ({
        DEFAULT: 'var(--c-focus-ring)',
        ...theme('colors'),
      }),
    },
  },
  plugins: [],
}
