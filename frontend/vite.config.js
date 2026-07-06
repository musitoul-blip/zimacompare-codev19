import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  define: { __APP_VERSION__: JSON.stringify(process.env.VITE_APP_VERSION || '0.0.0') },
  server: {
    proxy: {
      '/api': 'http://localhost:8516',
      '/ws':  { target: 'ws://localhost:8516', ws: true },
    },
  },
})
