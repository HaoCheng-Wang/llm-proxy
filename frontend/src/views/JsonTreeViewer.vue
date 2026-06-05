<template>
  <div class="jtv-page">
    <!-- Header -->
    <div class="jtv-header">
      <div class="flex gap-12" style="align-items:center">
        <button class="btn btn-outline btn-sm" @click="goBack" title="返回上一页">
          ← 返回
        </button>
        <h2>🌳 JSON 树形查看</h2>
      </div>
      <div v-if="reqRecord" class="flex gap-12" style="align-items:center">
        <span class="jtv-label">{{ viewLabel }}</span>
        <span :class="['method-tag', 'method-' + reqRecord.method.toLowerCase()]">{{ reqRecord.method }}</span>
        <span class="jtv-path">{{ reqRecord.path }}</span>
        <span v-if="reqRecord.status_code" :class="['status-tag', getStatusClass(reqRecord.status_code)]">{{ reqRecord.status_code }}</span>
      </div>
    </div>

    <!-- Error: not found -->
    <div v-if="error" class="card empty-state">
      <h3>😕 {{ error }}</h3>
      <button class="btn btn-primary" style="margin-top:16px" @click="goBack">← 返回</button>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="card empty-state">
      <h3>⏳ 加载中...</h3>
    </div>

    <!-- Parsed JSON tree -->
    <div v-if="parsedJson !== null" class="jtv-body">
      <JsonTree :data="parsedJson" />
    </div>

    <!-- Raw text fallback -->
    <div v-if="!loading && !error && parsedJson === null && rawText" class="card">
      <div class="jtv-raw-hint">
        ⚠️ 无法解析为 JSON，以下为原始文本内容：
      </div>
      <pre class="jtv-raw-text">{{ rawText }}</pre>
    </div>

    <!-- Empty body -->
    <div v-if="!loading && !error && !rawText && parsedJson === null" class="card empty-state">
      <h3>📭 空的 JSON 内容</h3>
      <p>该请求/响应没有 JSON 数据体。</p>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, inject } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import api from '../api'
import JsonTree from '../components/JsonTree.vue'

const route = useRoute()
const router = useRouter()
const showToast = inject('showToast', () => {})

const loading = ref(true)
const error = ref('')
const reqRecord = ref(null)

const portId = route.params.portId
const requestId = route.params.requestId
const viewType = route.query.type || 'response' // 'request' | 'response'

const viewLabel = computed(() => viewType === 'request' ? '请求 JSON' : '响应 JSON')

// The raw JSON string to display
const rawJson = computed(() => {
  if (!reqRecord.value) return null
  return viewType === 'request' ? reqRecord.value.request_body : reqRecord.value.response_body
})

// Parsed JSON object (null = not valid JSON or empty)
const parsedJson = computed(() => {
  const raw = rawJson.value
  if (raw == null) return undefined // distinguish from null (valid parsed null)
  if (typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch (e) { return null }
})

// Raw text fallback (when not parseable as JSON)
const rawText = computed(() => {
  const raw = rawJson.value
  if (raw == null) return ''
  if (typeof raw === 'object') return JSON.stringify(raw, null, 2)
  return raw
})

function getStatusClass(code) {
  if (!code) return ''
  if (code < 300) return 'status-2xx'
  if (code < 400) return 'status-3xx'
  if (code < 500) return 'status-4xx'
  return 'status-5xx'
}

function goBack() {
  if (window.history.length > 1) {
    router.back()
  } else {
    router.push('/')
  }
}

onMounted(async () => {
  try {
    reqRecord.value = await api.getSingleRequest(portId, requestId)
  } catch (e) {
    const status = e.response?.status
    if (status === 404) {
      error.value = '请求记录未找到，可能已被删除'
    } else if (status === 403) {
      error.value = '无权访问该请求记录'
    } else {
      error.value = '加载数据失败，请稍后重试'
    }
    if (status !== 404 && status !== 403) {
      showToast('加载失败', 'error')
    }
  } finally {
    loading.value = false
  }
})
</script>

<style>
.jtv-page {
  max-width: 1400px;
  margin: 0 auto;
}

.jtv-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
  padding-bottom: 16px;
  border-bottom: 1px solid #2c3e50;
  flex-wrap: wrap;
  gap: 12px;
}

.jtv-header h2 {
  font-size: 20px;
  color: #e0e6ed;
}

.jtv-label {
  font-size: 13px;
  font-weight: 600;
  color: #5dade2;
  padding: 4px 10px;
  border-radius: 4px;
  background: rgba(93, 173, 226, 0.10);
}

.jtv-path {
  font-size: 13px;
  color: #aeb6bf;
  word-break: break-all;
  max-width: 400px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.jtv-body {
  background: #15222b;
  border: 1px solid #2c3e50;
  border-radius: 10px;
  padding: 20px 24px;
}

.jtv-raw-hint {
  font-size: 13px;
  color: #f39c12;
  margin-bottom: 12px;
}

.jtv-raw-text {
  background: #0f1923;
  border: 1px solid #2c3e50;
  border-radius: 6px;
  padding: 16px;
  font-size: 13px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 600px;
  overflow-y: auto;
  color: #c8d6e5;
  font-family: 'Cascadia Code', 'Fira Code', 'JetBrains Mono', 'Consolas', monospace;
}
</style>
