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
          <button class="btn btn-success btn-sm" @click="toggleCopyMenu" title="点击展开导出菜单 — 可导出 JSON 数据或完整交互记录">
            📥 一键导出 ▾
          </button>
          <div v-if="copyMenuOpen" class="copy-menu">
            <div class="copy-menu-hint">导出当前分类下已加载的记录</div>
            <div class="copy-menu-item" @click="exportJsonOnly">📄 仅导出JSON数据</div>
            <div class="copy-menu-item" @click="exportAllData">📦 导出全部交互数据</div>
            <div class="copy-menu-divider"></div>
            <div class="copy-menu-hint">直接从后端导出（无需前端加载）</div>
            <div class="copy-menu-item" @click="exportApiFromServer">🔄 从后端导出全部API请求JSON</div>
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
          <code>http://{{ displayIp }}:{{ data.port.port_number }}</code>
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
        <div class="flex gap-8" style="align-items:center">
          <!-- Method filter tabs -->
          <div class="method-filters">
            <button class="btn btn-sm method-filter-btn" :class="{ active: methodFilter === 'all' }" @click="methodFilter = 'all'">
              全部 ({{ requests.length }})
            </button>
            <button class="btn btn-sm method-filter-btn" :class="{ active: methodFilter === 'api' }" @click="methodFilter = 'api'"
                    title="智能体 API 调用 — 即 POST/PUT/PATCH/DELETE 请求，是实际发送给 LLM 的交互记录">
              📤 API请求 ({{ apiCount }})
            </button>
            <button class="btn btn-sm method-filter-btn" :class="{ active: methodFilter === 'other' }" @click="methodFilter = 'other'"
                    title="非 API 请求 — 包括端口扫描探测、浏览器预检（OPTIONS）、健康检查（HEAD）、网站图标（favicon）、爬虫抓取等触发的 GET/OPTIONS/HEAD 请求，通常可忽略">
              🌐 其他 ({{ otherCount }})
            </button>
          </div>
          <button class="btn btn-outline btn-sm" @click="collapseAll">全部折叠</button>
        </div>
      </div>

      <div v-for="req in filteredRequests" :key="req.id" class="request-card">
        <!-- Card Header -->
        <div class="request-card-header" @click="toggleExpand(req.id)">
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
                    @click.stop="handleDeleteRequest(req)"
                    title="删除此条记录">
              ✕
            </button>
            <span class="text-sm text-muted">{{ formatTime(req.created_at) }}</span>
            <span style="color:#85929e;font-size:12px">{{ expanded[req.id] ? '▲' : '▼' }}</span>
          </div>
        </div>

        <!-- Expanded Body -->
        <div v-if="expanded[req.id]" class="request-card-body"
             @mouseenter="scrollLocked = true" @mouseleave="scrollLocked = false">
          <!-- Toolbar: headers toggle -->
          <div style="margin-bottom:8px;display:flex;gap:8px;align-items:center">
            <button class="btn btn-outline btn-sm" @click.stop="headersExpanded[req.id] = !headersExpanded[req.id]">
              {{ headersExpanded[req.id] ? '🔽 隐藏HTTP头' : '🔍 查看HTTP头' }}
            </button>
          </div>

          <!-- Headers (shown above JSON when expanded) -->
          <div v-if="headersExpanded[req.id]" class="headers-grid" style="margin-bottom:12px">
            <div>
              <div class="section-label">📤 请求头</div>
              <pre class="headers-pre">{{ formatJson(req.request_headers) }}</pre>
            </div>
            <div>
              <div class="section-label">📥 响应头</div>
              <pre class="headers-pre">{{ formatJson(req.response_headers) }}</pre>
            </div>
          </div>

          <!-- JSON Panels -->
          <div class="json-panels">
            <!-- Request JSON -->
            <div class="json-panel json-panel-request">
              <div class="json-panel-header">
                <span>📤 请求 JSON</span>
                <button class="btn btn-sm tree-view-btn tree-view-btn-req"
                        @click="openTreeView(req, 'request')"
                        :disabled="!parseJsonOrNull(req.request_body)"
                        title="在新页面中以树形结构查看请求 JSON">
                  🌳 树形查看
                </button>
              </div>
              <div class="json-tree-wrapper">
                <pre class="json-content">{{ formatJson(req.request_body) }}</pre>
              </div>
            </div>
            <!-- Response JSON -->
            <div class="json-panel json-panel-response">
              <div class="json-panel-header">
                <span>📥 响应 JSON</span>
                <button class="btn btn-sm tree-view-btn tree-view-btn-resp"
                        @click="openTreeView(req, 'response')"
                        :disabled="!parseJsonOrNull(req.response_body)"
                        title="在新页面中以树形结构查看响应 JSON">
                  🌳 树形查看
                </button>
              </div>
              <div class="json-tree-wrapper">
                <pre class="json-content">{{ formatJson(req.response_body) }}</pre>
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

    <!-- Load More -->
    <div v-if="hasMore" style="text-align:center;margin-top:16px;display:flex;gap:12px;justify-content:center">
      <button class="btn btn-outline" @click="loadMore" :disabled="loadingMore">
        {{ loadingMore ? '加载中...' : `加载更多 (${data.port.request_count - requests.length} 条剩余)` }}
      </button>
      <button class="btn btn-primary" @click="loadAll" :disabled="loadingMore">
        {{ loadingMore ? '加载中...' : '加载全部' }}
      </button>
    </div>

    <!-- Usage hints -->
    <div class="card mt-16" style="border-color:#2c3e50;background:rgba(255,255,255,0.02)">
      <div style="font-size:13px;line-height:1.8;color:#85929e">
        <p style="color:#aeb6bf;margin-bottom:4px">💡 使用提示</p>
        <p>• 为降低负载，首次仅加载 10 条记录，可点击 <strong>加载更多</strong> 分批查看，或 <strong>加载全部</strong> 一键拉取所有记录（大量数据时可能较慢）。</p>
        <p>• <strong>📥 一键导出</strong> 默认只导出当前已加载且符合筛选条件的记录；如需导出全部 API 请求，请使用菜单中的 <strong>🔄 从后端导出</strong>，无需在前端全部加载。</p>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onUnmounted, inject, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

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
const loadingMore = ref(false)
const hasMore = ref(false)
const scrollLocked = ref(false)
const methodFilter = ref('api')

