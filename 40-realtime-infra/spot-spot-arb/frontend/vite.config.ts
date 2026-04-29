import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 35173,
    proxy: {
      '/api': 'http://localhost:38742',
      '/ws': { target: 'ws://localhost:38742', ws: true },
    },
  },
})
