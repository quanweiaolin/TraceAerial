/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // 开发环境代理/location-review到后端，省得本地跑前端时还要配CORS或者
    // 手动填VITE_API_BASE_URL(Finding I6)——生产构建走VITE_API_BASE_URL，
    // 跟这个dev proxy互不影响。
    proxy: {
      '/location-review': 'http://localhost:8088',
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: true,
  },
})