// API requests = POST/PUT/PATCH/DELETE (intelligent agent calls)
// Other = GET/OPTIONS/HEAD (browser scans, probes, etc.)
const isApiMethod = (m) => ['post', 'put', 'patch', 'delete'].includes(m.toLowerCase())

const apiCount = computed(() => requests.value.filter(r => isApiMethod(r.method)).length)
const otherCount = computed(() => requests.value.filter(r => !isApiMethod(r.method)).length)

const filteredRequests = computed(() => {
  if (methodFilter.value === 'api') return requests.value.filter(r => isApiMethod(r.method))
  if (methodFilter.value === 'other') return requests.value.filter(r => !isApiMethod(r.method))
  return requests.value
})

let _maxId = 0
let _pollTimer = null
let _offset = 0
const PAGE_SIZE = 10

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
    _offset = 0
    data.value = await api.getPortHistory(portId, 0, PAGE_SIZE, 0)
    requests.value = data.value.requests || []
    _maxId = requests.value.length > 0 ? Math.max(...requests.value.map(r => r.id)) : 0
    _offset = requests.value.length
    hasMore.value = _offset < (data.value.port?.request_count || 0)
    // Don't auto-expand any cards for faster initial render
    expanded.value = {}
    newCount.value = 0
  } catch (e) {
    showToast('加载数据失败', 'error')
  }
}

async function loadMore() {
  if (loadingMore.value || !hasMore.value) return
  loadingMore.value = true
  try {
    const result = await api.getPortHistory(portId, 0, PAGE_SIZE, _offset)
    const newReqs = result.requests || []
    if (newReqs.length > 0) {
      requests.value = [...requests.value, ...newReqs]
      _offset += newReqs.length
    }
    hasMore.value = _offset < (data.value.port?.request_count || 0)
  } catch (e) {
    showToast('加载更多失败', 'error')
  } finally {
    loadingMore.value = false
  }
}

