import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import api from '../api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const username = ref(localStorage.getItem('username') || '')
  const role = ref(localStorage.getItem('role') || '')
  const userId = ref(parseInt(localStorage.getItem('userId') || '0'))

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => role.value === 'admin')

  async function login(data) {
    const res = await api.login(data)
    token.value = res.access_token
    username.value = res.username
    role.value = res.role
    userId.value = res.user_id
    localStorage.setItem('token', res.access_token)
    localStorage.setItem('username', res.username)
    localStorage.setItem('role', res.role)
    localStorage.setItem('userId', res.user_id)
    return res
  }

  async function register(data) {
    return await api.register(data)
  }

  function logout() {
    token.value = ''
    username.value = ''
    role.value = ''
    userId.value = 0
    localStorage.removeItem('token')
    localStorage.removeItem('username')
    localStorage.removeItem('role')
    localStorage.removeItem('userId')
  }

  return { token, username, role, userId, isLoggedIn, isAdmin, login, register, logout }
})
