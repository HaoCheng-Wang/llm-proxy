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
  // Streaming NDJSON — yields {port} then records one by one.
  // onRecord(record) is called for each record as it arrives.
  // Returns Promise<{port, requests}> when the stream is complete.
  getPortHistoryStream: async (portId, sinceId = 0, limit = 20, offset = 0, onRecord = null) => {
    const params = new URLSearchParams()
    if (sinceId > 0) params.set('since_id', sinceId)
    if (limit !== 20) params.set('limit', limit)
    if (offset > 0) params.set('offset', offset)
    const qs = params.toString()
    const url = `/api/ports/${portId}${qs ? '?' + qs : ''}`
    const token = localStorage.getItem('token')
    const headers = token ? { Authorization: `Bearer ${token}` } : {}

    const response = await fetch(url, { headers })
    if (response.status === 401) {
      localStorage.removeItem('token'); localStorage.removeItem('username')
      localStorage.removeItem('role'); localStorage.removeItem('userId')
      window.location.href = '/login'
      throw new Error('Unauthorized')
    }
    if (!response.ok) {
      const text = await response.text()
      throw new Error(text || `HTTP ${response.status}`)
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let port = null
    const requests = []

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()  // keep incomplete last chunk
      for (const line of lines) {
        if (!line.trim()) continue
        try {
          const obj = JSON.parse(line)
          if (port === null) {
            port = obj  // first line = port metadata
          } else {
            requests.push(obj)
            if (onRecord) onRecord(obj)  // incremental render
          }
        } catch (e) {
          console.warn('[stream] Failed to parse NDJSON line:', line.slice(0, 200), e)
        }
      }
    }
    if (buffer.trim()) {
      try {
        const obj = JSON.parse(buffer)
        if (port === null) {
          // Trailing buffer is the port metadata line (should not happen,
          // but handle gracefully in case stream order is unexpected).
          port = obj
        } else {
          requests.push(obj)
          if (onRecord) onRecord(obj)
        }
      } catch (e) {
        console.warn("[stream] Failed to parse trailing NDJSON line:", buffer.slice(0, 200), e)
      }
    }
    return { port, requests }
  },
  deletePort: (portId) => http.delete(`/ports/${portId}`).then(r => r.data),
  stopPort: (portId) => http.post(`/ports/${portId}/stop`).then(r => r.data),
  startPort: (portId) => http.post(`/ports/${portId}/start`).then(r => r.data),
  updatePort: (portId, data) => http.put(`/ports/${portId}`, data).then(r => r.data),
  clearPortHistory: (portId) => http.delete(`/ports/${portId}/history`).then(r => r.data),
  deleteRequest: (portId, requestId) => http.delete(`/ports/${portId}/history/${requestId}`).then(r => r.data),
  getSingleRequest: (portId, requestId) => http.get(`/ports/${portId}/history/${requestId}`).then(r => r.data),
  getRawSse: (portId, requestId) => http.get(`/ports/${portId}/history/${requestId}/raw-sse`).then(r => r.data),
  // 获取一次性下载 ticket（用于浏览器原生下载，避免 JWT 出现在 URL 中）
  createExportTicket: (portId) => http.post(`/ports/${portId}/export-ticket`).then(r => r.data),
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
