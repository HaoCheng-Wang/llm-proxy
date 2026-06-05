<template>
  <div style="display:flex;flex-direction:column;height:100vh;background:#0f1923;color:#e0e6ed">
    <!-- Header -->
    <div style="display:flex;align-items:center;padding:10px 16px;background:#15222b;border-bottom:1px solid #2c3e50;gap:12px">
      <span style="font-weight:600;font-size:14px;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ title }}</span>
      <span class="method-tag" :style="{ fontSize: '12px', padding: '2px 8px' }" :class="'method-' + (method || 'get').toLowerCase()">{{ method }}</span>
      <span style="font-size:12px;color:#85929e;max-width:400px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{{ path }}</span>
    </div>

    <!-- Content -->
    <div style="flex:1;overflow:auto;padding:0">
      <JsonTree v-if="parsedData !== null" :data="parsedData" />
      <pre v-else style="padding:16px;font-size:13px;line-height:1.6;color:#aeb6bf;white-space:pre-wrap;word-break:break-all">{{ rawText || '(空)' }}</pre>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import JsonTree from '../components/JsonTree.vue'

const title = ref('')
const method = ref('')
const path = ref('')
const parsedData = ref(null)
const rawText = ref('')

onMounted(() => {
  // Try reading from sessionStorage first (set by PortDetail)
  title.value = sessionStorage.getItem('json-popup-title') || ''
  method.value = sessionStorage.getItem('json-popup-method') || ''
  path.value = sessionStorage.getItem('json-popup-path') || ''
  const data = sessionStorage.getItem('json-popup-data') || ''

  if (!data) {
    // Fallback: read from query param (for blocked popup fallback)
    const params = new URLSearchParams(window.location.search)
    title.value = params.get('title') || ''
    const encoded = params.get('data') || ''
    try {
      const raw = decodeURIComponent(encoded)
      try { parsedData.value = JSON.parse(raw) } catch { rawText.value = raw }
    } catch { rawText.value = encoded }
  } else {
    try { parsedData.value = JSON.parse(data) } catch { rawText.value = data }
  }

  // Clean up sessionStorage
  sessionStorage.removeItem('json-popup-title')
  sessionStorage.removeItem('json-popup-data')
  sessionStorage.removeItem('json-popup-method')
  sessionStorage.removeItem('json-popup-path')
})
</script>
