import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// En desarrollo (npm run dev) Vite sirve en el puerto 5173 y redirige
// las llamadas /api hacia el backend FastAPI (puerto 8000).
// En produccion (npm run build) FastAPI sirve tanto la API como la
// interfaz compilada, asi que el proxy no se usa.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
