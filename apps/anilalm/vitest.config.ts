// W0-6①:anilalm 在此之前**零測試設施**(package.json 無 `test` script、
// 無 vitest/jest 依賴),於是 CI 只跑 typecheck + build。而這支 app 承載
// Studio 產出、artifact 版本歷史、知識庫工作區(12,805 行手寫 TS/TSX)。
//
// 刻意獨立於 vite.config.ts:那支帶了 dev proxy 與 base path 推導,測試不需要
// 也不該受其環境變數影響。
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: './vitest.setup.ts',
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
