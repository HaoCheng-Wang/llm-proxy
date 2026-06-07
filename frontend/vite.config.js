import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 如需修改前端绑定地址或端口，直接修改下面 host/port 即可。
// 后端 API 端口（默认 3998）通过 .env 的 API_PORT 配置。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 3999,
    proxy: {
      '/api': {
        target: 'http://localhost:3998',
        changeOrigin: true,
      }
    }
  }
})
