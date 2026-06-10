<template>
  <div>
    <div class="flex-between mb-16">
      <h2 style="font-size:20px">{{ auth.isAdmin ? '📊 全部代理' : '我的代理' }}</h2>
      <button class="btn btn-primary" @click="showCreateModal = true">+ 创建代理</button>
    </div>

    <!-- Ports Table -->
    <div v-if="ports.length > 0" class="card">
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>编号</th>
              <th>代理地址</th>
              <th>目标地址</th>
              <th>描述</th>
              <th v-if="auth.isAdmin">创建者</th>
              <th>请求数</th>
              <th>状态</th>
              <th>协议</th>
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
                  http://{{ displayIp }}:{{ apiPort }}/{{ port.port_number }}
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
              <td>
                <span :class="['badge', port.prefer_http2 ? 'badge-http2' : 'badge-http11']"
                      :title="port.prefer_http2 ? 'HTTP/2 多路复用 — 低延迟但有流中断风险，适合直连模型 API' : 'HTTP/1.1 独立连接 — 最稳定，适合中转站 / 高并发场景'">
                  {{ port.prefer_http2 ? 'HTTP/2' : 'HTTP/1.1' }}
                </span>
              </td>
              <td class="text-sm text-muted">{{ formatTime(port.created_at) }}</td>
              <td>
                <div class="flex gap-8">
                  <button class="btn btn-outline btn-sm" @click="$router.push(`/port/${port.id}`)">
                    查看详情
                  </button>
                  <button class="btn btn-outline btn-sm" @click="openEdit(port)" style="color:#f39c12;border-color:rgba(243,156,18,0.4)">
                    编辑
                  </button>
                  <button v-if="port.is_active" class="btn btn-warning btn-sm" @click="handleStop(port)">
                    停用
                  </button>
                  <button v-else class="btn btn-success btn-sm" @click="handleStart(port)">
                    启用
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
      <h3>暂无代理</h3>
      <p>点击"创建代理"开始拦截和记录API通信</p>
    </div>

    <!-- Usage Guide -->
    <div class="card mt-24">
      <div class="card-header">📖 使用说明</div>
      <div style="font-size:14px;line-height:1.8;color:#aeb6bf">
        <p><strong>第 1 步：创建代理</strong></p>
        <p>点击 <strong>"创建代理"</strong>，输入大模型 API 的目标地址。常见示例：</p>
        <ul style="margin:4px 0 8px 20px">
          <li>OpenAI：<code>https://api.openai.com</code></li>
          <li>Ollama 本地模型：<code>http://localhost:11434</code></li>
          <li>第三方兼容接口：<code>https://your-api-provider.com</code></li>
        </ul>
        <p style="color:#e67e22">⚠️ 注意：只填域名和端口（如 <code>https://api.openai.com</code>），<strong>不要</strong>带 <code>/v1</code> 等路径，路径会在智能体配置中保留。</p>

        <p><strong>第 2 步：获取分配的代理编号</strong></p>
        <p>系统自动分配一个 5 位随机编号。此编号将放在 URL 路径中访问（例如 <code>http://{{ displayIp }}:{{ apiPort }}/12345/...</code>）。</p>

        <p><strong>第 3 步：修改智能体的 API 地址</strong></p>
        <p>在你的智能体（Agent）或客户端配置中，将大模型 API 的 Base URL 改为代理地址。<strong>路径部分保持不变</strong>，只需替换域名和端口。编号放在路径中：</p>
        <ul style="margin:4px 0 8px 20px">
          <li>原来：<code>https://api.openai.com/v1</code> → 改为：<code>http://{{ displayIp }}:{{ apiPort }}/&lt;编号&gt;/v1</code></li>
          <li>原来：<code>https://api.openai.com/v1/chat</code> → 改为：<code>http://{{ displayIp }}:{{ apiPort }}/&lt;编号&gt;/v1/chat</code></li>
          <li>原来：<code>http://localhost:11434/v1</code> → 改为：<code>http://{{ displayIp }}:{{ apiPort }}/&lt;编号&gt;/v1</code></li>
        </ul>
        <p>API Key 等其他配置保持不变。只需修改 Base URL，智能体代码无需其他改动。</p>

        <p><strong>第 4 步：开始使用</strong></p>
        <p>智能体发出的所有请求会自动转发到目标地址，同时系统会完整记录请求头/体、响应头/体、状态码和耗时。支持流式（SSE）和非流式请求。</p>

        <p><strong>第 5 步：查看交互记录</strong></p>
        <p>点击 <strong>"查看详情"</strong> 进入代理详情页，可以：</p>
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
        <h3>创建新代理</h3>
        <form @submit.prevent="handleCreate">
          <div class="form-group">
            <label>目标API地址</label>
            <input v-model="createForm.target_url" class="form-input"
                   placeholder="例如: https://api.openai.com  (只填域名，不带路径)"
                   required />
          </div>
          <div class="form-group">
            <label>描述（可选）</label>
            <input v-model="createForm.description" class="form-input"
                   placeholder="用于区分不同用途的代理" />
          </div>
          <div class="form-group">
            <label>🔗 转发协议 <span style="color:#e74c3c">*</span></label>
            <p class="protocol-hint">目标是中转站（如 dmxapi.cn）必须选 HTTP/1.1；直连模型厂商 API 可选 HTTP/2</p>
            <div class="protocol-selector">
              <label class="protocol-option" :class="{ active: !createForm.prefer_http2 }">
                <input type="radio" v-model="createForm.prefer_http2" :value="false" />
                <div class="protocol-info">
                  <strong>HTTP/1.1</strong> — <span class="protocol-tag-stable">稳定推荐 · 无需担心中断</span>
                  <p class="protocol-desc"><strong>原理</strong>：每个请求独占一条 TCP 连接，上游无法在中途切断。适合中转站（会定期回收连接）、高并发场景。延迟：首次 TLS 握手 ~50ms（有连接池复用后0ms）。</p>
                </div>
              </label>
              <label class="protocol-option" :class="{ active: createForm.prefer_http2 }">
                <input type="radio" v-model="createForm.prefer_http2" :value="true" />
                <div class="protocol-info">
                  <strong>HTTP/2</strong> — <span class="protocol-tag-risk">低延迟 · 中转站有中断风险</span>
                  <p class="protocol-desc"><strong>原理</strong>：多条请求复用一条 TCP 连接，省 TLS 握手。但上游回收连接时，该连接上<strong>所有正在传输的 SSE 流会同时中断</strong>——数据已发给用户、无法重试。<strong>仅适合直连 OpenAI/Anthropic/Google 等不会激进回收连接的 API。</strong></p>
                </div>
              </label>
            </div>
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

    <!-- Edit Port Modal -->
    <div v-if="showEditModal" class="modal-overlay" @click.self="showEditModal = false">
      <div class="modal">
        <h3>编辑代理配置</h3>
        <form @submit.prevent="handleEdit">
          <div class="form-group">
            <label>目标API地址</label>
            <input v-model="editForm.target_url" class="form-input"
                   placeholder="例如: https://api.openai.com"
                   required />
          </div>
          <div class="form-group">
            <label>描述</label>
            <input v-model="editForm.description" class="form-input"
                   placeholder="用于区分不同用途的代理" />
          </div>
          <div class="form-group">
            <label>🔗 转发协议 <span style="color:#e74c3c">*</span></label>
            <p class="protocol-hint">目标是中转站（如 dmxapi.cn）必须选 HTTP/1.1；直连模型厂商 API 可选 HTTP/2</p>
            <div class="protocol-selector">
              <label class="protocol-option" :class="{ active: editForm.prefer_http2 === false }">
                <input type="radio" v-model="editForm.prefer_http2" :value="false" />
                <div class="protocol-info">
                  <strong>HTTP/1.1</strong> — <span class="protocol-tag-stable">中转站首选</span>
                  <p class="protocol-desc">每个请求独占一条 TCP 连接，上游无法在中途切断。中转站定期回收连接时不影响正在传输的流。</p>
                </div>
              </label>
              <label class="protocol-option" :class="{ active: editForm.prefer_http2 === true }">
                <input type="radio" v-model="editForm.prefer_http2" :value="true" />
                <div class="protocol-info">
                  <strong>HTTP/2</strong> — <span class="protocol-tag-risk">⚠️ 中转站会中断流</span>
                  <p class="protocol-desc">多路复用省 TLS 握手，但中转站回收连接时该连接上<strong>所有正在传输的 SSE 流会同时中断</strong>。仅适合直连模型厂商。</p>
                </div>
              </label>
            </div>
          </div>
          <div v-if="editError" class="form-error">{{ editError }}</div>
          <div class="flex gap-8" style="justify-content:flex-end;margin-top:16px">
            <button type="button" class="btn btn-outline" @click="showEditModal = false">取消</button>
            <button type="submit" class="btn btn-primary" :disabled="editing">
              {{ editing ? '保存中...' : '保存' }}
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
const apiPort = ref(3998)
const ports = ref([])
const showCreateModal = ref(false)
const createForm = ref({ target_url: '', description: '', prefer_http2: false })
const createError = ref('')
const creating = ref(false)

