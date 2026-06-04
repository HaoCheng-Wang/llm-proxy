# LLM Proxy — 智能体与大模型 API 通信拦截记录系统

截获和记录智能体与大模型 API 之间 HTTP 通信的代理系统。支持流式/非流式请求、SSE 重组为完整 JSON、JSON 树形查看、实时刷新、多用户隔离。

## 功能

- **多端口代理** (4000–5000)：每用户端口数可通过 `MAX_PORTS_PER_USER` 环境变量配置（默认 10），被占端口自动跳过
- **全量截获**：请求头/体、响应头/体、状态码、耗时，流式 SSE 自动重组为完整 JSON，原始 SSE 文本保留
- **实时刷新**：前端每 2 秒轮询，新交互即时出现，无需手动刷新
- **JSON 树形查看**：折叠/展开、搜索过滤、请求与响应并排展示
- **一键复制**：可选仅复制 JSON 数据，或完整交互含 HTTP 头
- **用户系统**：注册→管理员审批→登录，数据按用户隔离
- **管理员面板**：查看全部端口及创建者，审批/删除用户

## 关于 uv（Python 包管理器）

本项目**可选**使用 uv 管理依赖。不使用 uv 也能直接运行。

| 方式 | 命令 | 说明 |
|------|------|------|
| 直接运行 | `python backend/main.py` | 用你自己的 Python 3.14 环境，依赖需手动 `pip install -r backend/requirements.txt` |
| uv 管理 | `uv sync` → `uv run python backend/main.py` | uv 自动创建 `.venv` 并安装依赖，干净隔离 |

`pyproject.toml` 是项目元数据文件，声明了 Python 版本要求（≥3.14）和依赖列表。uv 读取它来创建环境；不用 uv 时这个文件不影响你运行。

---

## 架构

```
智能体 (Agent)
    │
    ▼
http://your-server-ip:4000/v1/chat/completions
    │
┌───┴───────────────────────────────┐
│         LLM Proxy 宿主机            │
│                                    │
│  ┌─────────────────────────────┐  │
│  │  ProxyManager               │  │
│  │  每个端口一个独立代理进程      │  │
│  │  4000~5000 动态分配          │  │
│  └──────────┬──────────────────┘  │
│             │                      │
│  ┌──────────┴──────────────────┐  │
│  │  FastAPI 管理 API (:3998)    │  │
│  │  用户认证、端口管理、历史查询  │  │
│  └──────────┬──────────────────┘  │
│             │                      │
│         MySQL                     │
└─────────────┼─────────────────────┘
              │
              ▼
    大模型 API (OpenAI / DMXAPI / ...)
```

---

## 方式一：开发模式（两个终端分别启动）

适合本地开发和调试。

**前置条件：**

- Python 3.14（推荐 `conda activate py314`）
- Node.js 18+
- MySQL 8.0+（远端或本地均可）

### 1. 克隆项目

```bash
git clone https://github.com/HaoCheng-Wang/llm-proxy.git
cd llm-proxy
```

### 2. 配置环境变量

```bash
cp .env.example .env
vim .env
```
所有配置项的详细说明见 `.env.example` 中的注释。

> 数据库和表会在后端首次启动时自动创建。

### 3. 安装后端依赖

选择以下任一方式：

**方式 A：用自己的 Python 3.14 环境（conda）**

```bash
conda activate py314
pip install -r backend/requirements.txt
```

**方式 B：用 uv 创建隔离的虚拟环境**

```bash
# uv 会自动下载或复用你已有的 Python 3.14
# 不需要提前安装 uv 到系统，直接下载即可：
#   Linux/macOS: curl -LsSf https://astral.sh/uv/install.sh | sh
#   Windows:     powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

uv sync
```

> `uv sync` 在项目根目录创建 `.venv/`，里面包含所有依赖。

### 4. 启动后端（终端 1）

```bash
# 方式 A（conda 环境）
python backend/main.py

# 方式 B（uv 环境）
uv run python backend/main.py
```

输出示例：
```
[DB] Database 'llm_proxy' is ready
[DB] All tables verified (pool_size=20, max_overflow=40)
[DB] Column migration complete
  Created admin user: admin
[Main] Management API ready on port 3998
```

### 5. 启动前端（终端 2）

```bash
cd frontend
npm install
npm run dev
```

前端 Vite 开发服务器会自动代理 `/api` 到 `localhost:3998`。

### 6. 访问

打开浏览器 → **http://localhost:3999**

管理员账号：`.env` 中设置的 `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD`。

### 7. 使用流程

1. 管理员登录 → 进入「用户管理」→ 批准新注册的用户
2. 用户登录 → 点击「创建新端口」→ 输入目标 API 地址（如 `https://api.openai.com`）
3. 在智能体中，把 API Base URL 改为 `http://<你的IP>:<分配的端口>/v1`
4. 在「查看详情」页面实时查看所有交互记录

---

## 方式二：Docker 生产部署

两个容器：`backend`（Python）+ `frontend`（nginx）。MySQL 需要用户自行部署（可在外网或另外的容器中）。

### 前置条件

- Docker 24+
- MySQL 已运行（可达的地址）

### 1. 准备

```bash
git clone https://github.com/HaoCheng-Wang/llm-proxy.git
cd llm-proxy
cp .env.example .env
vim .env
```

### 2. 构建并启动

```bash
# 构建前端并创建镜像
docker compose build

# 启动（后台运行）
docker compose up -d
```

