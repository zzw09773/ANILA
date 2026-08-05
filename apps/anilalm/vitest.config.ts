import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * Test-only config, deliberately separate from vite.config.ts.
 *
 * vite.config.ts is type-checked by the production build (`tsc -b`, see
 * tsconfig.node.json's `include`) and is the only config file the image
 * copies in. Putting `test:` there would make the production build
 * depend on vitest's types — a dev-only dependency — so the day the
 * builder switches to `npm install --omit=dev` the image stops building.
 * This repo has already been bitten by that class of failure (2026-07-31,
 * a .test.ts pulled into the anilalm image build).
 *
 * This file is in neither tsconfig's `include` and is not copied by the
 * Dockerfile, so it cannot reach the production build at all.
 *
 * `include` is kept in step with tsconfig.app.json's `exclude`: test
 * files are `src/**\/*.test.ts(x)` on both sides.
 *
 * The three `*.node.test.mjs` files are deliberately NOT run here: they
 * need real Intl / timezone behaviour and a real module loader, which is
 * meaningless under jsdom (apps/anila-shell made the same call, and
 * found that letting vitest pick them up produces a permanently red
 * "No test suite found"). They run under `npm run test:node`, and
 * `npm test` runs both halves — so the number a reader sees covers every
 * test file in this app, not just the ones vitest can see.
 */
export default defineConfig({
  // The React plugin is needed for the JSX in the .tsx tests; the dev
  // server / proxy / base settings in vite.config.ts are irrelevant here.
  plugins: [react()],
  resolve: {
    // Mirrors vite.config.ts. Nothing imports '@/...' today; this is
    // here so that the first person who does isn't also the person who
    // has to work out why it resolves in the app but not under test.
    alias: { '@': '/src' },
  },
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
