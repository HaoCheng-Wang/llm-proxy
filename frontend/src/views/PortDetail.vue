<template>
  <div>
    <!-- Header -->
    <div class="flex-between mb-16">
      <div>
        <button class="btn btn-outline btn-sm" @click="$router.push('/')" style="margin-right:12px">
          ← 返回
        </button>
        <h2 v-if="data.port" style="display:inline;font-size:20px">
          端口 {{ data.port.port_number }}
          <span :class="['badge', data.port.is_active ? 'badge-active' : 'badge-inactive']" style="margin-left:8px">
            {{ data.port.is_active ? '运行中' : '已停止' }}
          </span>
        </h2>
      </div>
      <div class="flex gap-8" style="position:relative">
        <!-- Live indicator -->
        <span v-if="data.port?.is_active" class="live-indicator" :class="{ 'live-pulse': polling }">
          <span class="live-dot"></span> 实时
        </span>
        <!-- Copy dropdown -->
        <div class="copy-dropdown" v-click-outside="closeCopyMenu">
          <button class="btn btn-success btn-sm" @click="toggleCopyMenu">
            📋 一键复制 ▾
          </button>
          <div v-if="copyMenuOpen" class="copy-menu">
            <div class="copy-menu-item" @click="copyJsonOnly">📄 仅复制JSON数据</div>
            <div class="copy-menu-item" @click="copyAllData">📦 复制全部交互数据</div>
          </div>
        </div>
        <button class="btn btn-danger btn-sm" @click="clearHistory">
          🗑 清空全部历史
        </button>
      </div>
    </div>

    <!-- Port Info -->
    <div v-if="data.port" class="card mb-16">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:14px">
        <div><span class="text-muted">目标地址：</span>{{ data.port.target_url }}</div>
        <div><span class="text-muted">描述：</span>{{ data.port.description || '-' }}</div>
        <div><span class="text-muted">端口号：</span><code>{{ data.port.port_number }}</code></div>
        <div><span class="text-muted">总请求数：</span>{{ data.port.request_count }}</div>
        <div><span class="text-muted">创建时间：</span>{{ formatTime(data.port.created_at) }}</div>
        <div>
          <span class="text-muted">代理地址：</span>
          <code>http://{{ displayIp }}:{{ data.port.port_number }}/v1</code>
        </div>
      </div>
    </div>

    <!-- Requests List -->
    <div v-if="requests.length > 0">
      <div class="flex-between mb-16">
        <h3 style="font-size:16px">
          交互记录 ({{ requests.length }})
          <span v-if="newCount > 0" class="badge badge-active" style="margin-left:8px">
            +{{ newCount }} 条新记录
          </span>
        </h3>
        <div class="flex gap-8">
          <button class="btn btn-outline btn-sm" @click="expandAll">全部展开</button>
          <button class="btn btn-outline btn-sm" @click="collapseAll">全部折叠</button>
        </div>
      </div>

      <div v-for="(req, index) in requests" :key="req.id" class="request-card">
        <!-- Card Header -->
        <div class="request-card-header" @click="toggleExpand(index)">
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
                    @click.stop="handleDeleteRequest(req, index)"
                    title="删除此条记录">
              ✕
            </button>
            <span class="text-sm text-muted">{{ formatTime(req.created_at) }}</span>
            <span style="color:#85929e;font-size:12px">{{ expanded[index] ? '▲' : '▼' }}</span>
          </div>
        </div>

        <!-- Expanded Body -->
        <div v-if="expanded[index]" class="request-card-body">
          <div class="json-panels">
            <!-- Request JSON -->
            <div class="json-panel json-panel-request">
              <div class="json-panel-header">
                <span>📤 请求 JSON</span>
              </div>
              <div class="json-tree-wrapper">
                <JsonTree v-if="parseJsonOrNull(req.request_body) !== null"
                          :data="parseJsonOrNull(req.request_body)" />
                <pre v-else class="json-content">{{ formatJson(req.request_body) }}</pre>
              </div>
            </div>
            <!-- Response JSON -->
            <div class="json-panel json-panel-response">
              <div class="json-panel-header">
                <span>📥 响应 JSON</span>
              </div>
              <div class="json-tree-wrapper">
                <JsonTree v-if="parseJsonOrNull(req.response_body) !== null"
                          :data="parseJsonOrNull(req.response_body)" />
                <pre v-else class="json-content">{{ formatJson(req.response_body) }}</pre>
              </div>
            </div>
          </div>

          <!-- Headers toggle -->
          <div class="headers-section">
            <button class="btn btn-outline btn-sm" @click.stop="headersExpanded[index] = !headersExpanded[index]">
              {{ headersExpanded[index] ? '🔽 隐藏HTTP头' : '🔍 查看HTTP头' }}
            </button>
            <div v-if="headersExpanded[index]" class="headers-grid">
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
    </div>

    <div v-else class="card empty-state">
      <h3>暂无交互记录</h3>
      <p>配置智能体连接到该端口后，所有API通信将在此显示</p>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, inject, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import JsonTree from '../components/JsonTree.vue'

const route = useRoute()
const showToast = inject('showToast')
const displayIp = ref('your-server-ip')
const portId = route.params.id

const data = ref({ port: null, requests: [] })
const requests = ref([])
const expanded = ref({})
const headersExpanded = ref({})
const copyMenuOpen = ref(false)
const newCount = ref(0)
const polling = ref(false)

