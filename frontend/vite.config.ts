/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 18888,
    // 后端FastAPI跑在28000端口，用代理而不是纯CORS，
    // 前端代码可以直接fetch('/api/...')相对路径，跟生产环境同源部署时行为一致。
    proxy: {
      '/api': 'http://localhost:28000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
  },
})
