<template>
  <div>
    <div class="tabs">
      <button :class="['tab', tab === 'users' && 'active']" @click="tab = 'users'">👥 用户管理</button>
      <button :class="['tab', tab === 'deleted' && 'active']" @click="tab = 'deleted'; loadDeletedPorts()">🗑 已删除代理</button>
    </div>

    <!-- Users tab -->
    <div v-if="tab === 'users'">
      <div class="card">
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>用户名</th>
                <th>角色</th>
                <th>状态</th>
                <th>注册时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="user in users" :key="user.id">
                <td>{{ user.id }}</td>
                <td>{{ user.username }}</td>
                <td>
                  <span :class="['badge', user.role === 'admin' ? 'badge-active' : 'badge-inactive']">
                    {{ user.role === 'admin' ? '管理员' : '用户' }}
                  </span>
                </td>
                <td>
                  <span :class="['badge', user.is_approved ? 'badge-approved' : 'badge-pending']">
                    {{ user.is_approved ? '已审批' : '待审批' }}
                  </span>
                </td>
                <td class="text-sm text-muted">{{ formatTime(user.created_at) }}</td>
                <td>
                  <div v-if="user.role !== 'admin'" class="flex gap-8">
                    <button v-if="!user.is_approved" class="btn btn-success btn-sm" @click="approveUser(user.id, true)">批准</button>
                    <button v-else class="btn btn-warning btn-sm" @click="approveUser(user.id, false)">取消审批</button>
                    <button class="btn btn-danger btn-sm" @click="handleDelete(user)">删除</button>
                  </div>
                  <span v-else class="text-sm text-muted">—</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Deleted ports tab -->
    <div v-if="tab === 'deleted'">
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
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import api from '../api'

const showToast = inject('showToast')
const tab = ref('users')
const users = ref([])
const deletedPorts = ref([])

async function loadUsers() {
  try {
    const res = await api.listUsers()
    users.value = res.users
  } catch (e) {
    showToast('加载用户列表失败', 'error')
  }
}

async function loadDeletedPorts() {
  try {
    const res = await api.listDeletedPorts()
    deletedPorts.value = res.ports || []
  } catch (e) {
    showToast('加载已删除代理列表失败', 'error')
  }
}

async function approveUser(userId, approved) {
  try {
    await api.approveUser({ user_id: userId, is_approved: approved })
    showToast(approved ? '用户已批准' : '已取消审批', 'success')
    await loadUsers()
  } catch (e) {
    showToast(e.response?.data?.detail || '操作失败', 'error')
  }
}

async function handleDelete(user) {
  if (!confirm(`确定删除用户 "${user.username}" 吗？其所有代理和数据将被清除。`)) return
  try {
    await api.deleteUser(user.id)
    showToast('用户已删除', 'success')
    await loadUsers()
  } catch (e) {
    showToast(e.response?.data?.detail || '删除失败', 'error')
  }
}

function formatTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
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

onMounted(loadUsers)
</script>