### 3. 查看状态

```bash
docker compose ps
docker compose logs -f    # 实时日志
```

### 4. 停止

```bash
docker compose down
```

### docker-compose.yml 结构

```yaml
services:
  backend:              # Python + 代理管理器
    network_mode: host  # 直接绑宿主机网卡，端口动态分配
    volumes:
      - ./.env:/app/.env:ro  # 挂载配置文件，config.py 自动读取

  frontend:             # nginx + Vue 静态文件
    network_mode: host  # 和 backend 共享宿主机网络
    volumes:            # 挂载前端构建产物 + nginx 配置
```

> `network_mode: host` 让代理端口直接绑定宿主机网卡，被占端口自动跳过，不会卡住启动。所有配置项写在 `.env` 文件里，容器挂载后 `config.py` 通过 python-dotenv 读取，无需在 docker-compose.yml 重复声明。

所有环境变量的详细说明见 `.env.example`。

---

## 项目结构

```
llm-proxy/
├── .env                     # 环境变量（不提交 git）
├── .env.example             # 环境变量模板
├── .gitignore
├── .gitattributes
├── .dockerignore
├── Dockerfile               # 多阶段构建（前端→后端）
├── docker-compose.yml       # 2 容器编排
├── LICENSE                  # AGPL-3.0 许可证
├── nginx.conf               # 前端 nginx 配置
├── pyproject.toml           # uv 项目定义
├── README.md
│
├── backend/                 # Python FastAPI
│   ├── main.py              # 入口
│   ├── config.py            # 读取 .env
│   ├── database.py          # 自动建库建表 + 连接池
│   ├── models.py            # ORM 模型
│   ├── schemas.py           # Pydantic 模型
│   ├── auth.py              # JWT + bcrypt
│   ├── proxy_app.py         # 代理核心
│   ├── proxy_manager.py     # 多端口管理
│   ├── requirements.txt     # pip 兼容
│   └── routers/
│       ├── auth_router.py
│       ├── admin_router.py
│       ├── ports_router.py
│       └── config_router.py
│
└── frontend/                # Vue 3
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── components/
        │   └── JsonTree.vue # JSON 树形查看器
        ├── views/
        │   ├── Login.vue
        │   ├── Register.vue
        │   ├── Dashboard.vue
        │   ├── PortDetail.vue
        │   └── Admin.vue
        ├── api/index.js
        ├── stores/auth.js
        └── router/index.js
```

---

## API 接口

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/auth/register` | 注册（需管理员审批后才能登录） |
| `POST` | `/api/auth/login` | 登录，返回 JWT token |
| `GET` | `/api/auth/me` | 当前用户信息 |

### 端口管理

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/ports` | 创建代理端口 |
| `GET` | `/api/ports` | 端口列表（admin 看全部） |
| `GET` | `/api/ports/{id}?since_id=N&limit=20&offset=0` | 交互历史（`since_id` 增量轮询，`limit`/`offset` 分页，默认 20 条） |
| `DELETE` | `/api/ports/{id}` | 删除端口 |
| `DELETE` | `/api/ports/{id}/history` | 清空历史 |
| `DELETE` | `/api/ports/{id}/history/{request_id}` | 删除单条记录 |
| `GET` | `/api/ports/{id}/export` | 导出全量数据 |

### 管理员

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/admin/users` | 用户列表 |
| `PUT` | `/api/admin/users/approve` | 审批/取消审批用户 |
| `DELETE` | `/api/admin/users/{user_id}` | 删除用户 |

### 配置

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/config` | 返回 `display_ip` 等前端配置 |

---

## 生产环境安全建议

### HTTPS

默认 `nginx.conf` 仅监听 HTTP (3999)。生产环境**必须**启用 HTTPS，否则 JWT token 和密码以明文传输。

推荐使用 [Let's Encrypt](https://letsencrypt.org/) 免费证书 + [Certbot](https://certbot.eff.org/) 自动续期：

```bash
# 安装 certbot nginx 插件
apt install certbot python3-certbot-nginx

# 获取证书并自动配置 nginx
certbot --nginx -d your-domain.com
```

或在 nginx 前面加一层反向代理（如 Cloudflare、Caddy）来处理 TLS。

### 修改默认密码

首次启动后请立即修改 `.env` 中的 `DEFAULT_ADMIN_PASSWORD`，并重启后端。系统会在启动时打印警告提醒。

### SSRF 防护

系统默认允许代理目标指向任意地址（包括内网）。如需部署到公网环境，在 `.env` 中设置 `ALLOW_INTERNAL_TARGETS=false` 以阻止内网访问。

---

## 许可证

本项目采用**双重许可**模式：

### AGPL-3.0 开源许可（免费）

个人用户和开源项目可在 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 许可证下免费使用本项目。使用此许可证时，你**必须**：

- 公开你对本项目所做的任何修改的源代码
- 如果你通过网络提供服务（如 SaaS），必须向用户提供源代码
- 保留原始版权声明和许可证信息

### 商业许可（付费）

如果你希望在不公开源代码的情况下使用本项目（例如闭源商业产品、内部企业系统、SaaS 平台等），请联系作者获取商业许可证。

商业许可允许你：
- 在不公开源代码的情况下使用和修改本项目
- 将本项目集成到闭源商业产品中
- 无需遵守 AGPL 的 copyleft 条款

**联系方式：** hcwang0025@163.com