const showEditModal = ref(false)
const editForm = ref({ id: null, target_url: '', description: '', prefer_http2: false })
const editError = ref('')
const editing = ref(false)

async function loadPorts() {
  try {
    ports.value = await api.listPorts()
  } catch (e) {
    showToast('加载代理列表失败', 'error')
  }
}

async function handleCreate() {
  createError.value = ''
  creating.value = true
  try {
    await api.createPort(createForm.value)
    showCreateModal.value = false
    createForm.value = { target_url: '', description: '', prefer_http2: false }
    showToast('代理创建成功！', 'success')
    await loadPorts()
  } catch (e) {
    createError.value = e.response?.data?.detail || '创建失败'
  } finally {
    creating.value = false
  }
}

async function handleDelete(port) {
  if (!confirm(`确定删除代理 ${port.port_number} 吗？所有历史记录将被清除。`)) return
  try {
    await api.deletePort(port.id)
    showToast('代理已删除', 'success')
    await loadPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '删除失败', 'error')
  }
}

async function handleStop(port) {
  if (!confirm(`确定停用代理 ${port.port_number} 吗？停用后将无法通过此代理访问目标 API。`)) return
  try {
    await api.stopPort(port.id)
    showToast(`代理 ${port.port_number} 已停用`, 'success')
    await loadPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '停用失败', 'error')
  }
}

async function handleStart(port) {
  try {
    await api.startPort(port.id)
    showToast(`代理 ${port.port_number} 已启用`, 'success')
    await loadPorts()
  } catch (e) {
    showToast(e.response?.data?.detail || '启用失败', 'error')
  }
}

function openEdit(port) {
  editError.value = ''
  editForm.value = {
    id: port.id,
    target_url: port.target_url,
    description: port.description || '',
    prefer_http2: port.prefer_http2 ?? false,
  }
  showEditModal.value = true
}

async function handleEdit() {
  editError.value = ''
  if (editForm.value.prefer_http2 == null) {
    editError.value = '请选择转发协议'
    return
  }
  editing.value = true
  try {
    await api.updatePort(editForm.value.id, {
      target_url: editForm.value.target_url,
      description: editForm.value.description,
      prefer_http2: editForm.value.prefer_http2,
    })
    showEditModal.value = false
    showToast('代理配置已更新', 'success')
    await loadPorts()
  } catch (e) {
    editError.value = e.response?.data?.detail || '保存失败'
  } finally {
    editing.value = false
  }
}

async function loadConfig() {
  try {
    const cfg = await api.getConfig()
    displayIp.value = cfg.display_ip
    apiPort.value = cfg.api_port || 3998
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
