import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

const frontendRoot = fileURLToPath(new URL('.', import.meta.url))

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '127.0.0.1',
    strictPort: true,
    allowedHosts: ['localhost', '127.0.0.1'],
    cors: false,
    fs: { strict: true, allow: [frontendRoot] },
    proxy: { '/api': { target: process.env.ERP_API_TARGET || 'http://127.0.0.1:8000' } },
  },
  preview: {
    host: '127.0.0.1',
    strictPort: true,
    allowedHosts: ['localhost', '127.0.0.1'],
    cors: false,
  },
})

