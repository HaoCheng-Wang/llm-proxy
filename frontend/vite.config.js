import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 如需修改前端绑定地址或端口，直接修改下面 host/port 即可。
// 后端 API 端口（默认 3998）通过 .env 的 API_PORT 配置。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 3999,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: 'http://localhost:3998',
        changeOrigin: true,
        // 流式接口（详情/轮询/导出）的硬超时：浏览器侧流被冻结或连接半死时，
        // http-proxy 会销毁到后端的 socket，防止后端流式连接池被僵尸流占满
        // （此前导致详情页“正在从后端拉取交互记录...”永久卡死）。
        // 300s 足够详情/轮询（秒级）与常规导出；超大导出建议用 simple 格式。
        timeout: 300000,
        proxyTimeout: 300000,
      }
    }
  }
})
