<template>
  <div>
    <h2 style="font-size:20px;margin-bottom:16px">👑 用户管理</h2>

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
                  <button v-if="!user.is_approved" class="btn btn-success btn-sm" @click="approveUser(user.id, true)">
                    批准
                  </button>
                  <button v-else class="btn btn-warning btn-sm" @click="approveUser(user.id, false)">
                    取消审批
                  </button>
                  <button class="btn btn-danger btn-sm" @click="handleDelete(user)">
                    删除
                  </button>
                </div>
                <span v-else class="text-sm text-muted">—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import api from '../api'

const showToast = inject('showToast')
const users = ref([])

async function loadUsers() {
  try {
    const res = await api.listUsers()
    users.value = res.users
  } catch (e) {
    showToast('加载用户列表失败', 'error')
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
  if (!confirm(`确定删除用户 "${user.username}" 吗？其所有端口和数据将被清除。`)) return
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

onMounted(loadUsers)
</script>
