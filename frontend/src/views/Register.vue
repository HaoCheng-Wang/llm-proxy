<template>
  <div class="auth-page">
    <div class="auth-card">
      <h2>🔍 注册新账号</h2>
      <form @submit.prevent="handleRegister">
        <div class="form-group">
          <label>用户名</label>
          <input v-model="form.username" class="form-input" placeholder="2-50个字符" required />
        </div>
        <div class="form-group">
          <label>密码</label>
          <input v-model="form.password" class="form-input" type="password" placeholder="至少4个字符" required />
        </div>
        <div v-if="error" class="form-error">{{ error }}</div>
        <div v-if="success" style="color:var(--color-success);font-size:13px;margin-top:8px">{{ success }}</div>
        <button class="btn btn-primary" style="width:100%;margin-top:12px" :disabled="loading">
          {{ loading ? '提交中...' : '注册' }}
        </button>
      </form>
      <div class="auth-link">
        已有账号？<router-link to="/login">返回登录</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

const form = ref({ username: '', password: '' })
const error = ref('')
const success = ref('')
const loading = ref(false)

async function handleRegister() {
  error.value = ''
  success.value = ''
  loading.value = true
  try {
    await auth.register(form.value)
    // Auto-login after registration — the backend decides whether
    // approval is required. If login succeeds, the user is approved
    // (or approval is disabled); if 403, they need to wait.
    try {
      await auth.login(form.value)
      form.value = { username: '', password: '' }
      router.push('/')
    } catch (loginErr) {
      form.value = { username: '', password: '' }
      if (loginErr.response?.status === 403) {
        success.value = '注册成功！请等待管理员审批后登录。'
      } else {
        // Login failed for another reason — go to login page
        success.value = '注册成功！请登录。'
        router.push('/login')
      }
    }
  } catch (e) {
    error.value = e.response?.data?.detail || '注册失败'
  } finally {
    loading.value = false
  }
}
</script>
