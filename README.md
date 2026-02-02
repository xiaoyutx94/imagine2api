# Grok Imagine API Gateway

Grok 图片生成 API 代理网关，将 Grok Imagine 封装为 OpenAI 兼容的 REST API。

使用 WebSocket 直连 Grok，无需浏览器自动化，最小化资源占用。

## 功能特性

- **OpenAI 兼容 API** - 提供 `/v1/images/generations` 和 `/v1/chat/completions` 接口
- **WebSocket 直连** - 直接与 Grok 服务通信，无需 Playwright/Selenium
- **多 SSO 管理** - 支持多账号轮询，内置多种轮询策略
- **图片缓存** - 自动保存生成的图片，支持画廊预览
- **Redis 支持** - 可选的分布式会话持久化
- **代理支持** - 支持 HTTP/HTTPS/SOCKS5 代理

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 SSO

在项目根目录创建 `key.txt` 文件，每行一个 SSO Token：

```
your-sso-token-1
your-sso-token-2
```

### 3. 配置环境变量（可选）

首次运行会自动生成 `.env` 配置文件，主要配置项：

```env
# 服务器配置
HOST=0.0.0.0
PORT=9563
DEBUG=false

# API 密钥保护
API_KEY=your-secure-api-key-here

# 代理配置（可选，支持 HTTP/HTTPS/SOCKS4/SOCKS5）
# PROXY_URL=http://127.0.0.1:7890
# PROXY_URL=socks5://127.0.0.1:1080

# SSO 轮询策略: round_robin / least_used / least_recent / weighted / hybrid
SSO_ROTATION_STRATEGY=hybrid
SSO_DAILY_LIMIT=10
```

### 4. 启动服务

```bash
python main.py
```

服务将在 `http://localhost:9563` 启动。

## API 接口

### 图片生成

```bash
curl -X POST http://localhost:9563/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "prompt": "A beautiful sunset over mountains",
    "n": 1
  }'
```

### Chat Completions（OpenAI 兼容）

```bash
curl -X POST http://localhost:9563/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{
    "model": "grok-imagine",
    "messages": [{"role": "user", "content": "Generate a cat"}]
  }'
```

### 健康检查

```bash
curl http://localhost:9563/health
```

## 路由说明

| 路径 | 说明 |
|------|------|
| `/` | 服务信息 |
| `/docs` | Swagger API 文档 |
| `/health` | 健康检查 |
| `/gallery` | 图片画廊 |
| `/images/{filename}` | 静态图片访问 |
| `/v1/images/generations` | 图片生成 API |
| `/v1/chat/completions` | Chat API |
| `/admin/*` | 管理接口 |

## 项目结构

```
├── app/
│   ├── api/
│   │   ├── admin.py          # 管理接口
│   │   ├── chat.py           # Chat API
│   │   └── imagine.py        # 图片生成 API
│   ├── core/
│   │   ├── config.py         # 配置管理
│   │   └── logger.py         # 日志
│   └── services/
│       ├── grok_client.py    # Grok WebSocket 客户端
│       ├── sso_manager.py    # SSO 管理
│       └── redis_sso_manager.py  # Redis SSO 管理
├── data/
│   └── images/               # 图片缓存
├── main.py                   # 入口文件
├── requirements.txt          # 依赖
└── key.txt                   # SSO Token 文件
```

## 配置项说明

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST` | `0.0.0.0` | 服务监听地址 |
| `PORT` | `9563` | 服务端口 |
| `DEBUG` | `false` | 调试模式 |
| `API_KEY` | - | API 访问密钥 |
| `PROXY_URL` | - | 代理地址 |
| `SSO_FILE` | `key.txt` | SSO 文件路径 |
| `BASE_URL` | - | 外部访问地址 |
| `DEFAULT_ASPECT_RATIO` | `2:3` | 默认宽高比 |
| `GENERATION_TIMEOUT` | `120` | 生成超时(秒) |
| `REDIS_ENABLED` | `false` | 启用 Redis |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 地址 |
| `SSO_ROTATION_STRATEGY` | `hybrid` | 轮询策略 |
| `SSO_DAILY_LIMIT` | `10` | 每 Key 日限制 |

## 依赖

- Python 3.8+
- FastAPI
- uvicorn
- aiohttp + aiohttp-socks (WebSocket 代理支持)
- pydantic
- redis (可选)

## License

MIT