async function loadAll() {
  if (loadingMore.value || !hasMore.value) return
  loadingMore.value = true
  try {
    // Load in batches of 100 (backend max) until all records are fetched
    while (hasMore.value) {
      const result = await api.getPortHistory(portId, 0, 100, _offset)
      const newReqs = result.requests || []
      if (newReqs.length === 0) break
      requests.value = [...requests.value, ...newReqs]
      _offset += newReqs.length
      hasMore.value = _offset < (data.value.port?.request_count || 0)
    }
  } catch (e) {
    showToast('加载全部失败', 'error')
  } finally {
    loadingMore.value = false
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

      // If user is reading (scroll locked), preserve scroll position
      const savedScrollY = scrollLocked.value ? window.scrollY : null

      // Prepend new records
      requests.value = [...newReqs, ...requests.value]
      _offset += newReqs.length
      newCount.value += newReqs.length
      // Update request_count and hasMore
      if (result.port?.request_count !== undefined) {
        data.value.port.request_count = result.port.request_count
      }
      hasMore.value = _offset < (data.value.port?.request_count || 0)

      // Restore scroll position after Vue re-renders
      if (savedScrollY !== null) {
        await nextTick()
        window.scrollTo(0, savedScrollY)
      }

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

function toggleExpand(reqId) {
  expanded.value[reqId] = !expanded.value[reqId]
}

function collapseAll() {
  expanded.value = {}
  headersExpanded.value = {}
}

function openTreeView(req, type) {
  window.open(`/json-viewer/${portId}/${req.id}?type=${type}`, '_blank', 'noopener')
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

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

function getExportFilename(suffix) {
  const port = data.value.port?.port_number || 'unknown'
  const filterLabel = methodFilter.value === 'api' ? '-api' : methodFilter.value === 'other' ? '-other' : ''
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
  return `llm-proxy-port${port}${filterLabel}-${suffix}-${ts}.json`
}

// Use filtered requests for "export JSON only"
function _getExportRequests() {
  return filteredRequests.value
}

function exportJsonOnly() {
  closeCopyMenu()
  try {
    const source = _getExportRequests()
    const output = source.map((r, i) => {
      const entry = { index: i + 1, method: r.method, path: r.path, status_code: r.status_code }
      try { entry.request = JSON.parse(r.request_body) } catch (e) { entry.request = r.request_body }
      try { entry.response = JSON.parse(r.response_body) } catch (e) { entry.response = r.response_body }
      return entry
    })
    downloadJson(getExportFilename('json-only'), output)
    const label = methodFilter.value === 'all' ? '' : ` (${methodFilter.value === 'api' ? '仅API请求' : '仅其他请求'})`
    showToast(`已导出 ${output.length} 条JSON数据${label}`, 'success')
  } catch (e) {
    showToast('导出失败', 'error')
  }
}

async function exportAllData() {
  closeCopyMenu()
  try {
    const exportData = await api.exportPortHistory(portId)
    // If filtered, only include filtered requests in full export too
    if (methodFilter.value !== 'all') {
      const filteredIds = new Set(_getExportRequests().map(r => r.id))
      exportData.requests = (exportData.requests || []).filter(r => filteredIds.has(r.id))
      exportData.total_requests = exportData.requests.length
    }
    downloadJson(getExportFilename('full'), exportData)
    const label = methodFilter.value === 'all' ? '' : ` (${methodFilter.value === 'api' ? '仅API请求' : '仅其他请求'})`
    showToast(`已导出 ${exportData.total_requests} 条完整交互记录${label}`, 'success')
  } catch (e) {
    try {
      const source = _getExportRequests()
      downloadJson(getExportFilename('full'), { port: data.value.port, requests: source, total_requests: source.length })
      showToast('已导出全部数据', 'success')
    } catch (e2) {
      showToast('导出失败', 'error')
    }
  }
}

async function exportApiFromServer() {
  closeCopyMenu()
  try {
    const exportData = await api.exportPortHistory(portId, 'api')
    // Extract only JSON data (request/response bodies), same as exportJsonOnly format
    const output = (exportData.requests || []).map((r, i) => {
      const entry = { index: i + 1, method: r.method, path: r.path, status_code: r.status_code }
      entry.request = typeof r.request_body === 'string' ? (tryParseJson(r.request_body) ?? r.request_body) : r.request_body
      entry.response = typeof r.response_body === 'string' ? (tryParseJson(r.response_body) ?? r.response_body) : r.response_body
      return entry
    })
    downloadJson(getExportFilename('api-json-only'), output)
    showToast(`已从后端导出 ${output.length} 条API请求JSON数据`, 'success')
  } catch (e) {
    showToast('后端导出失败', 'error')
  }
}

function tryParseJson(raw) {
  if (!raw) return null
  try { return JSON.parse(raw) } catch (e) { return null }
}

async function handleDeleteRequest(req) {
  if (!confirm(`确定删除 ${req.method} ${req.path.slice(0, 40)} 这条记录吗？`)) return
  try {
    await api.deleteRequest(portId, req.id)
    const idx = requests.value.findIndex(r => r.id === req.id)
    if (idx !== -1) requests.value.splice(idx, 1)
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
    _offset = 0
    _maxId = 0
    hasMore.value = false
    newCount.value = 0
    if (data.value.port) data.value.port.request_count = 0
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
  // Load config and data in parallel for faster initial render
  const configPromise = api.getConfig().then(cfg => {
    displayIp.value = cfg.display_ip
  }).catch(() => { /* keep default */ })

  await loadData()
  startPolling()

  // Wait for config to finish (non-blocking)
  await configPromise
})

onUnmounted(() => {
  stopPolling()
})
</script>
