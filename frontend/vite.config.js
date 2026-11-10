import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// A API e os relatórios vêm do servidor Python (finance/server.py), que o ./start.sh sobe
// junto. Em desenvolvimento o Vite só repassa /api e /data para ele. O Host do navegador
// segue intacto, porque é com ele que o servidor confere a Origin; a forma curta
// ('/api': url) liga changeOrigin e faria o servidor recusar toda gravação.
const API = { target: `http://127.0.0.1:${process.env.FINANCE_PORT || 5274}`,
  changeOrigin: false }

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5273,
    open: true,
    proxy: { '/api': API, '/data': API },
  },
})
