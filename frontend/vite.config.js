import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const target = process.env.SENTINEL_API_ORIGIN || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/cases': target,
      '/rules': target,
      '/stats': target,
      '/ws': { target, ws: true },
    },
  },
})
