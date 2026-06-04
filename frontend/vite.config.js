import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3999,
    proxy: {
      '/api': {
        target: 'http://localhost:3998',
        changeOrigin: true,
      }
    }
  }
})
