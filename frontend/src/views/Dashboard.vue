<template>
  <div>
    <div class="flex-between mb-16">
      <h2 style="font-size:20px">{{ auth.isAdmin ? '📊 全部代理端口' : '我的代理端口' }}</h2>
      <button class="btn btn-primary" @click="showCreateModal = true">+ 创建新端口</button>
    </div>

    <!-- Ports Table -->
    <div v-if="ports.length > 0" class="card">
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>端口号</th>
              <th>代理地址</th>
              <th>目标地址</th>
              <th>描述</th>
              <th v-if="auth.isAdmin">创建者</th>
              <th>请求数</th>
              <th>状态</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="port in ports" :key="port.id">
              <td>
                <strong style="font-size:16px;color:#5dade2">{{ port.port_number }}</strong>
              </td>
              <td>
                <code style="font-size:12px;color:#aeb6bf;background:#15222b;padding:2px 6px;border-radius:4px">
                  http://{{ displayIp }}:{{ port.port_number }}/v1
                </code>
              </td>
              <td>
                <span style="font-size:13px;word-break:break-all">{{ port.target_url }}</span>
              </td>
              <td>{{ port.description || '-' }}</td>
              <td v-if="auth.isAdmin">
                <span class="badge" style="background:rgba(93,173,226,0.15);color:#5dade2">{{ port.username || '-' }}</span>
              </td>
              <td>{{ port.request_count }}</td>
              <td>
                <span :class="['badge', port.is_active ? 'badge-active' : 'badge-inactive']">
                  {{ port.is_active ? '运行中' : '已停止' }}
                </span>
              </td>
              <td class="text-sm text-muted">{{ formatTime(port.created_at) }}</td>
              <td>
                <div class="flex gap-8">
                  <button class="btn btn-outline btn-sm" @click="$router.push(`/port/${port.id}`)">
                    查看详情
                  </button>
                  <button v-if="port.is_active" class="btn btn-warning btn-sm" @click="handleStop(port)">
                    停止
                  </button>
                  <button v-else class="btn btn-success btn-sm" @click="handleStart(port)">
                    启动
                  </button>
                  <button class="btn btn-danger btn-sm" @click="handleDelete(port)">
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div v-else class="card empty-state">
      <h3>暂无代理端口</h3>
      <p>点击"创建新端口"开始拦截和记录API通信</p>
    </div>

    <!-- Usage Guide -->
    <div class="card mt-24">
      <div class="card-header">📖 使用说明</div>
      <div style="font-size:14px;line-height:1.8;color:#aeb6bf">
        <p>1. 点击 <strong>"创建新端口"</strong>，输入大模型API的目标地址（如 <code>https://api.openai.com</code>）</p>
        <p>2. 系统会自动分配一个端口号（4000-5000）</p>
        <p>3. 在智能体配置中，将大模型网址从 <code>https://xxxx.com/v1</code> 改为 <code>http://{{ displayIp }}:&lt;端口号&gt;/v1</code></p>
        <p>4. 其他配置不变，请求会自动转发到目标地址，同时记录完整的通信内容</p>
        <p>5. 在 <strong>"查看详情"</strong> 页面可以查看、复制和清空交互历史</p>
      </div>
    </div>

    <!-- Create Port Modal -->
    <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
      <div class="modal">
        <h3>创建新代理端口</h3>
        <form @submit.prevent="handleCreate">
          <div class="form-group">
            <label>目标API地址</label>
            <input v-model="createForm.target_url" class="form-input"
                   placeholder="例如: https://api.openai.com  (不要带/v1路径)"
                   required />
          </div>
          <div class="form-group">
            <label>描述（可选）</label>
            <input v-model="createForm.description" class="form-input"
                   placeholder="用于区分不同用途的端口" />
          </div>
          <div v-if="createError" class="form-error">{{ createError }}</div>
          <div class="flex gap-8" style="justify-content:flex-end;margin-top:16px">
            <button type="button" class="btn btn-outline" @click="showCreateModal = false">取消</button>
            <button type="submit" class="btn btn-primary" :disabled="creating">
              {{ creating ? '创建中...' : '创建' }}
            </button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, inject } from 'vue'
import { useAuthStore } from '../stores/auth'
import api from '../api'

const auth = useAuthStore()
const showToast = inject('showToast')
const displayIp = ref('your-server-ip')
const ports = ref([])
const showCreateModal = ref(false)
const createForm = ref({ target_url: '', description: '' })
const createError = ref('')
const creating = ref(false)

async function loadPorts() {
  try {
    ports.value = await api.listPorts()
  } catch (e) {
    showToast('加载端口列表失败', 'error')
  }
}

async function handleCreate() {
  createError.value = ''
  creating.value = true
  try {
    await api.createPort(createForm.value)
    showCreateModal.value = false
    createForm.value = { target_url: '', description: '' }
    showToast('端口创建成功！', 'success')
    await loadPorts()
  } catch (e) {
    createError.value = e.response?.data?.detail || '创建失败'
  } finally {
    creating.value = false
  }
}

async function handleDelete(port) {
  if (!confirm(`确定删除端口 ${port.port_number} 吗？所有历史记录将被清除。`)) return
  try {
    await api.deletePort(port.id)
    showToast('端口已删除', 'success')
    await loadPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '删除失败', 'error')
  }
}

async function handleStop(port) {
  if (!confirm(`确定停止端口 ${port.port_number} 吗？停止后该端口的代理服务将不可用。`)) return
  try {
    await api.stopPort(port.id)
    showToast(`端口 ${port.port_number} 已停止`, 'success')
    await loadPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '停止失败', 'error')
  }
}

async function handleStart(port) {
  try {
    await api.startPort(port.id)
    showToast(`端口 ${port.port_number} 已启动`, 'success')
    await loadPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '启动失败', 'error')
  }
}

async function loadConfig() {
  try {
    const cfg = await api.getConfig()
    displayIp.value = cfg.display_ip
  } catch (e) {
    // keep default
  }
}

function formatTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

onMounted(() => { loadConfig(); loadPorts() })
</script>
