<template>
  <div>
    <h2 style="font-size:20px;margin-bottom:16px">🗑 已删除代理</h2>

    <div v-if="deletedPorts.length > 0" class="card">
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>编号</th>
              <th>目标地址</th>
              <th>创建者</th>
              <th>请求数</th>
              <th>删除时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="port in deletedPorts" :key="port.id">
              <td><strong style="color:#95a5a6">{{ port.port_number }}</strong></td>
              <td><span style="font-size:13px;word-break:break-all">{{ port.target_url }}</span></td>
              <td>{{ port.username || '-' }}</td>
              <td>{{ port.request_count }}</td>
              <td class="text-sm text-muted">{{ formatTime(port.deleted_at) }}</td>
              <td>
                <div class="flex gap-8">
                  <button class="btn btn-outline btn-sm" @click="$router.push(`/port/${port.id}`)">
                    查看详情
                  </button>
                  <button class="btn btn-success btn-sm" @click="restorePort(port)">恢复</button>
                  <button class="btn btn-danger btn-sm" @click="permanentDelete(port)">彻底删除</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-else class="card empty-state">
      <h3>📭 无已删除代理</h3>
      <p>用户删除的代理会出现在这里，可恢复或彻底清除。</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import api from '../api'

const showToast = inject('showToast')
const deletedPorts = ref([])

async function loadDeletedPorts() {
  try {
    const res = await api.listDeletedPorts()
    deletedPorts.value = res.ports || []
  } catch (e) {
    showToast('加载已删除代理列表失败', 'error')
  }
}

async function restorePort(port) {
  if (!confirm(`确定恢复代理 ${port.port_number} 吗？恢复后为停用状态。`)) return
  try {
    await api.restorePort(port.id)
    showToast(`代理 ${port.port_number} 已恢复`, 'success')
    await loadDeletedPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '恢复失败', 'error')
  }
}

async function permanentDelete(port) {
  if (!confirm(`⚠️ 确定彻底删除代理 ${port.port_number} 吗？\n\n所有历史记录将被永久清除，不可恢复！`)) return
  try {
    await api.permanentDeletePort(port.id)
    showToast(`代理 ${port.port_number} 已彻底删除`, 'success')
    await loadDeletedPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '删除失败', 'error')
  }
}

function formatTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

onMounted(loadDeletedPorts)
</script>
