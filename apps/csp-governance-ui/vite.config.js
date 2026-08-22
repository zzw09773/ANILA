import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Frontend routes must not share exact paths with unprefixed APIs.
// Only /api, /v1, /v2 are proxied; /keys is the SPA page (not /api-keys).
function preserveSetCookieHeaders(proxy) {
  proxy.on('proxyRes', (proxyRes) => {
    const cookies = proxyRes.headers['set-cookie']
    if (Array.isArray(cookies) && cookies.length > 1) {
      proxyRes.headers['set-cookie'] = cookies
    }
  })
}

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '^/api(?:/|$)': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        configure: preserveSetCookieHeaders,
      },
      '/v1': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/v2': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
