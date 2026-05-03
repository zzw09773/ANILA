import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// loadEnv reads .env files without pulling in node typings — keeps the
// build config self-contained without needing @types/node.
//
// BASE_PATH: when ANILALM is mounted under a subpath (e.g. ``/anilalm/``
// behind the ANILA reverse proxy), Vite needs to know so that asset URLs
// in index.html resolve absolutely from the prefix. Default is ``/`` for
// local dev. Read from BOTH ``BASE_PATH`` (build-arg friendly) and
// ``VITE_BASE_PATH`` (env-file friendly).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', '')
  const cspBackend = env.VITE_CSP_BACKEND || 'http://localhost:8000'
  const rawBase = env.BASE_PATH || env.VITE_BASE_PATH || '/'
  // Normalise: must start AND end with a slash (Vite contract).
  const base =
    (rawBase.startsWith('/') ? rawBase : `/${rawBase}`).replace(/\/?$/, '/') || '/'
  return {
    base,
    plugins: [react()],
    server: {
      port: 5174,
      strictPort: false,
      proxy: {
        '/api': { target: cspBackend, changeOrigin: true },
        '/v1': { target: cspBackend, changeOrigin: true },
        '/v2': { target: cspBackend, changeOrigin: true },
      },
    },
    resolve: {
      alias: {
        '@': '/src',
      },
    },
  }
})
