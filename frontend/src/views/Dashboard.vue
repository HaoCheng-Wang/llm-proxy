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
        <p><strong>第 1 步：创建代理端口</strong></p>
        <p>点击 <strong>"创建新端口"</strong>，输入大模型 API 的目标地址。常见示例：</p>
        <ul style="margin:4px 0 8px 20px">
          <li>OpenAI：<code>https://api.openai.com</code></li>
          <li>Ollama 本地模型：<code>http://localhost:11434</code></li>
          <li>第三方兼容接口：<code>https://your-api-provider.com</code></li>
        </ul>
        <p style="color:#e67e22">⚠️ 注意：只填域名和端口，<strong>不要</strong>带 <code>/v1</code> 路径，系统会自动拼接。</p>

        <p><strong>第 2 步：获取分配的端口号</strong></p>
        <p>系统会在 4000–5000 范围内自动分配一个空闲端口号，每个用户最多可创建多个端口（上限可通过环境变量配置）。</p>

        <p><strong>第 3 步：修改智能体的 API 地址</strong></p>
        <p>在你的智能体（Agent）或客户端配置中，将大模型 API 地址改为代理地址：</p>
        <ul style="margin:4px 0 8px 20px">
          <li>原来：<code>https://api.openai.com/v1</code> 或 <code>http://localhost:11434/v1</code></li>
          <li>改为：<code>http://{{ displayIp }}:&lt;端口号&gt;/v1</code></li>
        </ul>
        <p>API Key 等其他配置保持不变。如果你的客户端支持自定义 Base URL，只需修改 Base URL 即可。</p>

        <p><strong>第 4 步：开始使用</strong></p>
        <p>智能体发出的所有请求会自动转发到目标地址，同时系统会完整记录请求头/体、响应头/体、状态码和耗时。支持流式（SSE）和非流式请求。</p>

        <p><strong>第 5 步：查看交互记录</strong></p>
        <p>点击 <strong>"查看详情"</strong> 进入端口详情页，可以：</p>
        <ul style="margin:4px 0 0 20px">
          <li>实时查看所有交互记录（每 2 秒自动刷新）</li>
          <li>展开单条记录查看完整的请求和响应 JSON</li>
          <li>切换「纯文本」或「树形查看」模式</li>
          <li>一键复制 JSON 数据或完整交互内容</li>
          <li>清空历史记录或删除单条记录</li>
        </ul>
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
