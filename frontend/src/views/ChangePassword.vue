<template>
  <div class="auth-page">
    <div class="auth-card">
      <h2>🔑 修改密码</h2>
      <form @submit.prevent="handleChange">
        <div class="form-group">
          <label>当前密码</label>
          <input v-model="form.old_password" class="form-input" type="password" placeholder="请输入当前密码" required />
        </div>
        <div class="form-group">
          <label>新密码</label>
          <input v-model="form.new_password" class="form-input" type="password" placeholder="请输入新密码" required />
        </div>
        <div class="form-group">
          <label>确认新密码</label>
          <input v-model="newPasswordConfirm" class="form-input" type="password" placeholder="再次输入新密码" required />
        </div>
        <div v-if="error" class="form-error">{{ error }}</div>
        <div v-if="success" class="form-success">{{ success }}</div>
        <button class="btn btn-primary" style="width:100%;margin-top:12px" :disabled="loading">
          {{ loading ? '修改中...' : '确认修改' }}
        </button>
      </form>
      <div class="auth-link">
        <router-link to="/">← 返回首页</router-link>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()

const form = ref({ old_password: '', new_password: '' })
const newPasswordConfirm = ref('')
const error = ref('')
const success = ref('')
const loading = ref(false)

async function handleChange() {
  error.value = ''
  success.value = ''

  if (form.value.new_password !== newPasswordConfirm.value) {
    error.value = '两次输入的新密码不一致'
    return
  }


  loading.value = true
  try {
    await api.changePassword(form.value)
    success.value = '密码修改成功！3 秒后返回首页...'
    setTimeout(() => {
      router.push('/')
    }, 3000)
  } catch (e) {
    error.value = e.response?.data?.detail || '修改失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.form-success {
  background: rgba(39, 174, 96, 0.1);
  border: 1px solid #27ae60;
  color: #27ae60;
  padding: 10px;
  border-radius: 6px;
  margin-top: 12px;
  font-size: 14px;
}
</style>
