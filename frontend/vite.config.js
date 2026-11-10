import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Em produção o servidor Python (finance/server.py) serve o build e a API na porta 5273.
// Em desenvolvimento (./start.sh --dev) o Vite roda na 5173 e repassa /api e /data a ele.
// O Host do navegador segue intacto, porque é com ele que o servidor confere a Origin; a
// forma curta ('/api': url) liga changeOrigin e faria o servidor recusar toda gravação.
const API = { target: `http://127.0.0.1:${process.env.FINANCE_PORT || 5273}`,
  changeOrigin: false }

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    open: true,
    proxy: { '/api': API, '/data': API },
  },
})
