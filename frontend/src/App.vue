<template>
  <div class="app-container">
    <header v-if="auth.isLoggedIn" class="app-header">
      <div class="flex gap-16" style="align-items:center">
        <h1>🔍 LLM Proxy</h1>
        <nav class="flex gap-16">
          <router-link to="/" class="nav-link">Dashboard</router-link>
          <router-link v-if="auth.isAdmin" to="/admin" class="nav-link-admin">👑 用户管理</router-link>
        </nav>
      </div>
      <div class="header-right">
        <router-link to="/change-password" class="nav-link" style="margin-right:8px">修改密码</router-link>
        <span class="username">{{ auth.username }}</span>
        <span v-if="auth.isAdmin" class="badge badge-active">管理员</span>
        <button class="btn btn-outline btn-sm" @click="handleLogout">退出</button>
      </div>
    </header>

    <main :class="auth.isLoggedIn ? 'app-main' : ''">
      <router-view />
    </main>

    <div v-if="toast.show" :class="['toast', `toast-${toast.type}`]" @click="toast.show = false">
      {{ toast.message }}
    </div>
  </div>
</template>

<script setup>
import { reactive, provide } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
const router = useRouter()
const toast = reactive({ show: false, message: '', type: 'info' })

function showToast(message, type = 'info') {
  toast.message = message
  toast.type = type
  toast.show = true
  setTimeout(() => { toast.show = false }, 3000)
}

provide('showToast', showToast)

function handleLogout() {
  auth.logout()
  router.push('/login')
}
</script>

<style scoped>
.nav-link {
  color: #85929e;
  font-size: 14px;
  padding: 6px 12px;
  border-radius: 6px;
  transition: all 0.2s;
}
.nav-link:hover, .nav-link.router-link-active {
  color: #5dade2;
  background: rgba(93, 173, 226, 0.08);
}

.nav-link-admin {
  color: #f39c12;
  font-size: 14px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid rgba(243, 156, 18, 0.3);
  background: rgba(243, 156, 18, 0.08);
  transition: all 0.2s;
}
.nav-link-admin:hover, .nav-link-admin.router-link-active {
  color: #f1c40f;
  background: rgba(243, 156, 18, 0.18);
  border-color: rgba(241, 196, 15, 0.5);
}
</style>
