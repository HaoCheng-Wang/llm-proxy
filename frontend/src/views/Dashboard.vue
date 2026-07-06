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
                <strong style="font-size:16px;color:var(--accent)">{{ port.port_number }}</strong>
                <span v-if="port.has_api_key" title="已配置自定义 API Key" style="margin-left:4px">🔑</span>
              </td>
              <td>
                <code style="font-size:12px;color:var(--text-secondary);background:var(--bg-card-alt);padding:2px 6px;border-radius:4px">
                  http://{{ displayIp }}:{{ apiPort }}/{{ port.port_number }}
                </code>
              </td>
              <td>
                <span style="font-size:13px;word-break:break-all">{{ port.target_url }}</span>
              </td>
              <td>{{ port.description || '-' }}</td>
              <td v-if="auth.isAdmin">
                <span class="badge" style="background:var(--accent-bg-active);color:var(--accent)">{{ port.username || '-' }}</span>
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
                  <button class="btn btn-outline btn-sm" @click="openEdit(port)" style="color:var(--color-warning);border-color:var(--color-warning-bg)">
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
      <div style="font-size:14px;line-height:1.8;color:var(--text-secondary)">
        <p><strong>第 1 步：创建代理</strong></p>
        <p>点击 <strong>"创建代理"</strong>，输入大模型 API 的目标地址。常见示例：</p>
        <ul style="margin:4px 0 8px 20px">
          <li>OpenAI：<code>https://api.openai.com</code></li>
          <li>Ollama 本地模型：<code>http://localhost:11434</code></li>
          <li>第三方兼容接口：<code>https://your-api-provider.com</code></li>
        </ul>
        <p style="color:var(--color-orange)">⚠️ 注意：只填域名和端口（如 <code>https://api.openai.com</code>），<strong>不要</strong>带 <code>/v1</code> 等路径，路径会在智能体配置中保留。</p>

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
          <div v-if="auth.isAdmin" class="form-group">
            <label>👤 所属用户</label>
            <select v-model="createForm.user_id" class="form-input"
                    :disabled="usersLoading">
              <option :value="null">— 选择用户（默认：自己）—</option>
              <option v-for="u in userList" :key="u.id" :value="u.id"
                      :disabled="!u.is_approved">
                {{ u.username }} ({{ u.role === 'admin' ? '管理员' : u.is_approved ? '已审批' : '待审批' }})
              </option>
            </select>
            <p style="font-size:12px;color:var(--text-muted);margin-top:4px">
              管理员可为其他用户创建代理。默认创建给自己。只能为已审批用户创建。
            </p>
          </div>
          <div class="form-group">
            <label>� 自定义 API Key（可选）</label>
            <input v-model="createForm.api_key" class="form-input" type="password"
                   autocomplete="new-password" name="api-key-override"
                   placeholder="留空则透传智能体原始 Key；填写则替换为本系统配置的 Key" />
            <p style="font-size:12px;color:var(--text-muted);margin-top:4px">配置后，智能体发送的 Authorization / x-api-key 等认证头将被替换为此 Key</p>
          </div>
          <div class="form-group">
            <label>🔗 转发协议 <span style="color:var(--color-danger)">*</span></label>
            <p class="protocol-hint">目标是中转站（如 dmxapi.cn）必须选 HTTP/1.1；直连模型厂商 API 可选 HTTP/2</p>
            <div class="protocol-selector">
              <label class="protocol-option" :class="{ active: createForm.prefer_http2 === false }">
                <input type="radio" v-model="createForm.prefer_http2" :value="false" />
                <div class="protocol-info">
                  <strong>HTTP/1.1</strong> — <span class="protocol-tag-stable">稳定推荐 · 无需担心中断</span>
                  <p class="protocol-desc"><strong>原理</strong>：每个请求独占一条 TCP 连接，上游无法在中途切断。适合中转站（会定期回收连接）、高并发场景。延迟：首次 TLS 握手 ~50ms（有连接池复用后0ms）。</p>
                </div>
              </label>
              <label class="protocol-option" :class="{ active: createForm.prefer_http2 === true }">
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
            <label>编号（5位数字，10000–99999）</label>
            <input v-model.number="editForm.port_number" class="form-input" type="number"
                   min="10000" max="99999"
                   placeholder="修改代理编号（不修改则保持原编号）" />
            <p style="font-size:12px;color:var(--text-muted);margin-top:4px">修改编号后，需使用新编号访问代理。请确保新编号未被其他代理占用。</p>
          </div>
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
            <label>🔑 自定义 API Key</label>
            <div v-if="editForm.has_api_key" style="margin-bottom:8px;display:flex;align-items:center;gap:12px;flex-wrap:wrap">
              <span class="badge badge-active">已配置自定义 Key</span>
              <label style="font-size:13px;cursor:pointer;display:flex;align-items:center;gap:4px">
                <input type="checkbox" v-model="editForm.clear_api_key" />
                清除自定义 Key（恢复透传智能体原始 Key）
              </label>
            </div>
            <span v-else class="badge badge-inactive" style="margin-bottom:8px;display:inline-block">未配置（透传智能体原始 Key）</span>
            <input v-model="editForm.api_key" class="form-input" type="password"
                   autocomplete="new-password" name="api-key-override"
                   :disabled="editForm.clear_api_key"
                   :placeholder="editForm.has_api_key ? '如需修改请输入新的 API Key（留空则保持不变）' : '留空则透传智能体原始 Key；填写则替换'" />
            <p style="font-size:12px;color:var(--text-muted);margin-top:4px">
              {{ editForm.has_api_key ? '已配置自定义 Key。留空保存则保持不变；勾选上方选项可清除。' : '留空则透传智能体原始 Key；填写则替换智能体发送的认证头' }}
            </p>
          </div>
          <div v-if="auth.isAdmin" class="form-group">
            <label>👤 所属用户</label>
            <select v-model="editForm.user_id" class="form-input"
                    :disabled="usersLoading">
              <option :value="null">— 不修改（保持当前用户）—</option>
              <option v-for="u in userList" :key="u.id" :value="u.id"
                      :disabled="!u.is_approved">
                {{ u.username }} ({{ u.role === 'admin' ? '管理员' : u.is_approved ? '已审批' : '待审批' }})
              </option>
            </select>
            <p style="font-size:12px;color:var(--text-muted);margin-top:4px">
              管理员可将代理转移给其他已审批用户。默认不修改。
            </p>
          </div>
          <div class="form-group">
            <label>🔗 转发协议 <span style="color:var(--color-danger)">*</span></label>
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
import { ref, watch, onMounted, inject } from 'vue'
import { useAuthStore } from '../stores/auth'
import api from '../api'

