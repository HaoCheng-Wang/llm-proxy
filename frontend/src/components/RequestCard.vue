<template>
  <div class="request-card">
    <!-- Card Header -->
    <div class="request-card-header" @click="toggleExpand">
      <div class="flex gap-12" style="align-items:center">
        <span class="request-direction" :class="['post','patch','put'].includes(req.method.toLowerCase()) ? 'dir-req' : 'dir-resp'">
          {{ ['post','patch','put'].includes(req.method.toLowerCase()) ? '📤' : '📥' }}
        </span>
        <span :class="['method-tag', 'method-' + req.method.toLowerCase()]">{{ req.method }}</span>
        <span :class="['status-tag', getStatusClass(req.status_code)]">{{ req.status_code }}</span>
        <span class="req-path">{{ req.path.length > 60 ? req.path.slice(0, 60) + '...' : req.path }}</span>
        <span v-if="req.duration_ms" class="text-sm text-muted">{{ req.duration_ms }}ms</span>
      </div>
      <div class="flex gap-8" style="align-items:center">
        <button class="btn btn-sm" style="color:#e74c3c;font-size:11px;padding:2px 6px;background:transparent;border:1px solid rgba(231,76,60,0.3);border-radius:4px"
                @click.stop="$emit('delete', req)"
                title="删除此条记录">
          ✕
        </button>
        <span class="text-sm text-muted">{{ formatTime(req.created_at) }}</span>
        <span style="color:#85929e;font-size:12px">{{ isExpanded ? '▲' : '▼' }}</span>
      </div>
    </div>

    <!-- Expanded Body -->
    <div v-if="isExpanded" class="request-card-body">
      <div class="json-panels">
        <!-- Request JSON -->
        <div class="json-panel json-panel-request">
          <div class="json-panel-header">
            <span>📤 请求 JSON</span>
          </div>
          <div class="json-tree-wrapper">
            <JsonTree v-if="parsedRequestBody !== null" :data="parsedRequestBody" />
            <pre v-else class="json-content">{{ formatJson(req.request_body) }}</pre>
          </div>
        </div>
        <!-- Response JSON -->
        <div class="json-panel json-panel-response">
          <div class="json-panel-header">
            <span>📥 响应 JSON</span>
          </div>
          <div class="json-tree-wrapper">
            <JsonTree v-if="parsedResponseBody !== null" :data="parsedResponseBody" />
            <pre v-else class="json-content">{{ formatJson(req.response_body) }}</pre>
          </div>
        </div>
      </div>

      <!-- Headers toggle -->
      <div class="headers-section">
        <button class="btn btn-outline btn-sm" @click.stop="headersExpanded = !headersExpanded">
          {{ headersExpanded ? '🔽 隐藏HTTP头' : '🔍 查看HTTP头' }}
        </button>
        <div v-if="headersExpanded" class="headers-grid">
          <div>
            <div class="section-label">📤 请求头</div>
            <pre class="headers-pre">{{ formatJson(req.request_headers) }}</pre>
          </div>
          <div>
            <div class="section-label">📥 响应头</div>
            <pre class="headers-pre">{{ formatJson(req.response_headers) }}</pre>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed } from 'vue'
import JsonTree from './JsonTree.vue'

const props = defineProps({
  req: { type: Object, required: true },
  isExpanded: { type: Boolean, default: false },
})

const emit = defineEmits(['toggle', 'delete'])

const headersExpanded = ref(false)

const parsedRequestBody = computed(() => parseJsonOrNull(props.req.request_body))
const parsedResponseBody = computed(() => parseJsonOrNull(props.req.response_body))

function toggleExpand() {
  emit('toggle', props.req)
}

function parseJsonOrNull(raw) {
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch (e) { return null }
}

function formatJson(raw) {
  if (!raw) return '(空)'
  if (typeof raw === 'object') return JSON.stringify(raw, null, 2)
  return raw
}

function formatTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

function getStatusClass(code) {
  if (!code) return 'status-unknown'
  if (code >= 200 && code < 300) return 'status-success'
  if (code >= 400 && code < 500) return 'status-client-error'
  if (code >= 500) return 'status-server-error'
  return 'status-unknown'
}
</script>
