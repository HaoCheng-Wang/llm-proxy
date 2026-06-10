import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// Attach token
http.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Handle 401
http.interceptors.response.use(
  res => res,
  err => {
    if (err.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('username')
      localStorage.removeItem('role')
      localStorage.removeItem('userId')
      window.location.href = '/login'
    }
    return Promise.reject(err)
  }
)

export default {
  // Auth
  login: (data) => http.post('/auth/login', data).then(r => r.data),
  register: (data) => http.post('/auth/register', data).then(r => r.data),
  getMe: () => http.get('/auth/me').then(r => r.data),
  changePassword: (data) => http.post('/auth/change-password', data).then(r => r.data),

  // Ports
  listPorts: () => http.get('/ports').then(r => r.data),
  createPort: (data) => http.post('/ports', data).then(r => r.data),
  getPortHistory: (portId, sinceId = 0, limit = 20, offset = 0) => {
    const params = {}
    if (sinceId > 0) params.since_id = sinceId
    if (limit !== 20) params.limit = limit
    if (offset > 0) params.offset = offset
    return http.get(`/ports/${portId}`, { params }).then(r => r.data)
  },
  deletePort: (portId) => http.delete(`/ports/${portId}`).then(r => r.data),
  stopPort: (portId) => http.post(`/ports/${portId}/stop`).then(r => r.data),
  startPort: (portId) => http.post(`/ports/${portId}/start`).then(r => r.data),
  updatePort: (portId, data) => http.put(`/ports/${portId}`, data).then(r => r.data),
  clearPortHistory: (portId) => http.delete(`/ports/${portId}/history`).then(r => r.data),
  deleteRequest: (portId, requestId) => http.delete(`/ports/${portId}/history/${requestId}`).then(r => r.data),
  getSingleRequest: (portId, requestId) => http.get(`/ports/${portId}/history/${requestId}`).then(r => r.data),
  exportPortHistory: (portId, methodFilter = 'all') => {
    const params = methodFilter !== 'all' ? { method_filter: methodFilter } : {}
    return http.get(`/ports/${portId}/export`, { params }).then(r => r.data)
  },
  getActivePorts: () => http.get('/ports/active-ports').then(r => r.data),

  // Admin
  listUsers: () => http.get('/admin/users').then(r => r.data),
  approveUser: (data) => http.put('/admin/users/approve', data).then(r => r.data),
  deleteUser: (userId) => http.delete(`/admin/users/${userId}`).then(r => r.data),
  listDeletedPorts: () => http.get('/admin/deleted-ports').then(r => r.data),
  restorePort: (portId) => http.post(`/admin/ports/${portId}/restore`).then(r => r.data),
  permanentDeletePort: (portId) => http.delete(`/admin/ports/${portId}/permanent`).then(r => r.data),

  // Config
  getConfig: () => http.get('/config').then(r => r.data),
}
