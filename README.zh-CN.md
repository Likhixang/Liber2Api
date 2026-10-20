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

部署凭据不进入 Git。首次部署从 example 复制；已有本地配置不要覆盖。

```bash
test -e docker-compose.yml || cp docker-compose.example.yml docker-compose.yml
docker network inspect docker_share >/dev/null 2>&1 || docker network create docker_share
# 仅首次部署：给镜像 UID 10001 准备数据目录。
sudo install -d -m 0700 -o 10001 -g 10001 data
# 从 secrets 工具注入或交互输入，不把密码写进版本控制。
read -rsp "Admin password: " ADMIN_PASSWORD; export ADMIN_PASSWORD; printf '\n'
docker compose config --quiet
docker compose up -d
```

example 仅发布 `127.0.0.1:8035`，在宿主机访问 `http://localhost:8035`，或通过可信反代访问。不要使用 `0777` 数据目录。已有数据改权限前先核对属主并备份。管理密码与下游 API 鉴权是两个独立机制。

`docker-compose.yml`、`.env*` 和数据仅在本地保留；公开文件为 `docker-compose.example.yml`。从 Git 移除已泄露密码不会让密码失效，需单独轮换。`/health` 仅表示进程存活；`/ready` 在无可调度账号时返回 503，不触发注册。

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


### 鉴权与监听机制 / Authentication

- `ADMIN_PASSWORD` 保护管理 API；仅可信本地开发可以留空，部署 example 强制要求设置。
- `API_KEY` 按不同值一次性导入托管令牌表；WebUI 禁用或删除后，重启不会重新启用。下游 API 鉴权开关与管理认证独立。
- `ADMIN_SESSION_TTL` 是服务端校验的会话秒数，默认 `2592000`；退出撤销当前会话，改管理密码会令旧会话失效。
- `ADMIN_ALLOWED_ORIGINS` 是逗号分隔的可信管理来源，用于特殊反代；默认同源即可。`/v1` 使用 Bearer，不使用管理 Cookie。
- HTTPS 反代需保留外部 `Host` 并设置 `X-Forwarded-Proto`，`FORWARDED_ALLOW_IPS` 仅填实际可信反代 IP/CIDR（不要填 `*`）。否则同源校验和 Secure Cookie 无法正确识别外部 HTTPS。
- Docker 入口读取 `HOST` / `PORT`；若改容器端口须同时修改 Compose 目标端口。example 内部固定 `8000`，宿主端口通过 `LIBER2API_PORT` 调整。

## 🔌 调用示例

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8035/v1",
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
curl -X POST http://localhost:8035/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
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
