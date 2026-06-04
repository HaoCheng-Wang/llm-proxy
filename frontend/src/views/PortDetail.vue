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

      <DynamicScroller
        :items="requests"
        :min-item-size="60"
        class="virtual-scroller"
        key-field="id"
      >
        <template v-slot="{ item, index, active }">
          <DynamicScrollerItem
            :item="item"
            :active="active"
            :data-index="index"
          >
            <RequestCard
              :req="item"
              :is-expanded="!!expanded[index]"
              @toggle="toggleExpandByReq"
              @delete="handleDeleteRequest"
            />
          </DynamicScrollerItem>
        </template>
      </DynamicScroller>
    </div>

    <div v-else class="card empty-state">
      <h3>暂无交互记录</h3>
      <p>配置智能体连接到该端口后，所有API通信将在此显示</p>
    </div>

    <!-- Load More -->
    <div v-if="hasMore" style="text-align:center;margin-top:16px">
      <button class="btn btn-outline" @click="loadMore" :disabled="loadingMore">
        {{ loadingMore ? '加载中...' : `加载更多 (${data.port.request_count - requests.length} 条剩余)` }}
      </button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onUnmounted, inject, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { DynamicScroller, DynamicScrollerItem } from 'vue-virtual-scroller'
import api from '../api'
import RequestCard from '../components/RequestCard.vue'

const route = useRoute()
const showToast = inject('showToast')
const displayIp = ref('your-server-ip')
const portId = route.params.id

const data = ref({ port: null, requests: [] })
const requests = ref([])
const expanded = ref({})
const copyMenuOpen = ref(false)
const newCount = ref(0)
const polling = ref(false)
const loadingMore = ref(false)
const hasMore = ref(false)

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

async function pollNewRecords() {
  if (!data.value.port?.is_active) return
  polling.value = true
  try {
    const result = await api.getPortHistory(portId, _maxId)
    const newReqs = result.requests || []
    if (newReqs.length > 0) {
      // Get the highest new id
      _maxId = Math.max(_maxId, ...newReqs.map(r => r.id))
      // Prepend new records
      requests.value = [...newReqs, ...requests.value]
      _offset += newReqs.length
      newCount.value += newReqs.length
      // Update request_count and hasMore
      if (result.port?.request_count !== undefined) {
        data.value.port.request_count = result.port.request_count
      }
      hasMore.value = _offset < (data.value.port?.request_count || 0)
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

function toggleExpand(index) {
  expanded.value[index] = !expanded.value[index]
}

function toggleExpandByReq(req) {
  const index = requests.value.findIndex(r => r.id === req.id)
  if (index !== -1) {
    toggleExpand(index)
  }
}

function expandAll() {
  if (requests.value.length > 5) {
    if (!confirm(`确定要展开全部 ${requests.value.length} 条记录吗？大量展开可能导致页面卡顿。`)) {
      return
    }
  }
  const next = {}
  requests.value.forEach((_, i) => { next[i] = true })
  expanded.value = next
}

function collapseAll() {
  expanded.value = {}
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

async function handleDeleteRequest(req) {
  if (!confirm(`确定删除 ${req.method} ${req.path.slice(0, 40)} 这条记录吗？`)) return
  try {
    await api.deleteRequest(portId, req.id)
    const index = requests.value.findIndex(r => r.id === req.id)
    if (index !== -1) {
      requests.value.splice(index, 1)
      // Rebuild expanded map since indices shifted
      const newExpanded = {}
      Object.keys(expanded.value).forEach(k => {
        const i = parseInt(k)
        if (i < index) newExpanded[i] = expanded.value[i]
        else if (i > index) newExpanded[i - 1] = expanded.value[i]
      })
      expanded.value = newExpanded
    }
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

<style scoped>
.virtual-scroller {
  max-height: calc(100vh - 280px);
  overflow-y: auto;
}
</style>
