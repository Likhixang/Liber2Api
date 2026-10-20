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

Run:

```bash
docker compose up -d
```

Access the dashboard at `http://localhost:8000`.

---

### Docker CLI

```bash
mkdir -p data
docker run -d   --name liber2api   --restart unless-stopped   -p 127.0.0.1:8000:8000   -v $(pwd)/data:/app/data   ghcr.io/likhixang/liber2api:latest
```

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

## 🔌 API Usage

### OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
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
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
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
