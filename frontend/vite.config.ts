import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Dev: relative /api requests proxy to the local backend.
    proxy: { '/api': 'http://localhost:8000' },
  },
})
