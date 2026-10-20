# Liber2Api

<p align="center">
  <a href="README.md">English</a> •
  <a href="README.zh-CN.md">简体中文</a> •
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <strong>高可用 LibertAI 号池管理与 OpenAI 兼容 2API 反向代理网关。</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/OpenAI-API%20Compatible-green" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg" alt="Python 3.11+">
</p>

---

## 概述

Liber2Api 为 LibertAI 模型提供标准 OpenAI 兼容反向代理网关，具备多账号负载均衡、限额自动冷却、无感容灾重试及 Web 管理控制台。

---

## ✨ 核心特性

- **OpenAI 兼容网关**：原生提供 `/v1/chat/completions` 与 `/v1/models` 端点。
- **原生工具调用**：完整支持 OpenAI `tools` 与 `tool_calls` 规范（流式与非流式）。
- **思维链平滑处理**：规范化流式思考内容输出，保证客户端解析稳定性。
- **高可用号池管理**：LRU 轮询调度、429/401 自动冷却隔离及请求级无缝容灾重试。
- **API Key 管理**：支持创建、启停与监控多客户端 API Key 及用量统计。
- **插件化邮箱网关**：支持对接 Cloudflare Email Workers、Webhooks 等接口进行自动接码建号。
- **代理路由支持**：支持通用正向代理与住宅代理粘性会话路由。

---

## 🚀 快速启动

### Docker Compose（推荐）

```yaml
services:
  liber2api:
    image: ghcr.io/likhixang/liber2api:latest
    container_name: liber2api
    restart: unless-stopped
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      - PORT=8000
      - HOST=0.0.0.0
      - DATA_DIR=/app/data
      # - ADMIN_PASSWORD=your_secure_password
      # - PROXY_URL=http://user:pass@proxy:port
    volumes:
      - ./data:/app/data
    networks:
      - docker_share

networks:
  docker_share:
    external: true
```

启动命令：

```bash
docker compose up -d
```

启动后访问 `http://localhost:8000` 进入管理控制台。

---

### Docker CLI 直接运行

```bash
mkdir -p data
docker run -d   --name liber2api   --restart unless-stopped   -p 127.0.0.1:8000:8000   -v $(pwd)/data:/app/data   ghcr.io/likhixang/liber2api:latest
```

---

### 本地开发直接运行

```bash
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

pip install -r requirements.txt
pytest -v
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ 环境变量

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `8000` | 服务监听端口 |
| `HOST` | `0.0.0.0` | 服务绑定地址 |
| `DATA_DIR` | `/app/data` | 数据持久化存储目录（SQLite 数据库） |
| `ADMIN_PASSWORD` | `""` | 控制台管理密码 |
| `API_KEY` | `""` | 初始系统 API Key |
| `PROXY_URL` | `""` | 正向 HTTP/SOCKS 代理地址 |
| `RESIN_URL` | `""` | Resin 反向网关地址 |
| `RESIN_PLATFORM_NAME` | `Default` | Resin 平台名称 |
| `COOLDOWN_HOURS` | `5` | 限额冷却时长（小时） |
| `REQUEST_TIMEOUT` | `120.0` | 上游请求超时时间（秒） |

---

## 🔌 调用示例

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="your_api_key_or_placeholder"
)

response = client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "你好！"}],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### cURL

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [{"role": "user", "content": "你好！"}],
    "stream": false
  }'
```

---

## 📋 支持的模型

- `deepseek-v4.1-flash`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`
- `qwen3.5-122b-a10b`
- `hermes-3-8b`

---

## 📄 许可证

[MIT License](LICENSE)
