<template>
  <div class="json-viewer-page">
    <div class="json-viewer-header">
      <button class="btn btn-outline btn-sm" @click="window.close()">✕ 关闭</button>
      <h2 style="display:inline;font-size:16px;margin-left:12px">{{ title }}</h2>
    </div>
    <div class="json-viewer-content">
      <JsonTree v-if="jsonData !== null" :data="jsonData" />
      <pre v-else class="json-content" style="padding:20px">{{ rawText }}</pre>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import JsonTree from '../components/JsonTree.vue'

const route = useRoute()
const title = ref('')
const jsonData = ref(null)
const rawText = ref('')

onMounted(() => {
  const key = route.query.key || ''
  const stored = sessionStorage.getItem(key)
  sessionStorage.removeItem(key)

  title.value = decodeURIComponent(route.query.title || 'JSON 查看器')

  if (stored) {
    try {
      jsonData.value = JSON.parse(stored)
      return
    } catch { /* fall through */ }
  }

  // Fallback: try to parse the raw text as JSON
  if (stored) {
    rawText.value = stored
    try { jsonData.value = JSON.parse(stored) } catch { /* show raw text */ }
  } else {
    rawText.value = '(无数据或数据已过期)'
  }
})
</script>

<style scoped>
.json-viewer-page {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #0f1923;
  color: #e0e6ed;
}
.json-viewer-header {
  padding: 12px 20px;
  border-bottom: 1px solid #2c3e50;
  background: #15222b;
  flex-shrink: 0;
}
.json-viewer-content {
  flex: 1;
  overflow: auto;
  padding: 0;
}
</style>