let _maxId = 0
let _pollTimer = null

// Click-outside directive
const vClickOutside = {
  mounted(el, binding) {
    el._clickOutside = (event) => {
      if (!(el === event.target || el.contains(event.target))) {
        binding.instance[binding.arg || 'closeCopyMenu']()
      }
    }
    document.addEventListener('click', el._clickOutside)
  },
  unmounted(el) {
    document.removeEventListener('click', el._clickOutside)
  }
}

async function loadData() {
  try {
    data.value = await api.getPortHistory(portId)
    requests.value = data.value.requests || []
    _maxId = requests.value.length > 0 ? Math.max(...requests.value.map(r => r.id)) : 0
    // Auto expand first 3
    const nextExpanded = {}
    requests.value.forEach((_, i) => { if (i < 3) nextExpanded[i] = true })
    expanded.value = nextExpanded
    newCount.value = 0
  } catch (e) {
    showToast('加载数据失败', 'error')
  }
}

async function pollNewRecords() {
  if (!data.value.port?.is_active) return
  polling.value = true
  try {
    const result = await api.getPortHistory(portId, _maxId)
    const newReqs = result.requests || []
    if (newReqs.length > 0) {
      // Get the highest new id
      _maxId = Math.max(_maxId, ...newReqs.map(r => r.id))
      // Prepend to requests (they come desc by created_at, latest first)
      // But _maxId tracking means newer = higher id
      // We need to merge properly: new reqs have higher ids but newest created_at
      // API returns desc by created_at, so they're newest-first already
      // Just prepend them
      requests.value = [...newReqs, ...requests.value]
      newCount.value += newReqs.length
      showToast(`收到 ${newReqs.length} 条新交互记录`, 'info')
    }
  } catch (e) {
    // Silently ignore poll errors
  } finally {
    polling.value = false
  }
}

function startPolling() {
  _pollTimer = setInterval(pollNewRecords, 2000)
}

function stopPolling() {
  if (_pollTimer) {
    clearInterval(_pollTimer)
    _pollTimer = null
  }
}

function parseJsonOrNull(raw) {
  if (!raw) return null
  if (typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch (e) { return null }
}

function toggleExpand(index) {
  expanded.value[index] = !expanded.value[index]
}

function expandAll() {
  const next = {}
  requests.value.forEach((_, i) => { next[i] = true })
  expanded.value = next
}

function collapseAll() {
  expanded.value = {}
  headersExpanded.value = {}
}

function formatJson(raw) {
  if (!raw) return '(空)'
  if (typeof raw === 'object') return JSON.stringify(raw, null, 2)
  return raw
}

function getStatusClass(code) {
  if (!code) return ''
  if (code < 300) return 'status-2xx'
  if (code < 400) return 'status-3xx'
  if (code < 500) return 'status-4xx'
  return 'status-5xx'
}

function toggleCopyMenu() { copyMenuOpen.value = !copyMenuOpen.value }
function closeCopyMenu() { copyMenuOpen.value = false }

async function copyJsonOnly() {
  closeCopyMenu()
  try {
    const output = requests.value.map((r, i) => {
      const entry = { index: i + 1, method: r.method, path: r.path, status_code: r.status_code }
      try { entry.request = JSON.parse(r.request_body) } catch (e) { entry.request = r.request_body }
      try { entry.response = JSON.parse(r.response_body) } catch (e) { entry.response = r.response_body }
      return entry
    })
    await navigator.clipboard.writeText(JSON.stringify(output, null, 2))
    showToast(`已复制 ${output.length} 条JSON数据到剪贴板`, 'success')
  } catch (e) {
    showToast('复制失败', 'error')
  }
}

async function copyAllData() {
  closeCopyMenu()
  try {
    const exportData = await api.exportPortHistory(portId)
    await navigator.clipboard.writeText(JSON.stringify(exportData, null, 2))
    showToast(`已复制 ${exportData.total_requests} 条完整交互记录到剪贴板`, 'success')
  } catch (e) {
    try {
      await navigator.clipboard.writeText(JSON.stringify(data.value, null, 2))
      showToast('已复制全部数据到剪贴板', 'success')
    } catch (e2) {
      showToast('复制失败，请手动选择文本复制', 'error')
    }
  }
}

async function handleDeleteRequest(req, index) {
  if (!confirm(`确定删除 ${req.method} ${req.path.slice(0, 40)} 这条记录吗？`)) return
  try {
    await api.deleteRequest(portId, req.id)
    requests.value.splice(index, 1)
    newCount.value = Math.max(0, newCount.value - 1)
    showToast('已删除', 'success')
  } catch (e) {
    showToast('删除失败', 'error')
  }
}

async function clearHistory() {
  if (!confirm(`确定清空端口 ${data.value.port?.port_number} 的全部交互历史吗？此操作不可恢复！`)) return
  try {
    await api.clearPortHistory(portId)
    requests.value = []
    newCount.value = 0
    showToast('历史记录已清空', 'success')
  } catch (e) {
    showToast('清空失败', 'error')
  }
}

function formatTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

onMounted(async () => {
  try {
    const cfg = await api.getConfig()
    displayIp.value = cfg.display_ip
  } catch (e) { /* keep default */ }
  await loadData()
  startPolling()
})

onUnmounted(() => {
  stopPolling()
})
</script>
