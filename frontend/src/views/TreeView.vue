<template>
  <div style="min-height:100vh;background:#0f1923;color:#e0e6ed;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
    <!-- Header -->
    <div style="padding:12px 20px;background:#15222b;border-bottom:1px solid #2c3e50;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10">
      <button @click="window.close()" style="background:none;border:1px solid #2c3e50;color:#85929e;padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px">← 关闭</button>
      <span style="font-size:14px;color:#aeb6bf">{{ metadata.method }} {{ metadata.path?.slice(0, 80) }}</span>
      <span :class="['status-tag', getStatusClass(metadata.status_code)]">{{ metadata.status_code }}</span>
      <span v-if="metadata.duration_ms" style="font-size:12px;color:#85929e">{{ metadata.duration_ms }}ms</span>
    </div>

    <div v-if="!data" style="padding:40px;text-align:center;color:#85929e">
      <p>没有数据可显示，请从端口详情页重新打开。</p>
    </div>

    <div v-else style="padding:12px 20px;max-width:1400px;margin:0 auto">
      <!-- Toolbar -->
      <div style="margin-bottom:12px;display:flex;gap:8px;align-items:center">
        <button class="btn btn-sm" :class="viewMode === 'tree' ? 'btn-primary' : 'btn-outline'" @click="viewMode = 'tree'">
          🌳 树形查看
        </button>
        <button class="btn btn-sm" :class="viewMode === 'text' ? 'btn-primary' : 'btn-outline'" @click="viewMode = 'text'">
          📝 纯文本
        </button>
        <button class="btn btn-outline btn-sm" @click="headersExpanded = !headersExpanded">
          {{ headersExpanded ? '🔽 隐藏HTTP头' : '🔍 查看HTTP头' }}
        </button>
      </div>

      <!-- Headers -->
      <div v-if="headersExpanded" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
        <div>
          <div style="font-size:12px;color:#85929e;margin-bottom:4px">📤 请求头</div>
          <pre style="background:#15222b;padding:10px;border-radius:4px;font-size:12px;overflow:auto;max-height:300px;color:#aeb6bf">{{ formatHeaders(data.requestHeaders) }}</pre>
        </div>
        <div>
          <div style="font-size:12px;color:#85929e;margin-bottom:4px">📥 响应头</div>
          <pre style="background:#15222b;padding:10px;border-radius:4px;font-size:12px;overflow:auto;max-height:300px;color:#aeb6bf">{{ formatHeaders(data.responseHeaders) }}</pre>
        </div>
      </div>

      <!-- JSON Panels -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <div class="json-panel">
          <div style="font-size:12px;color:#85929e;margin-bottom:4px">📤 请求 JSON</div>
          <div v-if="viewMode === 'tree'" style="background:#15222b;border-radius:4px;padding:8px;max-height:70vh;overflow:auto">
            <JsonTree v-if="data.request !== null && typeof data.request === 'object'" :data="data.request" />
            <pre v-else style="font-size:12px;color:#aeb6bf;white-space:pre-wrap">{{ formatValue(data.request) }}</pre>
          </div>
          <pre v-else style="background:#15222b;padding:10px;border-radius:4px;font-size:12px;max-height:70vh;overflow:auto;color:#aeb6bf;white-space:pre-wrap">{{ formatValue(data.request) }}</pre>
        </div>
        <div class="json-panel">
          <div style="font-size:12px;color:#85929e;margin-bottom:4px">📥 响应 JSON</div>
          <div v-if="viewMode === 'tree'" style="background:#15222b;border-radius:4px;padding:8px;max-height:70vh;overflow:auto">
            <JsonTree v-if="data.response !== null && typeof data.response === 'object'" :data="data.response" />
            <pre v-else style="font-size:12px;color:#aeb6bf;white-space:pre-wrap">{{ formatValue(data.response) }}</pre>
          </div>
          <pre v-else style="background:#15222b;padding:10px;border-radius:4px;font-size:12px;max-height:70vh;overflow:auto;color:#aeb6bf;white-space:pre-wrap">{{ formatValue(data.response) }}</pre>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import JsonTree from '../components/JsonTree.vue'

const viewMode = ref('tree')
const headersExpanded = ref(false)
const data = ref(null)
const metadata = ref({})

function getStatusClass(code) {
  if (!code) return ''
  if (code < 300) return 'status-2xx'
  if (code < 400) return 'status-3xx'
  if (code < 500) return 'status-4xx'
  return 'status-5xx'
}

function formatHeaders(headers) {
  if (!headers) return '(空)'
  if (typeof headers === 'object') return JSON.stringify(headers, null, 2)
  return headers
}

function formatValue(val) {
  if (val === null || val === undefined) return '(空)'
  if (typeof val === 'object') return JSON.stringify(val, null, 2)
  return String(val)
}

onMounted(() => {
  const key = new URLSearchParams(window.location.search).get('key')
  if (!key) return
  try {
    const stored = sessionStorage.getItem(key)
    if (stored) {
      const parsed = JSON.parse(stored)
      data.value = parsed
      metadata.value = parsed.metadata || {}
      sessionStorage.removeItem(key) // clean up
    }
  } catch (e) {
    console.error('Failed to load tree view data', e)
  }
})
</script>

<style>
.status-tag {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}
.status-2xx { background: rgba(39,174,96,0.15); color: #27ae60; }
.status-3xx { background: rgba(93,173,226,0.15); color: #5dade2; }
.status-4xx { background: rgba(230,126,34,0.15); color: #e67e22; }
.status-5xx { background: rgba(231,76,60,0.15); color: #e74c3c; }

.btn { cursor:pointer; border:none; border-radius:4px; font-size:13px; padding:6px 14px; transition:all 0.15s; }
.btn-primary { background:#2980b9; color:#fff; }
.btn-primary:hover { background:#3498db; }
.btn-outline { background:transparent; border:1px solid #2c3e50; color:#85929e; }
.btn-outline:hover { border-color:#5dade2; color:#5dade2; }
.btn-sm { padding:4px 10px; font-size:12px; }

.json-panel { border:1px solid #2c3e50; border-radius:6px; overflow:hidden; }
</style>