const auth = useAuthStore()
const showToast = inject('showToast')
const displayIp = ref('your-server-ip')
const apiPort = ref(3998)
const ports = ref([])
const showCreateModal = ref(false)
const createForm = ref({ target_url: '', description: '', prefer_http2: false, api_key: '', user_id: null })
const createError = ref('')
const creating = ref(false)
const userList = ref([])
const usersLoading = ref(false)

const showEditModal = ref(false)
const editForm = ref({ id: null, target_url: '', description: '', prefer_http2: null, api_key: '', has_api_key: false, clear_api_key: false })
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
    const payload = {
      target_url: createForm.value.target_url,
      description: createForm.value.description,
      prefer_http2: createForm.value.prefer_http2,
      api_key: createForm.value.api_key || undefined,
    }
    // Only include user_id when admin explicitly selects a different user
    if (auth.isAdmin && createForm.value.user_id !== null && createForm.value.user_id !== undefined) {
      payload.user_id = createForm.value.user_id
    }
    await api.createPort(payload)
    showCreateModal.value = false
    createForm.value = { target_url: '', description: '', prefer_http2: false, api_key: '', user_id: null }
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
    port_number: port.port_number,
    _original_port_number: port.port_number,
    user_id: null,  // admin-only: null = don't change
    _original_user_id: port.user_id || null,
    target_url: port.target_url,
    description: port.description || '',
    prefer_http2: port.prefer_http2,  // null=not set yet, true/false=user picked
    api_key: '',  // always empty — actual value is never sent from backend
    has_api_key: port.has_api_key || false,
    clear_api_key: false,
  }
  showEditModal.value = true
}

async function handleEdit() {
  editError.value = ''
  if (editForm.value.prefer_http2 == null) {
    editError.value = '请选择转发协议（HTTP/1.1 或 HTTP/2）'
    return
  }
  editing.value = true
  try {
    const payload = {
      target_url: editForm.value.target_url,
      description: editForm.value.description,
      prefer_http2: editForm.value.prefer_http2,
    }
    // port_number: only send if user changed it to a valid 5-digit number
    if (editForm.value.port_number !== editForm.value._original_port_number) {
      const pn = editForm.value.port_number
      if (pn === '' || pn === null || pn === undefined || isNaN(Number(pn))) {
        // User cleared the field — treat as "no change"
      } else {
        const numPn = Number(pn)
        if (Number.isInteger(numPn) && numPn >= 10000 && numPn <= 99999) {
          payload.port_number = numPn
        } else {
          editError.value = '编号必须是 10000-99999 范围的整数'
          editing.value = false
          return
        }
      }
    }
    // api_key: only send when user explicitly types a new value or checks "clear"
    if (editForm.value.clear_api_key) {
      payload.api_key = ''  // clear (set to NULL)
    } else if (editForm.value.api_key) {
      payload.api_key = editForm.value.api_key  // override with new value
    }
    // else: don't include api_key → backend treats as None → don't change
    // user_id: only send when admin explicitly selects a different user
    if (auth.isAdmin && editForm.value.user_id !== null && editForm.value.user_id !== undefined) {
      if (editForm.value.user_id !== editForm.value._original_user_id) {
        payload.user_id = editForm.value.user_id
      }
    }
    await api.updatePort(editForm.value.id, payload)
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

async function loadUsersForCreate() {
  // Only admin needs the user list for the create modal
  if (!auth.isAdmin) return
  usersLoading.value = true
  try {
    const res = await api.listUsers()
    userList.value = res.users || []
  } catch (e) {
    // silently ignore — admin can still create for themselves
  } finally {
    usersLoading.value = false
  }
}

// Watch modals: load user list when admin opens create or edit modal
watch([showCreateModal, showEditModal], ([createVal, editVal]) => {
  if ((createVal || editVal) && auth.isAdmin) {
    loadUsersForCreate()
  }
})

function formatTime(t) {
  if (!t) return '-'
  return new Date(t).toLocaleString('zh-CN')
}

onMounted(() => { loadConfig(); loadPorts() })
</script>
