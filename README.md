# Liber2Api

<p align="center">
  <a href="README.md">English</a> •
  <a href="README.zh-CN.md">简体中文</a> •
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <strong>High-availability OpenAI-compatible 2API gateway and account pool management system for LibertAI.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/OpenAI-API%20Compatible-green" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg" alt="Python 3.11+">
</p>

---

## Overview

Liber2Api provides an OpenAI-compatible reverse proxy gateway for LibertAI models, featuring multi-account load balancing, automatic rate-limit cooldown, transparent failover, and a WebUI management console.

---

## ✨ Features

- **OpenAI Compatible Gateway**: Drop-in replacement for `/v1/chat/completions` and `/v1/models`.
- **Native Function Calling**: Full support for OpenAI `tools` and `tool_calls` schemas in streaming and non-streaming modes.
- **Reasoning Stream Normalizer**: Stabilizes thinking content streams and ensures compatibility across diverse clients.
- **High-Availability Pool**: Least-recently-used (LRU) scheduling, automatic 429/401 cooldown isolation, and in-flight request failover.
- **API Key Management**: Multi-key generation, revocation, and per-key usage metrics.
- **Pluggable Mail Gateways**: Support for Cloudflare Email Workers, webhooks, and custom mail APIs for automated account provisioning.
- **Proxy Routing**: Support for upstream forward proxies and sticky residential session isolation.

---

## 🚀 Quick Start

### Docker Compose (Recommended)

Keep deployment secrets out of Git. Copy the example only when creating a new local configuration; do not overwrite an existing deployment file.

```bash
test -e docker-compose.yml || cp docker-compose.example.yml docker-compose.yml
docker network inspect docker_share >/dev/null 2>&1 || docker network create docker_share
# Fresh installations: prepare the bind mount for the image's UID.
sudo install -d -m 0700 -o 10001 -g 10001 data
# Obtain ADMIN_PASSWORD from your secret manager or export it without saving it in Git.
read -rsp "Admin password: " ADMIN_PASSWORD; export ADMIN_PASSWORD; printf '\n'
docker compose config --quiet
docker compose up -d
```

The example publishes only `127.0.0.1:8035`; use `http://localhost:8035` on the Docker host, or a trusted reverse proxy. Do not set data permissions to `0777`. Review ownership and back up existing data before changing its permissions. Management authentication and downstream API authentication are independent.

`docker-compose.yml`, `.env*` and data are local-only. The public file is `docker-compose.example.yml`. Removing a leaked secret from Git does not revoke it: rotate it separately. `/health` reports process liveness; `/ready` returns 503 when no account can be scheduled, without triggering registration.

---

### Local Development

```bash
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

pip install -r requirements.txt
pytest -v
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8000` | Application listening port |
| `HOST` | `0.0.0.0` | Host interface to bind |
| `DATA_DIR` | `/app/data` | Directory for persistent SQLite storage |
| `ADMIN_PASSWORD` | `""` | Password protecting WebUI and management APIs |
| `API_KEY` | `""` | Optional initial master API key |
| `PROXY_URL` | `""` | Outbound HTTP/SOCKS forward proxy URL |
| `RESIN_URL` | `""` | Optional Resin reverse gateway endpoint |
| `RESIN_PLATFORM_NAME` | `Default` | Resin platform selector |
| `COOLDOWN_HOURS` | `5` | Cooldown duration (hours) upon quota exhaustion |
| `REQUEST_TIMEOUT` | `120.0` | Upstream request timeout in seconds |

---


### 鉴权与监听机制 / Authentication

- `ADMIN_PASSWORD` 保护管理 API；仅可信本地开发可以留空，部署 example 强制要求设置。
- `API_KEY` 按不同值一次性导入托管令牌表；WebUI 禁用或删除后，重启不会重新启用。下游 API 鉴权开关与管理认证独立。
- `ADMIN_SESSION_TTL` 是服务端校验的会话秒数，默认 `2592000`；退出撤销当前会话，改管理密码会令旧会话失效。
- `ADMIN_ALLOWED_ORIGINS` 是逗号分隔的可信管理来源，用于特殊反代；默认同源即可。`/v1` 使用 Bearer，不使用管理 Cookie。
- HTTPS 反代需保留外部 `Host` 并设置 `X-Forwarded-Proto`，`FORWARDED_ALLOW_IPS` 仅填实际可信反代 IP/CIDR（不要填 `*`）。否则同源校验和 Secure Cookie 无法正确识别外部 HTTPS。
- Docker 入口读取 `HOST` / `PORT`；若改容器端口须同时修改 Compose 目标端口。example 内部固定 `8000`，宿主端口通过 `LIBER2API_PORT` 调整。

## 🔌 API Usage

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8035/v1",
    api_key="your_api_key_or_placeholder"
)

response = client.chat.completions.create(
    model="deepseek-v4.1-flash",
    messages=[{"role": "user", "content": "Hello!"}],
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
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

---

## 📋 Supported Models

- `deepseek-v4.1-flash`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`
- `qwen3.5-122b-a10b`
- `hermes-3-8b`

---

## 📄 License

[MIT License](LICENSE)
