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
        <div v-if="success" style="color:#2ecc71;font-size:13px;margin-top:8px">{{ success }}</div>
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
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

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
    success.value = '注册成功！请等待管理员审批后登录。'
    form.value = { username: '', password: '' }
  } catch (e) {
    error.value = e.response?.data?.detail || '注册失败'
  } finally {
    loading.value = false
  }
}
</script>
