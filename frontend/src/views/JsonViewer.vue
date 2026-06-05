<template>
  <div class="json-viewer-page">
    <div class="json-viewer-header">
      <h2 style="font-size:18px;margin:0">{{ title }}</h2>
      <button class="btn btn-outline btn-sm" @click="window.close()">✕ 关闭</button>
    </div>
    <div class="json-viewer-content">
      <JsonTree v-if="parsed" :data="parsed" />
      <pre v-else class="json-fallback">{{ rawData }}</pre>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import JsonTree from '../components/JsonTree.vue'

const title = ref('JSON 查看器')
const rawData = ref('')
const parsed = ref(null)

onMounted(() => {
  title.value = sessionStorage.getItem('jsonViewerTitle') || 'JSON 查看器'
  rawData.value = sessionStorage.getItem('jsonViewerData') || ''
  try {
    parsed.value = JSON.parse(rawData.value)
  } catch {
    parsed.value = null
  }
})
</script>

<style scoped>
.json-viewer-page {
  min-height: 100vh;
  background: #0f1923;
  color: #e0e6ed;
  display: flex;
  flex-direction: column;
}
.json-viewer-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: #15222b;
  border-bottom: 1px solid #2c3e50;
}
.json-viewer-content {
  flex: 1;
  padding: 16px 24px;
  overflow: auto;
}
.json-fallback {
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 13px;
  line-height: 1.6;
  color: #aeb6bf;
}
</style>
