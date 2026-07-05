<template>
  <div class="app-container">
    <header v-if="auth.isLoggedIn" class="app-header">
      <div class="flex gap-16" style="align-items:center">
        <h1>🔍 LLM Proxy</h1>
        <nav class="flex gap-16">
          <router-link to="/" class="nav-link">Dashboard</router-link>
          <router-link v-if="auth.isAdmin" to="/admin" class="nav-link-admin">👑 用户管理</router-link>
          <router-link v-if="auth.isAdmin" to="/admin/deleted-ports" class="nav-link-deleted">🗑 已删除代理</router-link>
        </nav>
      </div>
      <div class="header-right">
        <div class="theme-selector">
          <span class="theme-icon">{{ themeIcon }}</span>
          <select v-model="themeValue" @change="onThemeChange" class="theme-select" title="颜色风格">
            <option value="light">☀️ 浅色</option>
            <option value="dark">🌙 深色</option>
            <option value="auto">💻 跟随系统</option>
          </select>
        </div>
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
import { reactive, provide, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'

const auth = useAuthStore()
const router = useRouter()
const themeStore = useThemeStore()
const toast = reactive({ show: false, message: '', type: 'info' })

const themeValue = computed({
  get: () => themeStore.theme,
  set: (val) => themeStore.setTheme(val)
})

const themeIcon = computed(() => {
  const map = { light: '☀️', dark: '🌙', auto: '💻' }
  return map[themeStore.theme] || '💻'
})

function onThemeChange() {
  // setTheme is called via the computed setter
}

onMounted(() => {
  themeStore.initTheme()
})

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
  color: var(--text-muted);
  font-size: 14px;
  padding: 6px 12px;
  border-radius: 6px;
  transition: all 0.2s;
}
.nav-link:hover, .nav-link.router-link-active {
  color: var(--accent);
  background: var(--accent-bg);
}

.nav-link-admin {
  color: var(--color-warning);
  font-size: 14px;
  font-weight: 600;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid var(--color-warning-bg);
  background: var(--color-warning-bg);
  transition: all 0.2s;
}
.nav-link-admin:hover, .nav-link-admin.router-link-active {
  color: var(--btn-warning-hover);
  background: var(--color-warning-bg);
  border-color: var(--color-warning-bg);
}

.nav-link-deleted {
  color: var(--color-danger);
  font-size: 14px;
  padding: 6px 14px;
  border-radius: 6px;
  border: 1px solid var(--color-danger-bg);
  background: var(--color-danger-bg);
  transition: all 0.2s;
}
.nav-link-deleted:hover, .nav-link-deleted.router-link-active {
  color: var(--btn-danger-hover);
  background: var(--color-danger-bg);
  border-color: var(--color-danger-bg);
}

.theme-selector {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-right: 8px;
}

.theme-icon {
  font-size: 14px;
}

.theme-select {
  background: var(--bg-input);
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 12px;
  cursor: pointer;
  outline: none;
  transition: all 0.2s;
}
.theme-select:hover {
  border-color: var(--accent-border);
}
.theme-select:focus {
  border-color: var(--accent);
}
.theme-select option {
  background: var(--bg-card);
  color: var(--text-primary);
}
</style>
