# LLM Proxy — 智能体与大模型 API 通信拦截记录系统

截获和记录智能体与大模型 API 之间 HTTP 通信的代理系统。支持流式/非流式请求、SSE 重组为完整 JSON、JSON 树形查看、实时刷新、多用户隔离。

## 目录

- [核心设计](#核心设计)
- [部署方式](#部署方式)
- [项目结构](#项目结构)
- [前端功能](#前端功能)
- [API 接口](#api-接口)
- [环境变量](#环境变量)
- [生产环境安全建议](#生产环境安全建议)
- [完整数据流](#完整数据流)
- [数据模型](#数据模型)
- [安全设计](#安全设计)
- [高并发设计](#高并发设计)
- [SSE 流式处理](#sse-流式处理spooledtemporaryfile-架构)
- [大数据量导出：端到端流式架构](#大数据量导出端到端流式架构)
  - [各层关键设计](#各层关键设计)
  - [一次性 Ticket 鉴权](#一次性-ticket-鉴权)
  - [两条导出路径](#两条导出路径)
  - [format=simple 模式](#formatsimple-模式)
  - [每行处理性能](#每行处理性能零解析-vs-jsonloadsdumps)
  - [Nginx 流式 gzip 压缩](#nginx-流式-gzip-压缩)
  - [LONGTEXT 列按需选取](#longtext-列按需选取)
  - [复合索引与 COUNT 加速](#复合索引与-count-加速)
  - [生成器异常恢复与主动断连检测](#生成器异常恢复与主动断连检测)
  - [导出进度日志](#导出进度日志)
  - [ORDER BY 为什么不会拖慢速度](#order-by-为什么不会拖慢速度)
  - [编译 SQL 日志](#编译-sql-日志)
  - [实测效果](#实测效果)
  - [各优化项贡献](#各优化项贡献)
  - [瓶颈分析](#瓶颈分析)
- [许可证](#许可证)

## 核心设计

### 共享代理（Shared Proxy）

整个系统只监听一个 TCP 端口（默认 3998），通过 URL 路径中的代理编号区分不同用户：

```mermaid
flowchart LR
    A[智能体 Agent] -->|"POST /12345/v1/chat/completions"| B[LLM Proxy :3998]
    B -->|"查缓存/DB → target_url"| C[目标 LLM API]
    C -->|"响应"| B
    B -->|"记录"| D[(MySQL)]
    B -->|"透传"| A
```

| 特性 | 实现 |
|------|------|
| 编号 | 5 位随机数，系统分配，永不冲突 |
| 服务器 | 单进程 FastAPI + asyncio，一个端口处理千级并发 |
| 配置存储 | MySQL，所有状态持久化，内存缓存 + TTL 加速 |
| 安全 | JWT 认证 + bcrypt + SSRF 防护 + CORS |
| 转发协议 | HTTP/1.1 默认 + HTTP/2 按端口可选择 |
| API Key 覆盖 | 按端口可选配置专属 api_key，替换智能体原始认证头 |
| 前端主题 | 浅色/深色/跟随系统三种模式，CSS 变量 + localStorage 持久化 |
| 流式处理 | SpooledTemporaryFile 流内缓冲，流结束一次性写入 MySQL |

### 路由优先级

```
/api/*           → 管理接口（认证、端口 CRUD、历史查询）
/{port_number}/* → 共享代理端点（转发到目标 LLM API）
```

FastAPI 先注册 `/api/*` 路由，后注册 `/{port_number}/*`，确保管理接口优先匹配。

## 部署方式

### 开发模式

#### 前置条件

- Python 3.14（推荐 `conda activate py314`）
- Node.js 18+
- MySQL 8.0+（远端或本地均可）

#### 1. 克隆项目

```bash
git clone https://github.com/HaoCheng-Wang/llm-proxy.git
cd llm-proxy
```

#### 2. 配置环境变量

```bash
cp .env.example .env
vim .env
```

所有配置项的详细说明见 `.env.example` 中的注释。

> 数据库和表会在后端首次启动时自动创建。

#### 3. 安装后端依赖

选择以下任一方式：

**方式 A：用自己的 Python 3.14 环境（conda）**

```bash
conda activate py314
pip install -r backend/requirements.txt
```

**方式 B：用 uv 创建隔离的虚拟环境**

```bash
# uv 会自动下载或复用你已有的 Python 3.14
#   Linux/macOS: curl -LsSf https://astral.sh/uv/install.sh | sh
#   Windows:     powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
uv sync
```

> `uv sync` 在项目根目录创建 `.venv/`，里面包含所有依赖。

#### 4. 启动后端（终端 1）

```bash
# 方式 A（conda 环境）
nohup python backend/main.py > back.log 2>&1 & echo $! > back.pid

# 方式 B（uv 环境）
nohup uv run python backend/main.py > back.log 2>&1 & echo $! > back.pid
```

> 后端以后台方式运行，日志输出到 `back.log`。查看日志：`tail -f back.log`，停止：`kill $(cat back.pid)`

输出示例：
```
[Main] Running schema setup...
[DB] Database 'llm_proxy' is ready
[DB] All tables verified
[DB] Schema setup complete (DDL engine disposed)
[DB] Engine ready (pool_size=20, max_overflow=40)
  Created admin user: admin
[Main] Management API + Shared Proxy ready on port 3998
```

#### 5. 启动前端（终端 2）

```bash
cd frontend
npm install
npm run dev
```

前端 Vite 开发服务器自动代理 `/api` 到 `localhost:3998`，监听 `0.0.0.0:3999`。

> 如需修改前端绑定的 IP 或端口，编辑 `frontend/vite.config.js` 中的 `host` / `port` 字段即可。

#### 6. 访问

打开浏览器，按你的环境选择：

- 本机访问：**http://localhost:3999**
- 局域网访问：**http://<你的IP>:3999**（如 `http://192.168.2.105:3999`）

管理员账号：`.env` 中设置的 `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD`。

#### 7. 使用流程

1. 用户注册账号并登录（`REQUIRE_APPROVAL=false` 时注册后直接登录；设为 `true` 则由管理员审批后登录）
2. 用户登录 → 点击「创建代理」→ 输入目标 API 地址并**选择转发协议**（默认 HTTP/1.1，中转站场景推荐；直连模型 API 可选 HTTP/2），可选配置 **API Key 覆盖**（配置后替换智能体发送的认证头，不配置则原样透传）
3. 在智能体中，把 API Base URL 改为 `http://<你的IP>:3998/<分配的端口号>`，路径部分保持不变：
   - 原来：`https://api.openai.com/v1/chat/completions`
   - 改为：`http://<IP>:3998/12345/v1/chat/completions`（`12345` 为系统分配的 5 位编号）
   - API Key 等其他配置不需要任何修改（若端口配置了 API Key 覆盖，则智能体的 API Key 会被替换为系统配置的）
4. 在「查看详情」页面实时查看所有交互记录

### Docker 生产部署

两个容器：`backend`（Python）+ `frontend`（nginx）。MySQL 需自行部署。

#### 前置条件

- Docker 24+
- MySQL 已运行（可达的地址）

#### 1. 准备

```bash
git clone https://github.com/HaoCheng-Wang/llm-proxy.git
cd llm-proxy
cp .env.example .env
vim .env
```

#### 2. 构建并启动

```bash
docker compose build
docker compose up -d
```

#### 3. 查看状态

```bash
docker compose ps
docker compose logs -f    # 实时日志
```

#### 4. 停止

```bash
docker compose down
```

#### docker-compose.yml 结构

```yaml
services:
  backend:              # Python FastAPI + 共享代理
    network_mode: host  # 只需监听 :3998 一个端口
    volumes:
      - ./.env:/app/.env:ro  # 挂载配置文件

  frontend:             # nginx + Vue 静态文件（容器内自动构建）
    network_mode: host
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
```

> 后端只需监听一个端口（默认 3998）。前端在 Docker 构建阶段自动编译，无需本地安装 Node.js。

### 多机水平扩展

当单机连接数或 CPU 不足时，可部署多台代理实例，前面加 Nginx 负载均衡。

#### 架构

```mermaid
graph TD
    N[Nginx :3998<br/>least_conn 负载均衡]
    N -->|proxy_pass| P1[Proxy 1 :3998]
    N -->|proxy_pass| P2[Proxy 2 :3998]
    N -->|proxy_pass| P3[Proxy 3 :3998]
    P1 --> D[(MySQL<br/>共享数据库)]
    P2 --> D
    P3 --> D
    C1[智能体] -.->|请求| N
    C2[智能体] -.->|请求| N
    C3[智能体] -.->|请求| N
```

#### 必备条件

- MySQL 独立部署，所有代理实例连接同一个库
- 所有实例的 `.env` 中 `SECRET_KEY` **必须完全一致**（共享 JWT）
- `DATABASE_HOST` 指向同一台 MySQL

#### 配置步骤

**1. 每台机器部署代理实例**

```bash
git clone https://github.com/HaoCheng-Wang/llm-proxy.git
cd llm-proxy
cp .env.example .env
vim .env   # 设置 DATABASE_HOST、SECRET_KEY 等
docker compose up -d
```

**2. 配置 Nginx 负载均衡**

```nginx
upstream llm_proxy_backend {
    # 轮询模式，可改为 least_conn 优先分给连接最少的实例
    server 192.168.1.10:3998;
    server 192.168.1.11:3998;
    server 192.168.1.12:3998;
}

server {
    listen 3998;
    location / {
        proxy_pass http://llm_proxy_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;   # 匹配 SSE 长连接
    }
}
```

**3. 前端指向 Nginx**

修改 `frontend/vite.config.js`（开发）或 `nginx.conf`（生产）中的 API 代理目标为 Nginx 地址。

#### 多机安全机制

| 机制 | 实现 |
|------|------|
| 端口分配互斥 | `SELECT GET_LOCK('llm_proxy_port_alloc', 10)` — MySQL 服务端命名锁 |
| 端口缓存 | 每实例独立内存缓存，TTL 各自到期刷新，最终一致 |

#### 单机 vs 多机参数建议

| 参数 | 单机（默认） | 3 台 × 多机 |
|------|:--:|:--:|
| `HTTPX_MAX_KEEPALIVE_CONNECTIONS` | 100 | 每台 keepalive 连接数 |
| `DB_POOL_SIZE` | 20 | 10（每台，总 30） |
| `DB_LOG_POOL_SIZE` | 10 | 8（每台，总 24） |
| `DB_SAVE_WORKERS` | 8 | 8（每台） |
| MySQL `max_connections` | 100 | 200+ |

> 多机部署时降低每台的 DB 连接池，避免总连接数超出 MySQL 上限。总 DB 连接 ≈ 台数 × (DB_POOL_SIZE + DB_LOG_POOL_SIZE)。

## 项目结构

```
llm-proxy/
├── .env.example             # 环境变量模板
├── Dockerfile               # 后端镜像
├── Dockerfile.frontend      # 前端构建 + nginx
├── docker-compose.yml       # 2 容器编排
├── nginx.conf               # 前端 nginx 配置
├── pyproject.toml           # uv 项目定义 + Python 依赖声明 + pytest 配置
├── README.md
│
├── tests/                    # 测试套件
│   ├── conftest.py           # 共享 fixtures
│   ├── test_auth.py          # 13 个认证测试
│   ├── test_admin.py         # 7 个管理员测试
│   ├── test_ports.py         # 13 个端口管理测试
│   ├── test_proxy.py         # 17 个代理 / SSE 解析测试
│   └── test_stress.py        # 9 个压力测试
│
├── backend/                 # Python FastAPI 后端
│   ├── main.py              # 入口：启动 FastAPI + 注册路由 + lifespan
│   ├── config.py            # 读取 .env 环境变量
│   ├── database.py          # 自动建库建表 + 三连接池（管理/日志/流式导出）+ 迁移
│   ├── models.py            # ORM 模型（User / Port / Request）
│   ├── schemas.py           # Pydantic 请求/响应模型
│   ├── auth.py              # JWT 认证 + bcrypt 密码哈希
│   ├── proxy_app.py         # 代理核心：HTTP/1.1 + HTTP/2 双客户端、DB 记录、SSE 解析入口
│   ├── sse_parsers.py       # SSE 解析模块（LiteLLM 三层架构）：StreamChunk + ChunkAccumulator + 5 个 Provider 解析器
│   ├── shared_proxy.py      # 共享代理端点 /{port_number}/{path}
│   ├── proxy_manager.py     # 端口配置查询 + 缓存刷新
│   ├── requirements.txt     # pip 依赖
│   └── routers/
│       ├── auth_router.py   # 注册/登录/用户信息/修改密码
│       ├── admin_router.py  # 用户审批 + 已删除端口管理
│       ├── ports_router.py  # 端口 CRUD + 软删除 + 停用/启用 + 历史查询 + 流式导出
│       └── config_router.py # 前端配置（display_ip）
│
└── frontend/                # Vue 3 前端
    ├── index.html
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── main.js          # Vue 入口（createApp + Pinia + Router）
        ├── App.vue          # 根组件（导航栏 + 用户信息 + 主题切换 + toast 通知）
        ├── style.css        # 全局样式（CSS 变量主题系统：浅色/深色/跟随系统）
        ├── api/index.js     # API 客户端（axios 拦截器 + NDJSON 流式加载 + ticket 导出）
        ├── stores/auth.js   # Pinia 认证状态（token/用户信息/登录/登出）
        ├── stores/theme.js  # Pinia 主题状态（浅色/深色/跟随系统 + localStorage 持久化）
        ├── router/index.js  # 路由配置 + 守卫（guest/auth/admin 三级权限）
        ├── components/
        │   └── JsonTree.vue # JSON 树形查看组件（搜索/折叠/展开/导航）
        └── views/
            ├── Login.vue          # 登录页
            ├── Register.vue       # 注册页
            ├── Dashboard.vue     # 代理列表 + 创建/编辑/停用
            ├── PortDetail.vue    # 代理详情 + 交互记录 + 流式导出
            ├── JsonTreeViewer.vue # JSON 树形查看器（独立页面，含原始 SSE 查看）
            ├── Admin.vue         # 用户管理（审批/删除）
            ├── DeletedPorts.vue  # 已删除代理管理（恢复/彻底删除）
            └── ChangePassword.vue # 修改密码
```

## 前端功能

| 功能 | 实现 |
|------|------|
| 实时刷新 | 端口详情页每 2 秒轮询 `GET /api/ports/{id}?since_id=N`，仅拉取新记录 |
| 交互筛选 | 按请求方法分类：`📤 API请求`（POST/PUT/PATCH/DELETE）vs `🌐 其他`（GET/OPTIONS/HEAD） |
| JSON 树形查看 | 基于 `vue-json-pretty`，请求和响应 JSON 各有独立树形查看按钮，支持折叠/展开/搜索 |
| 重建异常审查 | 当 `reconstruction_error=True` 时显示橙色警告横幅，提供"查看完整 SSE 原始文本"按钮 |
| 一键导出 | 三合一：JSON 数据导出 / **流式全量导出**（浏览器原生 `<a>` 下载，不经过 JS 内存） / 后端全量 API 请求导出（浏览器原生下载，支持取消时自动释放后端资源） |
| 分页加载 | 首次加载 10 条，支持"加载更多"和"加载全部"，上限 100 条/次 |
| 滚动保护 | 阅读交互记录时新数据到达不跳动滚动位置 |
| 颜色风格切换 | 浅色/深色/跟随系统三种模式，通过顶部下拉菜单切换，偏好持久化到 localStorage，跟随系统模式实时响应操作系统主题变化 |

## API 接口

### 通用

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查 |
| GET | `/api/config` | 前端配置（display_ip, api_port） |

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册（审批行为由 REQUIRE_APPROVAL 控制） |
| POST | `/api/auth/login` | 登录，返回 JWT |
| GET | `/api/auth/me` | 当前用户信息 |
| POST | `/api/auth/change-password` | 修改密码 |

### 代理管理

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/ports` | 创建代理（自动分配编号，可配置 api_key 覆盖） |
| GET | `/api/ports` | 列出我的代理（管理员看全部） |
| GET | `/api/ports/active-ports` | 获取所有活跃端口号 |
| GET | `/api/ports/{id}` | 代理详情 + 交互历史（流式 NDJSON，分页） |
| PUT | `/api/ports/{id}` | 编辑代理（含转发协议、api_key 覆盖） |
| DELETE | `/api/ports/{id}` | 软删除（可恢复） |
| POST | `/api/ports/{id}/stop` | 停用 |
| POST | `/api/ports/{id}/start` | 启用 |
| DELETE | `/api/ports/{id}/history` | 清空历史 |
| DELETE | `/api/ports/{id}/history/{req_id}` | 删除单条记录 |
| GET | `/api/ports/{id}/history/{req_id}` | 获取单条记录详情 |
| GET | `/api/ports/{id}/history/{req_id}/raw-sse` | 按需获取原始 SSE 文本 |
| GET | `/api/ports/{id}/export` | 流式导出全部交互 JSON（SSCursor + StreamingResponse + Content-Disposition） |
| POST | `/api/ports/{id}/export-ticket` | 创建一次性下载 ticket（用于浏览器原生下载，JWT 不暴露在 URL 中） |

### 管理员

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/users` | 用户列表 |
| PUT | `/api/admin/users/approve` | 审批用户 |
| DELETE | `/api/admin/users/{id}` | 删除用户 |
| GET | `/api/admin/deleted-ports` | 已删除代理列表 |
| POST | `/api/admin/ports/{id}/restore` | 恢复代理 |
| DELETE | `/api/admin/ports/{id}/permanent` | 彻底删除 |

### 代理转发

| 方法 | 路径 | 说明 |
|------|------|------|
| 任意 | `/{port_number}/{path}` | 透明转发到目标 LLM API |

## 环境变量

> 完整参考 `.env.example`

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_HOST` | localhost | MySQL 地址 |
| `DATABASE_PORT` | 3306 | MySQL 端口 |
| `DATABASE_USER` | root | 数据库用户 |
| `DATABASE_PASSWORD` | root | 数据库密码 |
| `DATABASE_NAME` | llm_proxy | 数据库名（自动创建） |
| `SECRET_KEY` | — | JWT 签名密钥（必填） |
| `API_PORT` | 3998 | 后端监听端口 |
| `DISPLAY_IP` | — | 前端展示的服务器 IP |
| `CORS_ORIGINS` | — | CORS 白名单（逗号分隔） |
| `DEFAULT_ADMIN_USERNAME` | admin | 默认管理员 |
| `DEFAULT_ADMIN_PASSWORD` | admin123 | 默认密码 |
| `ALLOW_INTERNAL_TARGETS` | true | 是否允许代理到内网 |
| `REQUIRE_APPROVAL` | false | 新用户注册是否需要管理员审批 |
| `DB_POOL_SIZE` | 20 | 管理接口连接池 |
| `DB_MAX_OVERFLOW` | 40 | 管理接口连接池溢出 |
| `DB_LOG_POOL_SIZE` | 10 | 日志专用连接池 |
| `DB_LOG_MAX_OVERFLOW` | 20 | 日志专用连接池溢出 |
| `DB_SAVE_WORKERS` | 8 | 代理日志写入线程数 |
| `PORT_CACHE_TTL` | 5 | 端口缓存刷新间隔（秒） |
| `HTTPX_MAX_KEEPALIVE_CONNECTIONS` | 100 | httpx keep-alive 空闲连接数 |
| `PROXY_BODY_MEMORY_LIMIT` | 10485760 | 请求/响应体内存缓冲上限（字节） |

## 生产环境安全建议

### HTTPS

默认 `nginx.conf` 仅监听 HTTP (3999)。生产环境**必须**启用 HTTPS，否则 JWT token 和密码以明文传输。

推荐使用 Let's Encrypt 免费证书 + Certbot 自动续期：

```bash
apt install certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

或在 nginx 前面加一层反向代理（如 Cloudflare、Caddy）来处理 TLS。

### 修改默认密码

首次启动后请立即修改 `.env` 中的 `DEFAULT_ADMIN_PASSWORD`，并重启后端。

### SSRF 防护

系统默认允许代理目标指向任意地址（包括内网）。如需部署到公网，在 `.env` 中设置 `ALLOW_INTERNAL_TARGETS=false`。

## 完整数据流

### 1. 用户注册

用户注册行为由环境变量 `REQUIRE_APPROVAL` 控制。默认 `false`（无需审批，注册后直接登录）。

**无需审批模式（REQUIRE_APPROVAL=false，默认）**

```mermaid
sequenceDiagram
    actor U as 用户
    participant F as 前端
    participant P as LLM Proxy
    participant D as MySQL

    U->>F: 填写注册表单
    F->>P: POST /api/auth/register
    P->>D: INSERT users (is_approved=true)
    P-->>F: 注册成功，可以登录
    F-->>U: 提示注册成功

    U->>F: 登录
    F->>P: POST /api/auth/login
    P->>D: SELECT user + verify password
    P-->>F: JWT Token
    F-->>U: 登录成功
```

**需要审批模式（REQUIRE_APPROVAL=true）**

```mermaid
sequenceDiagram
    actor U as 用户
    participant F as 前端
    participant P as LLM Proxy
    participant D as MySQL
    actor A as 管理员

    U->>F: 填写注册表单
    F->>P: POST /api/auth/register
    P->>D: INSERT users (is_approved=false)
    P-->>F: 注册成功, 等待审批
    F-->>U: 提示等待管理员审批

    A->>F: 进入用户管理
    F->>P: GET /api/admin/users
    P->>D: SELECT * FROM users
    D-->>P: 用户列表
    P-->>F: 展示待审批用户
    A->>F: 点击"批准"
    F->>P: PUT /api/admin/users/approve
    P->>D: UPDATE users SET is_approved=true
    P-->>F: 审批成功

    U->>F: 登录
    F->>P: POST /api/auth/login
    P->>D: SELECT user + verify password
    P-->>F: JWT Token
    F-->>U: 登录成功
```

### 2. 代理创建

```mermaid
sequenceDiagram
    actor U as 用户
    participant P as LLM Proxy
    participant D as MySQL

    U->>P: POST /api/ports {target_url, description}
    P->>P: JWT 验证身份 (require_approved)
    P->>P: _validate_target_url() → SSRF 检查
    P->>D: SELECT GET_LOCK('llm_proxy_port_alloc')
    Note over P,D: MySQL 命名锁, 序列化分配
    P->>D: SELECT active port_numbers
    P->>P: 随机生成 5 位数 (10000-99999), 去重
    P->>D: INSERT INTO ports
    P->>D: SELECT RELEASE_LOCK
    P->>P: refresh_port_cache()
    P-->>U: 代理编号 + 目标地址
```

### 3. 代理转发（非流式）

```mermaid
sequenceDiagram
    participant C as 智能体
    participant P as LLM Proxy
    participant D as MySQL
    participant U as 上游 LLM API

    C->>P: POST /12345/v1/chat/completions
    P->>P: aget_target_url(12345) → 缓存/DB
    
    alt 缓存命中
        P->>P: 内存查找 (微秒级)
    else 缓存过期/缺失
        P->>D: SELECT port WHERE port_number=12345
        D-->>P: target_url
    end

    P->>P: SpooledTemporaryFile 读取请求体
    P->>P: 请求头处理 (移除 host/content-length 等)
    P->>U: httpx HTTP/1.1 转发 (启动时预热连接池, keepalive 复用)
    U-->>P: 响应 (非 streaming)
    P->>P: SpooledTemporaryFile 累积响应体
    P->>C: 返回完整响应
    
    P--)D: asyncio.create_task → 线程池 → LogSessionLocal → INSERT requests
    Note over P,D: 后台写入，不阻塞响应
```

### 4. 代理转发（流式 SSE — SpooledTemporaryFile 架构）

```mermaid
sequenceDiagram
    participant C as 智能体
    participant P as LLM Proxy
    participant D as MySQL
    participant U as 上游 LLM API

    C->>P: POST /12345/v1/chat/completions
    P->>U: httpx.stream 转发

    loop 逐 chunk
        U-->>P: SSE chunk
        P-->>C: yield chunk (立即透传)
        P->>P: SpooledTemporaryFile.write(chunk)
        Note over P: ≤10MB 内存, >10MB 溢写临时文件
    end

    P->>P: resp_buf.read() → 完整 SSE body
    P->>P: _reconstruct_sse_to_json() → 重组 JSON
    P--)D: asyncio.create_task → INSERT requests
    Note over P,D: 一次 INSERT, 不阻塞客户端
```

### 5. 数据入库（关键保证）

每条交互记录最终都进入 `requests` 表，含以下字段：

| 字段 | 非流式 | 流式 |
|------|:--:|:--:|
| `port_id` | port.id 或 NULL | port.id 或 NULL（端口删除后仍保存） |
| `prefer_http2` | port 配置 | 决定使用 HTTP/1.1 还是 HTTP/2 客户端转发 |
| `method` / `path` | ✅ | ✅ |
| `request_headers` / `request_body` | ✅ | ✅ |
| `response_headers` | ✅ | ✅ |
| `response_body` | JSON | 重建 JSON（失败时回退为原始文本） |
| `response_body_raw` | 同 response_body | 完整原始 SSE 文本 |
| `status_code` | ✅ | ✅ |
| `duration_ms` | ✅ | ✅ |
| `reconstruction_error` | 始终 False | True = 重建失败（前端展示警告） |

数据不丢失保证：

- 端口被删除 → 记录仍写入，`port_id=NULL`
- SSE 重建失败 → `response_body_raw` 保留原始数据，`reconstruction_error=True`
- DB 写入失败 → 3 次重试

### 6. 代理端口生命周期

```mermaid
stateDiagram-v2
    [*] --> 运行中 : 用户创建
    运行中 --> 停用 : stop_port()
    停用 --> 运行中 : start_port()
    运行中 --> 运行中 : 编辑(目标地址/描述/编号/api_key)
    运行中 --> 已删除 : 软删除
    停用 --> 已删除 : 软删除
    已删除 --> 停用 : 管理员恢复
    已删除 --> [*] : 彻底删除

    note right of 已删除
        deleted_at 设置时间戳
        is_active = false
        数据完整保留
    end note

    note right of 停用
        is_active = false
        编号保留, 可重新启用
    end note
```

**软删除机制**：删除端口时仅设置 `deleted_at` 时间戳和 `is_active=False`，不删除数据库记录。管理员可在「已删除代理」页面查看、恢复或彻底删除。软删除期间产生的交互记录仍写入 `requests` 表（`port_id=NULL`），数据不会丢失。

**停用 vs 软删除**：停用只是 `is_active=False`，代理编号保留且可重新启用。软删除后代理从缓存移除，编号不可再用。

## 数据模型

```
users                        ports                              requests
┌──────────────┐            ┌─────────────────────────┐        ┌──────────────────────┐
│ id (PK)      │──┐         │ id (PK)                 │──┐     │ id (PK)              │
│ username     │  │         │ port_number (UQ)        │  │     │ port_id (FK→ports)   │
│ password_hash│  │ 1:N     │ user_id (FK)            │←─┘     │ method               │
│ role         │──┘         │ target_url              │ 1:N    │ path                 │
│ is_approved  │            │ description             │────────│ request_headers      │
│ created_at   │            │ is_active               │        │ request_body         │
└──────────────┘            │ prefer_http2            │        │ response_headers     │
                            │ api_key                 │        │ response_body        │
                            │ deleted_at              │        │ response_body_raw    │
                            │ created_at              │        │ status_code          │
                                                               │ duration_ms          │
                                                               │ reconstruction_error │
                                                               │ created_at           │
                                                               └──────────────────────┘
```

## 安全设计

| 层面 | 措施 | 实现 |
|------|------|------|
| 身份认证 | JWT (HS256) | Bearer Token，7 天过期 |
| 密码存储 | bcrypt | 72 字节截断 + 随机盐 |
| 用户审批 | REQUIRE_APPROVAL 环境变量控制 | 设为 true 时新用户注册后需管理员审批；默认 false，无需审批 |
| 权限控制 | role (admin/user) | 依赖注入 require_admin / require_approved |
| SSRF 防护 | URL 校验 | 阻止内网 IP / localhost / metadata 端点 |
| CORS | FastAPI Middleware | 可配置来源白名单 |
| 端口分配锁 | MySQL GET_LOCK | 序列化分配，防止编号冲突 |
| API Key 透传 | 默认原样透传 | 默认行为：Authorization 头原样转发到上游，完整记录到数据库 |
| API Key 覆盖 | 按端口可选配置 | 每个代理端口可配置专属 api_key，配置后替换智能体发送的 Authorization/x-api-key 等认证头；不配置则原样透传 |

## 高并发设计

### 瓶颈分析与缓解

| 瓶颈点 | 方案 | 参数 |
|--------|------|------|
| 事件循环阻塞 | 所有 DB 查询在线程池执行 | `run_in_executor(_db_executor)` |
| 管理接口 vs 日志争抢 | 双 DB 连接池完全隔离 | 管理池 20+40 / 日志池 10+20 |
| 线程池争抢 | 代理日志使用专用线程池 | `DB_SAVE_WORKERS=8` |
| 上游连接数 | httpx 双客户端（热启动）| HTTP/1.1 (默认) + HTTP/2 (按端口可选), 连接无上限 |
| 端口查找 | 内存缓存 + TTL | 5 秒过期，缓存命中率 99.9%+ |
| 请求体 OOM | SpooledTemporaryFile | ≤10MB 内存，>10MB 溢写磁盘 |
| 流式内存累积 | SpooledTemporaryFile | ≤10MB 内存, >10MB 自动溢写临时文件 |
| 流式重建 | 流结束后立即同步重组 | 无需后台 Worker, 即写即见 |
| HTTP/1.1 keepalive | 连接池预热 + 自动重试 | 无 GOAWAY，流式稳定不中断 |

### 为什么不需要多进程

| 原因 | 说明 |
|------|------|
| I/O 密集型 | 代理转发 90%+ 时间在 await（等待网络），CPU 利用率 <5% |
| asyncio 协程 | 单线程调度数千协程，每个协程切换开销微秒级 |
| 阻塞操作已隔离 | DB 写入、SSE 解析全部放到专用线程池 |
| 真正瓶颈在上游 | OpenAI 的生成速度（秒级）远超代理转发开销（微秒级） |

如需高可用，应使用多容器 + 负载均衡，而非单机多进程。

## SSE 流式处理（SpooledTemporaryFile 架构）

### SSE 解析架构 (LiteLLM-inspired)

**问题**：LLM API 的流式响应是碎片化的 —— 模型的每段输出以独立 chunk 到达，且不同厂商有完全不同的 SSE 协议。代理需要把这些碎片重建成人类可读的完整 JSON 才能存入数据库。

**方案**：借鉴 LiteLLM 的分层设计，将 SSE 解析拆为 `sse_parsers` 独立模块，每个厂商的差异被封装在专属解析器中，对外暴露统一的接口。

**整体数据流**

```mermaid
flowchart TD
    subgraph 输入
        A["📥 原始 SSE 文本<br/>SpooledTemporaryFile 中已完整收集"]
    end

    A --> B

    subgraph Layer1["<b>Layer 1 — 格式检测 &amp; Provider 解析</b>"]
        direction LR
        B["detect_sse_format()<br/>首个 chunk 自动识别"] --> C["Parser Registry<br/>查表分发"]
        C --> D1["AnthropicSSEParser<br/>┃ 状态机驱动，维护<br/>┃ content_blocks 字典"]
        C --> D2["OpenAIChatSSEParser<br/>┃ delta 字段累加"]
        C --> D3["OpenAIResponsesSSEParser<br/>┃ 事件类型分发"]
        C --> D4["GeminiSSEParser<br/>┃ candidates 累积"]
        C --> D5["GenericSSEParser<br/>┃ 深度合并 + 回退"]
    end

    D1 & D2 & D3 & D4 & D5 --> E

    subgraph Layer2["<b>Layer 2 — 统一中间表示</b>"]
        E["StreamChunk dataclass<br/>┃ text_delta / reasoning_delta<br/>┃ tool_call_delta / finish_reason<br/>┃ usage / metadata / raw"]
    end

    E --> F

    subgraph Layer3["<b>Layer 3 — 响应组装</b>"]
        direction LR
        F["ChunkAccumulator<br/>┃ 按字段类型合并<br/>┃ 字符串拼接 / 按 index 合并<br/>┃ dict merge / first-wins / last-wins"] --> G["parser.finalize()<br/>构建厂商原生格式 JSON"]
    end

    G --> H["📤 存入数据库<br/>response_body 字段"]

    style Layer1 fill:#d6eaf8,stroke:#2980b9,color:#154360
    style Layer2 fill:#d5f5e3,stroke:#27ae60,color:#145a32
    style Layer3 fill:#e8daef,stroke:#8e44ad,color:#4a235a
```

#### 格式检测

从第一个有效 `data:` 行 JSON 中按优先级判断：

```mermaid
flowchart LR
    A["首个 chunk JSON"] --> B{"type 字段 ∈<br/>Anthropic 事件集?"}
    B -->|是| ANT["Anthropic"]
    B -->|否| C{"type 字段<br/>startswith 'response.'?"}
    C -->|是| RESP["OpenAI Responses"]
    C -->|否| D{"存在<br/>'candidates' 键?"}
    D -->|是| GEM["Gemini"]
    D -->|否| E{"存在<br/>'choices' 键?"}
    E -->|是| CHAT["OpenAI Chat<br/>（含 DeepSeek/Mistral/通义千问 等兼容格式）"]
    E -->|否| GEN["Generic<br/>（通用回退）"]

    style ANT fill:#d5f5e3,stroke:#27ae60,color:#145a32
    style RESP fill:#d6eaf8,stroke:#2980b9,color:#154360
    style GEM fill:#fdebd0,stroke:#e67e22,color:#7e5100
    style CHAT fill:#d5f5e3,stroke:#27ae60,color:#145a32
    style GEN fill:#fadbd8,stroke:#e74c3c,color:#78281f
```

| 优先级 | 检测条件 | 格式 | 典型 chunk 特征 |
|:--:|--------|------|----------------|
| ① | `type` 字段 ∈ `{message_start, content_block_start, content_block_delta, ...}` | Anthropic | 多事件类型 SSE：`event:` + `data:` 成对出现 |
| ② | `type` 字段 startswith `"response."` | OpenAI Responses | 单事件类型：每行 `data:` 含 `type` 字段 |
| ③ | `"candidates"` in chunk | Gemini | 每行是一个完整 `GenerateContentResponse` |
| ④ | `"choices"` in chunk | OpenAI Chat | 标准 OpenAI 格式，以 `[DONE]` 结束 |
| ⑤ | 以上均不匹配 | Generic | 深度合并 → 树遍历提取文本 → 返回原始文本 |

分发器 `reconstruct_sse_to_json()` 从注册表找到对应 Parser 并调用其 `parse()` 类方法。如果格式特定解析器返回 `None`，自动回退到 Generic 解析器；如果 Generic 也失败，返回原始 SSE 文本。

#### StreamChunk — 所有 Parser 的统一"语言"

每种厂商的 SSE chunk 结构不同，但都表达同一件事：**增量内容**。`StreamChunk` 把厂商差异归一化：

| 字段 | 类型 | 来源（以各厂商为例） | 说明 |
|------|------|---------------------|------|
| `text_delta` | `str` | Anthropic `delta.text`、OpenAI `choices[0].delta.content`、Gemini `parts[*].text` | 本次增量文本 |
| `reasoning_delta` | `str` | OpenAI `delta.reasoning_content`、Anthropic `delta.thinking` | 推理/思考过程增量 |
| `tool_call_delta` | `dict\|None` | OpenAI `delta.tool_calls[*]`、Anthropic `delta.partial_json` | 工具调用片段，含 `{index, id, name, arguments}` |
| `finish_reason` | `str\|None` | 各厂商的结束标记 | 生成结束原因 |
| `usage` | `dict\|None` | 各厂商的 token 统计 | 输入/输出 token 数 |
| `metadata` | `dict` | `{model, id, object, role, ...}` | 首次出现的元数据 |
| `raw` | `dict\|None` | 完整原始 JSON | 供 `finalize()` 按需取用厂商特有字段 |

各 Parser 的 `parse_line()` 只做一件事：解析一行 SSE → 返回 `StreamChunk`。它不关心如何重建最终 JSON，只负责翻译。

#### ChunkAccumulator — 碎片的拼图工

`finalize()` 调用之前，所有 `StreamChunk` 已被 `ChunkAccumulator` 合并。它定义了 6 种字段的合并规则：

| 字段 | 合并策略 | 示例 |
|------|----------|------|
| `text` / `reasoning` | **字符串拼接** | `"Hello" + " world"` → `"Hello world"` |
| `tool_calls` | **按 index 分组合并**：`id` 覆盖、`name` 拼接、`arguments` 拼接 | 3 个 tool_call chunk（index=0）→ 1 个完整 tool_call |
| `finish_reason` | **最后非 None 值**（last-wins） | 多个 chunk 带 finish_reason，取最后一个 |
| `usage` | **dict.update()** 合并 | input_tokens + output_tokens → 完整 usage |
| `metadata` | **首次非空保留**（first-wins） | 第一个 chunk 的 model/id 即为最终值 |

使用 `ChunkAccumulator` 的是简单解析器（OpenAI Chat、Gemini），它们内部状态就是 accumulator。Anthropic 和 OpenAI Responses 有更复杂的状态机，但最终 `finalize()` 中做了类似的事情。

#### Provider 深入

**Anthropic — 状态机驱动**

Anthropic 的 SSE 协议最复杂：多个 `event:` 类型，每个 content block 经历 start → delta(s) → stop 生命周期。

```mermaid
stateDiagram-v2
    [*] --> message_start : 提取 msg_id / model / usage.input_tokens
    message_start --> content_block_start : 根据 type 初始化 block
    state content_block_start {
        CBS_init : text → {type, text, citations}
        CBS_init2: tool_use/search_tool → {type, id, name, input_json}
        CBS_init3: thinking → {type, thinking, signature}
        CBS_init4: compaction → {type, content, encrypted_content}
    }
    content_block_start --> content_block_delta : 累积增量
    state content_block_delta {
        CBD_text : text_delta → 追加到 text
        CBD_tool : input_json_delta → 追加到 input_json
        CBD_think: thinking_delta → 追加到 thinking
        CBD_sig  : signature_delta → 追加到 signature
        CBD_cit  : citations_delta → 追加到 citations[]
        CBD_comp : compaction_delta → 设置 content / encrypted_content
    }
    content_block_delta --> content_block_delta : 同 block 继续
    content_block_delta --> content_block_stop : block 结束
    content_block_stop --> content_block_start : 下一个 block
    content_block_stop --> message_delta : 全部 block 完成
    message_delta --> message_stop : 提取 stop_reason / usage.output_tokens
    message_stop --> [*]
```

核心状态是 `self.blocks: dict[int, dict]` —— 按 index 索引 content block。每个 block 累积自己的文本/thinking/tool input。`finalize()` 按 index 排序遍历，根据 `type` 组装对应的 JSON 结构。

容错处理：如果 `content_block_delta` 到达时对应 index 的 block 尚未初始化（`content_block_start` 被跳过），根据 delta.type 反推 block 类型自动创建。

**OpenAI Chat — delta 累加**

最直接的格式。`parse_line()` 从 `choices[0].delta` 提取 `content`、`reasoning_content`、`tool_calls` 填入 `StreamChunk`，`ChunkAccumulator` 自动完成拼接。`finalize()` 将 accumulator 属性组装为 `chat.completion` JSON。

Tool call 的 arguments 是 JSON 片段流 —— 多个 chunk 的 `function.arguments` 累积后得到完整 JSON 字符串（如 `{"location":"NYC"}`），不经 `json.loads` 解析，原样保留。

**OpenAI Responses — 事件分发**

`parse_line()` 根据 `event.type` 分发：
- `response.created` → 记录 `response_id` / `model` / `status`
- `response.output_text.delta` → 累积文本
- `response.completed` → 捕获完整 `usage` 和 `output` 数组

如果 `response.completed` 事件携带了完整 `output` 数组，`finalize()` 直接使用它；否则从累积的 deltas 自行构造。

**Gemini — 每行完整 JSON**

Gemini 的每个 chunk 是完整的 `GenerateContentResponse`。`parse_line()` 提取 `candidates[0].content.parts[*].text` 和 `usageMetadata`，由 `ChunkAccumulator` 完成拼接。

**Generic — 三阶段回退**

1. `deep_merge()`：深度合并所有 chunk（string 拼接、list 按 index 合并、dict 递归）—— 对 OpenAI 兼容格式有效
2. `walk_json_for_text()`：递归遍历 JSON 树，提取常见文本键（content/text/delta/value/data）的值
3. 返回原始 SSE 文本 —— 确保**数据不会丢失**，前端仍可查看

#### 与 LiteLLM 的关键差异

| 维度 | LiteLLM | 本项目 |
|------|---------|--------|
| **输出格式** | 统一转为 OpenAI 格式（跨厂商兼容） | 保留原始厂商格式（忠实记录） |
| **处理时机** | 实时逐 chunk 输出给用户 | 先收集完整流，结束后一次性重建 |
| **目标场景** | LLM 网关 / 统一 API | 透明代理 / 日志审计 |
| **复杂度** | 需处理音频/图片/annotations/多轮对话重建 | 仅关注文本+tools+thinking 三种核心内容 |

### 格式支持

| API | 检测标志 | 解析器 | 解析方式 |
|-----|----------|--------|----------|
| OpenAI Chat Completions | `choices[].delta` | `OpenAIChatSSEParser` | ChunkAccumulator 累积 content + reasoning_content + tool_calls（按 index 合并） |
| OpenAI Responses | `type` 以 `response.` 开头 | `OpenAIResponsesSSEParser` | 按事件类型（`response.created`/`output_text.delta`/`completed`）累积 |
| Anthropic Messages | `type` 为已知事件名 | `AnthropicSSEParser` | 状态机驱动：`message_start` → `content_block_start` → `content_block_delta` → `content_block_stop` → `message_delta` → `message_stop` |
| Google Gemini | `candidates[]` 存在 | `GeminiSSEParser` | ChunkAccumulator 累积 parts[].text |
| 通用/未知 | 以上均不匹配 | `GenericSSEParser` | 三阶段回退：深度合并 → JSON 树遍历提取文本 → 返回原始 SSE 文本 |

### 流内缓冲

每个流式请求创建一个独立的 `SpooledTemporaryFile` 对象：

```
async for chunk in response.aiter_bytes():
    yield chunk                        # ① 立即发给客户端
    resp_buf.write(chunk)              # ② 写入缓冲区

# 流结束
resp_buf.seek(0)
full_body = resp_buf.read()           # ③ 读出完整 SSE
reconstructed = _reconstruct_sse_to_json(full_body)  # ④ 重组 JSON
asyncio.create_task(_save_record_async(...))          # ⑤ 一次性写入 requests
```

**为什么不在流进行中写 MySQL？**

每个 SSE 流可能产生数百到数千个 chunk，每个 chunk 几十到几百字节。逐 chunk INSERT 会产生大量数据库写入（redo log、undo log、binlog、索引维护），对 MySQL 造成不必要的压力。SpooledTemporaryFile 在流中进行零 I/O（内存模式），流结束后一次性写入 —— 从 N 次 INSERT 降为 1 次。

**溢出到磁盘**

SpooledTemporaryFile 在小于 `PROXY_BODY_MEMORY_LIMIT`（默认 10 MB）时完全在内存中操作。超过上限时自动透明地溢出到磁盘临时文件。LLM API 的响应通常远小于 10 MB，因此绝大多数流不会触及磁盘。

### 数据库连接池设计

整个系统使用 **三套独立的 SQLAlchemy 连接池**：

```
┌─ engine (DB_POOL_SIZE=20, DB_MAX_OVERFLOW=40)
│   连接同一个 MySQL
│   用途：FastAPI 管理路由 → 用户登录、端口 CRUD、历史查询
│   调用方：浏览器触发的 /api/* 请求
│
├─ _log_engine (DB_LOG_POOL_SIZE=10, DB_LOG_MAX_OVERFLOW=20)
│   连接同一个 MySQL
│   用途：代理日志写入 → 写 requests
│   调用方：代理转发线程
│
└─ _stream_engine (pool_size=5, max_overflow=5)
    连接同一个 MySQL
    用途：大数据量流式查询 → 端口历史导出
    特性：pymysql SSCursor (server-side cursor)，逐批拉取行，不一次性加载到内存
    调用方：导出接口 GET /api/ports/{id}/export
```

**为什么要三套？**

```mermaid
flowchart LR
    subgraph 管理接口
        A[用户登录]
        B[创建代理]
        C[查看历史]
    end
    subgraph 代理日志
        D[100个流同时结束]
        E[INSERT requests]
    end
    subgraph 流式导出
        F[大数据量导出]
        G[SSCursor 逐批拉取]
    end
    A --> engine[(engine 20)]
    B --> engine
    C --> engine
    D --> log[(log_engine 10)]
    E --> log
    F --> stream[(stream_engine 5)]
    G --> stream
```

如果共用一套连接池，100 个 SSE 流同时结束的瞬间——每个流一次 `INSERT requests`——可能暂时耗尽池中连接。此时管理员尝试登录，发现**无连接可用**，只能排队等 30 秒超时。三套池完全隔离后，日志写入再繁忙，管理接口始终有 20 个空闲连接待命。

流式导出使用第三套池，原因是 **pymysql SSCursor 的约束**：SSCursor 在读取完所有行之前不能在同一连接上执行其他查询。如果导出使用了管理池的连接，可能导致其他请求无法读取数据。独立小池（5+5）确保导出不阻塞其他操作。

**日志专用线程池**

代理日志写入（`_save_to_db`）使用 `database._db_executor`（`DB_SAVE_WORKERS=8` 个线程），而非 asyncio 的默认线程池。这样日志写入任务之间互不争抢，且不会占满 asyncio 默认线程池影响其他操作。

### 大数据量导出：端到端流式架构

端口交互历史导出（`GET /api/ports/{id}/export`）面临的核心挑战是：一个端口可能积累数万条记录，每行包含 LONGTEXT 字段。传统做法（后端全量查询 → 内存组装 → 一次性返回 → 前端解析）在数据量大时必然超时或 OOM。

本系统的导出采用**五层真流式 + 浏览器原生下载**——用户点击导出，浏览器**立即弹出下载对话框**，自带进度条。数据从 MySQL 直接流到用户硬盘，全程不经过 JavaScript 内存缓冲，也不受任何超时限制。

```mermaid
flowchart LR
    subgraph 用户操作
        U[点击导出] -->|"POST /api/ports/{id}/export-ticket<br/>（Bearer JWT 鉴权）"| T
        T[获得一次性 ticket] -->|"GET /api/ports/{id}/export?ticket=...<br/>（浏览器原生请求）"| N
    end
    subgraph nginx
        N -->|"导出路由独立 location<br/>proxy_read_timeout=0<br/>proxy_buffering off + gzip"| S
    end
    subgraph FastAPI
        S -->|"_consume_ticket()<br/>验证并销毁 ticket"| V
        V -->|"StreamingResponse<br/>Content-Disposition: attachment"| G
        G[stream_jsonl 生成器] -->|"defer 不需要的 LONGTEXT 列<br/>b''.join 直接字节拼接<br/>零解析零验证"| Q
    end
    subgraph MySQL
        Q -->|"yield_per(500)"| C[SSCursor]
        C -->|"逐行拉取<br/>服务端游标<br/>复合索引加速"| D[(requests 表)]
    end
    subgraph 浏览器
        N -->|"下载对话框立即弹出<br/>原生进度条<br/>直接写磁盘"| F[(用户硬盘)]
    end
```

#### 各层关键设计

| 层 | 技术选型 | 为什么 | 超时 |
|---|---|---|---|
| MySQL → Python | `pymysql.cursors.SSCursor` + 复合索引 + `USE INDEX` hint | 服务端游标，不在客户端缓冲全部行；显式强制 `ix_requests_port_created` 索引避免优化器选错索引导致 filesort | `read_timeout=600s` |
| Python 行处理 | `b"".join(parts)` + defer 不需要的列 | simple 格式只查 2 个 LONGTEXT（不是 5 个）；LONGTEXT 零解析零验证原样嵌入 | 每行 ~微秒级 |
| Python → FastAPI | `StreamingResponse` + `yield_per(500)` | 批次更大 = 更少 ORM identity map 过期开销；SSCursor 底层逐行拉取，内存恒定 | 无 |
| nginx | 独立 `location` + gzip + unbuffered | 传输压缩 5–10×；`proxy_buffering off` 字节立刻推到浏览器 | `proxy_read_timeout=0` |
| 浏览器 | `<a>` 标签 + `Content-Disposition: attachment` | 浏览器原生下载管理器，弹出对话框即开始写磁盘，JS 内存 0 | 无 |

#### 一次性 Ticket 鉴权

核心矛盾：浏览器 `<a>` 标签下载**无法携带自定义 HTTP 头**（如 `Authorization: Bearer ...`），但直接把 JWT 放在 URL 查询参数里会泄露到 nginx 日志、浏览器历史、Referer 头。

解决：引入**一次性下载 ticket**——前端通过 Bearer 鉴权的 API 获取一个随机字符串，然后把它放在下载 URL 中。Ticket 是：

- **单次使用**：验证后立即销毁（`dict.pop`），同一个 ticket 第二次请求直接 401
- **60 秒过期**：指拿到 ticket 后必须在 60 秒内**开始下载**，与下载耗时无关——ticket 在 HTTP 请求到达的瞬间就被验证并销毁了，后续几小时的流式传输完全不受影响
- **内存存储**：不落库，进程重启后全部作废
- **端口绑定**：ticket 记录了 `port_id`，不能跨端口使用

```mermaid
sequenceDiagram
    participant U as 用户
    participant F as 前端
    participant B as 后端

    U->>F: 点击"导出全部数据"
    F->>B: POST /api/ports/{id}/export-ticket<br/>Authorization: Bearer <JWT>
    B-->>F: {"ticket": "a1b2c3...", "expires_in": 60}
    F->>F: 构建 <a href="/api/ports/{id}/export?ticket=a1b2c3...">
    F->>F: a.click()
    Note over B: _consume_ticket() → 验证 → 销毁 → 开始流式传输
    B-->>U: Content-Disposition: attachment<br/>浏览器弹出下载对话框 + 进度条
    Note over U: 下载可能持续数分钟<br/>ticket 早已销毁，不受影响
```

#### 两条导出路径

```
导出方式          all / api 过滤器                    other 过滤器
─────────────────  ─────────────────────────────────  ──────────────────────
数据路径           浏览器原生下载（ticket → 流式）       前端已加载分页数据导出
处理              后端 SSCursor 逐批 → 浏览器写磁盘     前端 Blob → 下载
API 调用            POST /export-ticket + GET /export   无 API 调用
内存占用            浏览器 JS ~O(1)                     浏览器 JS O(n)
格式              后端 Content-Disposition 含时间戳文件名 前端生成文件名
```

`all` 和 `api` 过滤器走浏览器原生下载。`other`（非 API 方法）过滤器后端不支持，从前端已加载的分页数据导出。

#### format=simple 模式

导出 API 支持 `?format=simple` 参数。默认 `full` 模式返回完整的 `{port, total_requests, requests: [{id, method, path, request_headers, ...}]}`；`simple` 模式返回扁平数组 `[{index, method, path, status_code, request, response}]`，由后端完成 JSON 提取和重组，前端零处理开销。"导出全部 API 请求 JSON"按钮即使用此模式。

#### 每行处理性能：零解析 vs json.loads+dumps

导出最耗 CPU 的环节不是数据库查询，而是**每行数据的 Python JSON 处理**。一个端口 1 万条记录、每条 4 个 LONGTEXT 字段（请求/响应 header+body），平均 50KB 的 body 意味着一共 **2GB 文本需要处理**。

旧方案（每行的典型写法）：

```
MySQL LONGTEXT (已是有效 JSON 字符串)
  → json.loads(50KB) → Python dict/list 树 (数千个对象)
  → json.dumps(200KB 全行) → JSON 字符串
  → encode("utf-8") → bytes
  → GC 回收临时对象树
  # 合计：4 万次 json.loads + 1 万次 json.dumps
```

本系统的做法：

```
MySQL LONGTEXT  →  原样嵌入输出  →  b"".join(parts)
                   零复制              一次内存分配
```

**为什么可以原样嵌入？** LLM API 的请求体和响应体**永远是 JSON**（OpenAI/Anthropic/Google 的 API 都只接受/返回 JSON）。MySQL 里存的 `request_body` / `response_body` 字段已经是合法的 JSON 字符串。`_embed_body()` 做一次假值检查（`raw or "null"`）然后直接返回原始字符串——零解析、零验证、零复制。

| 指标 | 旧方案 (json.loads+dumps) | 新方案 (字节拼接) |
|------|--------------------------|-------------------|
| 每行处理 | `json.loads(4×body)` + `json.dumps(全行)` | `b"".join` |
| body 文本复制 | 2 次（parse 分配对象 → serialize 生成字符串） | 0 次（原样嵌入） |
| Python 对象分配 | 每行数千个 dict/list/str/int | 每行 1 个 bytes |
| 1 万行 50KB body 耗时 | ~30-120 秒（Python CPU bound） | ~1-3 秒 |
| 内存峰值 | Python 对象树递归分配 | ~500 行 × 各字段字节片段 |

#### 为什么不用 axios / fetch

| 方案 | 下载对话框 | 进度条 | 内存占用 | 超时风险 |
|------|:--:|:--:|:--:|:--:|
| axios `r.data` | ❌ 等全部传完才弹 | ❌ 无 | 全部数据在 JS 堆里 | 30s 硬超时 |
| fetch `r.blob()` | ❌ 等全部传完才弹 | ❌ 无 | 全部数据在 JS 堆里 | 无（但不写磁盘） |
| **`<a>` 标签 + 一次性 ticket** | ✅ 立即弹出 | ✅ 原生进度条 | 0（浏览器直接写磁盘） | 无 |

核心洞察：**浏览器自带一个完善的下载管理器，为什么要绕过它？**

#### Nginx 流式 gzip 压缩

导出 JSON 内容（尤其是 LLM API 的请求/响应体）具有极高的压缩比——JSON 中的键名（如 `"request_body"`、`"response_headers"`）和嵌套结构高度重复，gzip 可将体积缩小 **5–10 倍**。

nginx 配置了两项关键策略：

- **`proxy_buffering off`**：字节从 FastAPI 产出后**立刻推到浏览器**，不在 nginx 内存中排队。浏览器收到的第一个字节就是 export 开始 yield 的第一个字节。
- **`gzip on` + `gzip_proxied any`**：对代理响应启用**分块 gzip**（chunked gzip encoding）。nginx 对接收到的每一段 chunk 独立压缩然后转发——流不被阻塞，但传输量减少 80-90%。

```
FastAPI yield bytes  →  nginx 收取 chunk  →  gzip 压缩  →  浏览器解压  →  写磁盘
     (原始大小)           (不缓冲)             (5-10×缩小)     (浏览器原生)
```

#### LONGTEXT 列按需选取

InnoDB 将 LONGTEXT 列存储在行外的溢出页（overflow pages）。每条记录每个 LONGTEXT 列需要**一次额外的磁盘随机读**。导出时并非所有列都需要：

```
simple 格式实际需要  →  method, path, status_code, request_body, response_body
simple 格式 defer 掉 →  id, duration_ms, reconstruction_error, created_at,
                       request_headers, response_headers, response_body_raw
```

SQLAlchemy `defer()` 让 `SELECT` 语句不拉取这些列，MySQL 也就不会去读它们的溢出页。simple 格式从 5 个 LONGTEXT 降到 2 个，**减少 ~60% 的 LONGTEXT 磁盘 I/O**。

#### 复合索引与 COUNT 加速

导出前需要 `SELECT COUNT(*)` 确定总行数（用于进度百分比），同时在流式阶段需要 `ORDER BY created_at`。单列索引 `ix_requests_port_id` 只能快速定位行，但 MySQL 仍需额外 filesort。

`ix_requests_port_method_created (port_id, method, created_at)` 是一个**覆盖索引**——三项查询条件全部命中索引：

- `port_id` — 快速定位到特定端口
- `method` — 精确过滤 API 请求（POST/PUT/PATCH/DELETE）
- `created_at` — 直接按索引顺序遍历，无需 filesort

实际效果：COUNT 从 **5.3 秒降到 <0.01 秒**（生产环境实测）。

#### 生成器异常恢复与主动断连检测

**问题背景**：FastAPI 的 `StreamingResponse` 通过 async generator 逐块推送数据。如果客户端中途断开（关闭浏览器标签、取消下载、网络中断），Starlette 向生成器注入 `GeneratorExit`；如果 MySQL 连接意外断开（超时、重启、网络故障），未捕获的异常会导致 uvicorn 输出 ASGI application error，浏览器收到损坏的 HTTP 响应。

本项目有**两条流式路径**使用断连检测，检测粒度根据场景调整：

| 路径 | 接口 | 输出格式 | 检测间隔 | `yield_per` | 连接池 |
|------|------|----------|:--:|:--:|------|
| 历史查看 | `GET /api/ports/{id}` | NDJSON | 每 20 行 | 50 | `StreamSessionLocal` (SSCursor) |
| 数据导出 | `GET /api/ports/{id}/export` | JSON | 每 100 行 | 500 | `StreamSessionLocal` (SSCursor) |

**历史查看（NDJSON）的断连检测频率更高（20 行）**，因为前端轮询和"加载更多"场景下用户频繁切换页面，及时发现断开可以更快释放 SSCursor 连接。

```mermaid
flowchart TD
    A["客户端发起请求<br/>GET /api/ports/{id}?offset=0&limit=20"] --> B["FastAPI 创建 StreamingResponse<br/>调用 async generator"]
    B --> C["创建独立 StreamSessionLocal<br/>（SSCursor 服务端游标）"]
    C --> D["查询 requests 表<br/>USE INDEX + yield_per(50)"]
    D --> E{"yield 一行记录"}
    E -->|"每 20 行检查一次"| F{"request.is_disconnected() ?"}
    F -->|"否"| D
    F -->|"是 — 客户端已断开"| G["logger.info('client disconnected')<br/>return（退出生成器）"]
    G --> H["finally 块执行清理"]

    D -->|"查询完毕"| I["正常结束 → finally"]
    I --> H

    H --> J{"own_db.close()"}
    J -->|"成功"| K["连接归还连接池 ✔️"]
    J -->|"失败（连接已损坏）"| L{"own_db.invalidate()"}
    L -->|"成功"| M["标记连接为无效<br/>连接池下次请求时自动创建新连接 ✔️"]
    L -->|"也失败"| N["logger.warning<br/>连接已被 MySQL 服务端关闭<br/>连接池自身会检测并丢弃"]

    style F fill:#fdebd0,stroke:#e67e22,color:#7e5100
    style H fill:#d5f5e3,stroke:#27ae60,color:#145a32
    style N fill:#fadbd8,stroke:#e74c3c,color:#78281f
```

**三层防御的 finally 清理**：

```python
async def stream_ndjson():
    own_db = database.StreamSessionLocal()
    try:
        # ... yield rows ...
    finally:
        try:
            own_db.close()           # 第一层：正常归还连接
        except Exception:
            try:
                own_db.invalidate()  # 第二层：连接已损坏，标记无效
            except Exception:
                logger.warning(...)  # 第三层：连接已不复存在，仅告警
```

为什么需要三层？SSCursor 连接在以下场景会损坏：

1. **客户端中途断开** → TCP RST 传到 MySQL 服务端 → 服务端关闭连接 → `close()` 时 DBAPI 发现连接已死 → 抛异常 → 进入 `invalidate()`
2. **`read_timeout` 超时** → MySQL 服务端主动断开空闲 SSCursor 连接 → `close()` 失败 → `invalidate()` 成功
3. **MySQL 重启** → 所有连接失效 → `close()` 失败 → `invalidate()` 也失败（TCP 连接已不存在）→ 仅记日志，连接池在下次 `checkout()` 时自动创建新连接

**导出路径（JSON）的额外保护**：导出生成器还在两个 `for r in query.yield_per(500)` 循环外包裹了 `try/except`。异常发生时：

1. 记录 `ERROR` 日志，包含已处理行数和异常详情
2. 生成合法的 JSON 错误标记并闭合 JSON 数组 / 对象
3. 浏览器正常完成下载——文件末尾包含 `{"_export_error": "incomplete", "rows_received": N, ...}`

用户收到的是**部分但有效的 JSON 文件**，而非一个下载失败的错误提示。

```mermaid
sequenceDiagram
    participant C as 客户端（浏览器）
    participant N as Nginx
    participant F as FastAPI Generator
    participant D as MySQL (SSCursor)

    C->>N: GET /api/ports/60/export
    N->>F: 转发请求
    F->>D: StreamSessionLocal()<br/>SELECT ... USE INDEX ...<br/>ORDER BY created_at ASC<br/>yield_per(500)

    loop 每批 500 行
        D-->>F: SSCursor 逐行拉取
        F->>F: _build_full_row(r)
        F-->>N: yield bytes
        N-->>C: gzip 压缩 → 推送
        Note over F: 每 100 行: await request.is_disconnected()
    end

    alt 客户端取消下载
        C-->>N: TCP RST
        N-->>F: GeneratorExit 或 is_disconnected()=True
        F->>F: logger.info('client disconnected')
        F->>F: finally: close() → invalidate() 降级
        Note over D: SSCursor 连接被丢弃<br/>MySQL 释放服务端游标资源
    else 正常完成
        F->>F: 生成数组结束符 ]}
        F->>F: finally: close() 归还连接
        Note over D: SSCursor 连接正常归还连接池
    end
```

#### 导出进度日志

每次导出在后端日志中输出完整的时间线追踪，方便定位瓶颈：

```
Export request received  →  auth 耗时  →  port 查找  →  COUNT 查询
Export setup done        →  总耗时分解
Export started           →  开始流式传输
Export first row         →  MySQL 查询执行耗时（time_to_first_row）
Export progress          →  每 10% 里程碑 + 区间 rows/s
Export finished          →  总耗时 + first_row vs transfer+process 分解
Export stream closed     →  SSCursor session 关闭
```

每一步都有独立计时，可以直接看到时间花在 MySQL 查询执行、数据传输、还是 Python 处理上。

#### ORDER BY 为什么不会拖慢速度

两种格式的输出都统一按 `created_at ASC` 排序。full 格式因为输出包含 `timestamp` 字段，排序对用户有意义；simple 格式虽然输出不含时间字段，但数据本身代表一个对话的时间线，按交互时间排序能保证逻辑顺序正确。

加了排序为什么不会变慢？

1. **显式强制复合索引覆盖排序**。MySQL 优化器可能出于代价估算偏差选择单个 `port_id` 索引，导致 11k+ 行 LONGTEXT 的 filesort（实测耗时 >120 秒，触发 `read_timeout` 断开）。代码中使用 `query.with_hint(RequestModel, "USE INDEX (ix_requests_port_created)")` 显式强制使用 `(port_id, created_at DESC)` 复合索引，MySQL 反向扫描该索引即可满足 ASC 排序，`EXPLAIN` 显示 `Backward index scan`，无 `Using filesort`。

2. **主键顺序 ≈ 时间顺序**。InnoDB 的主键 `id` 是 auto-increment，`created_at` 是插入时间，两者几乎线性对齐——后插入的行 id 更大、时间更晚。MySQL 回表读 LONGTEXT 溢出页时，即使在物理层面也接近顺序访问。

3. **不管加不加 ORDER BY 都要回表**。索引只存了索引列，LONGTEXT 始终在溢出页里。先用哪个索引找主键、再回表，路径不同但本质相同：找到一批行 → 读 LONGTEXT。真正的瓶颈是 LONGTEXT 的磁盘 I/O，不是磁盘寻道。

#### 编译 SQL 日志

导出启动时，日志会输出完整的编译后 SQL（含字面值绑定）：

```
Export stream: query compiled in 0.001s
  -- run this on MySQL to see the execution plan:
  -- EXPLAIN SELECT requests.method, requests.path, requests.status_code,
           requests.request_body, requests.response_body
    FROM requests
    WHERE requests.port_id = 60
      AND requests.method IN ('POST', 'PUT', 'PATCH', 'DELETE')
    ORDER BY requests.created_at ASC
```

运维人员可以直接复制这条 SQL 到生产 MySQL 执行 `EXPLAIN`，验证 `Extra` 列不含 `Using filesort`——排序走索引顺序，零额外开销。

#### 实测效果

生产环境一个拥有 12087 条 API 交互记录的端口，**经过 filesort 消除优化前**的日志（`ix_requests_port_id` 被错误选中，11k+ 行 LONGTEXT filesort）：

```
Export count query: port=60 total_rows=12087 filter=api elapsed=0.023s
Export setup done: setup_total=0.028s
Export first row: time_to_first_row=7.23s
Export progress: 1208/12087 (10%) elapsed=46.8s interval=[1208 rows in 46.8s, 26 rows/s]
Export progress: 2416/12087 (20%) elapsed=85.9s interval=[1208 rows in 39.1s, 31 rows/s]
Export progress: 3624/12087 (30%) elapsed=124.3s interval=[1208 rows in 38.4s, 31 rows/s]
...
（全部 12087 行约 6.5 分钟完成）
```

**经过 `USE INDEX (ix_requests_port_created)` 优化后**，MySQL 反向扫描索引直接按 `created_at` 顺序返回行，无需 filesort。`EXPLAIN` 验证：`Backward index scan`，无 `Using filesort`。首行返回时间从 7.23s 大幅缩短，传输速率取决于 LONGTEXT 磁盘 I/O 带宽。

#### 各优化项贡献

| 优化项 | 优化前 | 优化后 | 改善幅度 |
|--------|--------|--------|----------|
| COUNT 查询 | 5.3s | 0.023s | **230×** |
| Handler 总耗时 | 5.3s+ | 0.028s | **190×** |
| filesort 消除 | 11k+ 行 LONGTEXT filesort（>120s 超时） | Backward index scan（秒级返回首行） | **从不可用到可用** |
| 导出是否成功 | 超时报错（300s 无数据） | 稳定完成 | 从不可用到可用 |
| Python 行处理 | json.loads+dumps（~30-120s） | b"".join（~1-3s） | **30-40×** |
| LONGTEXT 列数 | 5 个（含不需要的 headers） | 2 个 | **减少 60% I/O** |
| 传输体积（gzip） | 原始 JSON | 压缩后 10-20% | **5-10×** |
| MySQL 超时 | 120s 后断开 | 600s + 心跳检测，不会无故断开 | 任意大数据量 |

#### 瓶颈分析

全部软件优化到位后，传输速度由**生产 MySQL 服务器的磁盘 I/O 能力**决定。12087 行 × 2 个 LONGTEXT（request_body + response_body）≈ 每行 ~200KB 需从 InnoDB 溢出页读取，合计约 2.4GB 的磁盘 I/O。

在代码层面，SQL 使用 `USE INDEX` 强制复合索引（`Backward index scan`，零 filesort）、列按需选取（simple 只查 2 个 LONGTEXT）、字节拼接零 CPU 开销、async 生成器 + `request.is_disconnected()` 心跳检测——所有能做的都做了。如果需要进一步加速，方向是 MySQL 服务器硬件（SSD、InnoDB buffer pool 扩容到物理内存 70-80%，使 LONGTEXT 溢出页命中缓存）或架构层面（写入时预计算导出 JSON 列）。

### 请求头转发规则

代理转发到上游 LLM API 时，以下请求头会从**转发请求**中移除（但仍完整记录到数据库 `request_headers` 字段）：

| 移除的头 | 原因 |
|----------|------|
| `host` | 替换为目标 API 的 host（如 `api.openai.com`） |
| `content-length` | httpx 自动计算，手动传递可能不匹配 |
| `connection` | httpx 自行管理 keep-alive |
| `transfer-encoding` | httpx 自行处理分块传输 |
| `content-encoding` | httpx 自动解压响应，无需透传 |
| `accept-encoding` | 显式移除，避免上游返回压缩内容 |

`Authorization` 头（API Key）默认**原样透传**，代理不修改，但会完整记录到数据库 `request_headers` 字段。

#### API Key 覆盖（按端口可选）

每个代理端口可配置专属 `api_key`（在创建/编辑代理弹窗中设置）。配置后，代理转发时会**替换**智能体原始发送的认证头：

| 原始头 | 替换为 |
|--------|--------|
| `Authorization: Bearer xxx` | `Authorization: Bearer {配置的api_key}` |
| `x-api-key: xxx` | `x-api-key: {配置的api_key}` |
| `api-key: xxx` | `api-key: {配置的api_key}` |
| `x-goog-api-key: xxx` | `x-goog-api-key: {配置的api_key}` |

- **配置了 api_key** → 替换上述任一存在的认证头；若原始请求无认证头，则自动添加 `Authorization: Bearer {api_key}`
- **未配置 api_key（NULL）** → 原样透传，不修改任何认证头
- 无论是否覆盖，原始请求头仍完整记录到数据库 `request_headers` 字段

> **使用场景**：多个智能体共用同一个代理端口时，可用系统配置的统一 api_key 替换各智能体自带的 key，避免因个别 key 失效导致请求失败。

> **实际案例**：Claude CLI（`claude-cli/2.1.98`）等客户端会在请求中携带 `accept-encoding: gzip, deflate`，表示客户端能接受压缩响应。代理在转发前移除此头，确保上游返回未压缩的原始 SSE 流——否则压缩后的二进制数据会打碎 SSE 事件边界，导致逐 chunk 透传和 JSON 重组均无法工作。原始 `accept-encoding` 值仍完整保存在数据库 `request_headers` 中，可在前端查看详情时看到。

### HTTP 协议选择：HTTP/1.1 vs HTTP/2

每个代理端口可以独立选择转发协议，创建和编辑时在弹窗中配置。系统维护两个独立的 httpx 客户端池（HTTP/1.1 和 HTTP/2），请求到达时根据端口配置自动路由。

```mermaid
flowchart TD
    A[请求到达 :3998/port/path] --> B[aget_target_url port]
    B --> C[port.prefer_http2 判断]
    C -->|"NULL 或 False"| D[get_shared_client]
    D --> E[HTTP/1.1 客户端]
    E --> F["独立 TCP 连接\nconnect=15s read=120s\nmax_connections=无上限\nkeepalive=100"]
    C -->|"True"| G[get_http2_client]
    G --> H[h2 包是否安装]
    H -->|"是"| I[HTTP/2 客户端]
    I --> J["多路复用 TCP 连接\nconnect=15s read=300s\nmax_connections=无上限\nkeepalive=100"]
    H -->|"否"| K[退回 HTTP/1.1 客户端]
    K --> L["WARNING 日志\nh2 not installed, fallback to HTTP/1.1"]
    F --> M[转发请求到上游]
    J --> M
    L --> M

    style D fill:#1a3a1a,stroke:#2ecc71,color:#2ecc71
    style E fill:#1a3a1a,stroke:#2ecc71,color:#2ecc71
    style G fill:#1a2a3a,stroke:#5dade2,color:#5dade2
    style I fill:#1a2a3a,stroke:#5dade2,color:#5dade2
    style K fill:#3a2a1a,stroke:#f39c12,color:#f1c40f
```

#### 对比

| | HTTP/1.1（默认，推荐） | HTTP/2（按需启用） |
|---|---|---|
| 连接模型 | 1 请求 = 1 TCP 连接 | 多请求复用 1 条 TCP 连接（多路复用） |
| 首次建连 | TLS 握手 ~50ms（有 keepalive 复用） | TLS 握手 ~50ms（多路复用后零开销） |
| **流中断风险** | **无**——上游只能在响应完成后关闭连接 | **存在**——上游回收连接时 GOAWAY 帧会同时断开该连接上所有正在传输的流 |
| 故障影响面 | 只影响 1 个请求 | 影响该连接上所有复用的请求（可多达几十个） |
| 并发能力 | 连接数无上限（操作系统 ulimit 决定） | 少数连接承载大量请求 |
| 适用场景 | 中转站、代理、长连接 SSE 流 | 直连 OpenAI/Anthropic 等不会激进回收连接的 API |

#### 为什么默认 HTTP/1.1

LLM API 的请求几乎全部是流式 SSE，每个响应持续数秒到数分钟。这不是"高并发短请求"的网页浏览场景，而是**长连接稳定性**场景。

**核心问题：中转站会主动关闭 HTTP/2 连接**

中转站（如 dmxapi.cn）同时服务成百上千个客户端，必须严格控制服务器资源。HTTP/2 的每个连接需要维护流状态（stream state），消耗内存和 CPU，因此中转站会设置较短的连接空闲超时（通常 30-60 秒），到期就发 GOAWAY 帧关闭连接：

```
HTTP/2 的致命场景：

  代理 ──HTTP/2──→ 中转站
  一条 TCP 连接上复用了 50 个用户的 SSE 流
       │
       ├── 用户A 的 SSE 流 (已传输 20 秒, 还剩 10 秒)
       ├── 用户B 的 SSE 流 (已传输 5 秒, 还剩 20 秒)
       └── ...
       │
       中转站："这个连接太老了，关掉" → GOAWAY → TCP RST
       │
       └── 50 个用户同时看到回复中断 ❌
           （数据已发给最终客户端，无法重试）

HTTP/1.1 无此问题：

  代理 ──HTTP/1.1──→ 中转站
  TCP连接1 → 用户A (独立, 30s 流完成后才释放)
  TCP连接2 → 用户B (独立, 25s 流完成后才释放)
  ...
  中转站回收连接1 → 只影响用户A ✅
  其余 49 个用户完全无感知 ✅
```

**换句话说**：HTTP/2 的"多路复用"在网页浏览、微服务调用中是巨大优势（省连接、低延迟）。但在 LLM 流式代理 + 中转站的场景下，它把 50 个用户拴在同一根绳子上——中转站为了资源管理剪断绳子，50 个人一起摔。

**瓶颈不在建连速度（50ms 的 TLS 握手 vs 30 秒的流式输出可忽略），而在传输稳定性。**

#### 什么时候选 HTTP/2

直连 OpenAI / Anthropic / Google 等一线模型 API 时，这些 API 知道客户端在做 LLM 推理，连接超时设得很宽松，**不会在流中途发 GOAWAY**。此时 HTTP/2 的多路复用可以在同一连接上并发多个请求，减少 TLS 握手次数。

**简单判断：目标是中转站 → HTTP/1.1，目标是模型厂商直连 API → HTTP/2。**

#### 连接复用（keepalive）

两种协议都支持连接池。一条 TCP 连接在上一个请求完成后回到池中保持温热，后续请求直接复用，省去 TLS 握手。配置：并发连接数不设上限（由操作系统 ulimit 决定），keepalive=100。

#### 重试策略与稳定性设计

```mermaid
sequenceDiagram
    autonumber
    participant A as 智能体
    participant P as LLM Proxy
    participant U as 上游 LLM API

    Note over P: httpx 双客户端<br/>HTTP/1.1 (默认) / HTTP/2 (按端口可选)

    rect rgb(240, 248, 240)
        Note over A,U: ✅ HTTP/1.1 — 正常请求，连接池复用
        A->>P: POST /12345/v1/chat/completions
        P->>P: port.prefer_http2=False → get_shared_client()
        P->>P: 从 keepalive 池取温热连接 (0ms)
        P->>U: HTTP/1.1 请求 (复用已有 TCP)
        U-->>P: 响应 (200) + SSE 流 (30s)
        P->>P: 连接归还池
        P-->>A: SSE 流式透传 ✅
    end

    rect rgb(240, 248, 240)
        Note over A,U: ✅ HTTP/2 — 直连模型厂商 API
        A->>P: POST /12345/v1/chat/completions
        P->>P: port.prefer_http2=True → get_http2_client()
        P->>U: HTTP/2 stream (多路复用, 省 TLS)
        U-->>P: 响应 (200) + SSE 流
        P-->>A: SSE 流式透传 ✅
    end

    rect rgb(253, 238, 238)
        Note over A,U: ❌ HTTP/2 中转站场景 — GOAWAY 流中断
        A->>P: POST /12345/v1/chat/completions
        P->>P: prefer_http2=True → HTTP/2 客户端
        P->>U: HTTP/2 stream (连接上已有 50 个流)
        U-->>P: SSE chunk 1, chunk 2, ... (传输中)
        P-->>A: chunk 1, chunk 2, ...

        Note over U: 中转站："连接空闲太久，回收"
        U--xP: GOAWAY (error_code:0) + TCP RST
        Note over P: 流中途断开 — 数据已发给客户端<br/>无法重试 — 用户看到回复中断 ❌
    end

    rect rgb(255, 245, 230)
        Note over A,U: ⚠️ 连接建立阶段故障 — 自动重试
        A->>P: POST /12345/v1/chat/completions
        P->>U: client.stream() → 连接已死
        U--xP: RemoteProtocolError / ConnectError
        Note over P: 第1层重试：关闭死连接 → 新建连接 → stream()
        P->>U: 全新 TCP 连接 + 请求
        U-->>P: 响应 (200) + SSE 流
        P-->>A: 正常透传 ✅
    end

    rect rgb(255, 245, 230)
        Note over A,U: ⚠️ 首字节前故障 — 自动重试
        A->>P: POST /12345/v1/chat/completions
        P->>U: 请求已发出，等待首个 chunk
        U--xP: RemoteProtocolError (尚无数据发给客户端)
        Note over P: 第2层重试：关闭 → 新建 → 重新请求
        P->>U: 全新 TCP 连接 + 请求
        U-->>P: 响应 (200) + SSE 流
        P-->>A: 正常透传 ✅
    end

    rect rgb(240, 248, 240)
        Note over A,U: ✅ 兜底 — 两次都失败
        Note over P: 返回 502 + 结构化错误信息
    end
```

| 层次 | 错误位置 | 行为 |
|------|----------|------|
| 第1层 | `stream()` / `__aenter__()` | 静默重试一次 |
| 第2层 | `aiter_bytes()` 首字节前 | 关闭死连接，新建连接，静默重试 |
| 第2层 | `aiter_bytes()` 数据已发 | 优雅终止流，日志 warning |
| 兜底 | 两次都失败 | 返回 502 + 结构化错误信息 |

关键日志（`INFO` 级别）记录在 `llm_proxy.proxy` logger 下：

```
2026-06-08 14:30:16 [INFO] llm_proxy.proxy: Retrying stream setup (attempt 2/2) — connection may be dead: ConnectError: ...
```

### 编码清洗（Surrogate 字符处理）

智能体发送的请求体或上游 API 的响应体中可能包含非法 UTF-8 字节（如 lone surrogate 字符 `U+D800–U+DFFF`）。这些字符在 Unicode 标准中是为 UTF-16 内部使用保留的，无法被 MySQL 的 `utf8mb4` 编码接受。

**自动清洗机制**（`_sanitize_text`）：

```mermaid
flowchart LR
    A[请求体/响应体 bytes] --> B[decode UTF-8 严格模式]
    B -->|成功| C[正常写入 MySQL]
    B -->|UnicodeEncodeError| D[统计 surrogate 数量]
    D --> E["replace 模式: 替换为 U+FFFD"]
    E --> F["输出日志: Replaced N surrogate(s)"]
    F --> C
```

**生效范围**：所有写入 MySQL `LONGTEXT` 列的文本字段——`request_headers`、`request_body`、`response_headers`、`response_body`、`response_body_raw`。

**日志示例**：
```
[Proxy] WARNING: request body contains surrogate characters after JSON serialization — sanitizing
[Sanitize] Replaced 1 surrogate(s), 1898074 → 1898074 chars
```

清洗后的数据前端可正常展示，非法字符位置显示为 ``。

## 许可证

本项目采用 AGPL-3.0 开源许可。使用此许可证时，你必须公开对源代码的修改、通过网络提供服务时向用户提供源代码、保留原始版权声明。

如需在闭源商业产品中使用，请联系作者获取商业许可：hcwang0025@163.com
