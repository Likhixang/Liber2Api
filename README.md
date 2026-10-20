# Liber2Api

<p align="center">
  <a href="README.md">English</a> •
  <a href="README.zh-CN.md">简体中文</a> •
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <strong>High-availability OpenAI-compatible 2API reverse proxy gateway and account pool management system for LibertAI.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/docker-amd64%20%7C%20arm64-blue" alt="Docker Multi-Arch">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/OpenAI-API%20Compatible-green" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg" alt="Python 3.11+">
</p>

---

## Overview

**Liber2Api** bridges LibertAI's open-weights model inference services to any standard OpenAI-compatible client, developer SDK, or agent framework (such as NextChat, Cherry Studio, Dify, LangChain, Cline, and Claude Code).

It pools multiple LibertAI accounts, balances traffic using least-recently-used (LRU) scheduling, manages rate-limit cooldown windows, and delivers a fully responsive WebUI dashboard for pool observability and key configuration.

---

## ✨ Features

- **OpenAI Compatible Gateway**: Drop-in replacement for `/v1/chat/completions` and `/v1/models`, allowing seamless integration with any modern AI application or agent framework.
- **Native Function Calling & Tools**: Direct support for standard OpenAI `tools` and `tool_calls` formats in both streaming and non-streaming modes.
- **Stream Reasoning Normalizer**:
  - Atomically separates mixed `reasoning` and `content` payloads within single SSE chunks.
  - Enforces monotonic state transition (thinking ➔ response) to prevent thinking resumption jitter.
  - Dual-writes `delta.reasoning` and `delta.reasoning_content` for cross-client compatibility (DeepSeek, OpenAI, Cherry Studio).
- **High-Availability Pool Management**:
  - Least-recently-used scheduling across registered accounts.
  - Automatic quota exhaustion detection (HTTP 429 / 401) with smart 5-hour cooldown.
  - In-flight automatic failover to the next healthy account without dropping client requests.
- **Client API Key Management**:
  - WebUI interface to create, enable, disable, and monitor named client API keys with usage tracking.
  - Switchable global authentication policy (mandatory Bearer key or public access).
- **Custom Mail Provider Gateway**:
  - Pluggable mail provider architecture supporting custom Cloudflare Email Workers, private webhooks, and custom endpoints.
  - Configurable priority tiers, domain filters, and credential forwarding.
- **Forward & Residential Proxy Integration**:
  - Support for upstream HTTP/SOCKS proxies.
  - Resin proxy sticky session routing (`Platform.Account:TOKEN`) with dynamic exit IP assignment for registration.
- **Modern Responsive Dashboard**:
  - Dark, light, and system theme modes.
  - One-click privacy mask toggle for sensitive keys and endpoints.
  - Fully responsive layout optimized for desktop, tablet, and mobile browsers.
- **Multi-Architecture Docker Build**: Official support for both `linux/amd64` and `linux/arm64`.

---

## 🚀 Quick Start

### Docker Compose (Recommended)

1. Create a `docker-compose.yml` file:

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
      # Optional: set WebUI admin password
      # - ADMIN_PASSWORD=your_secure_password
      # Optional: default upstream proxy URL
      # - PROXY_URL=http://user:pass@proxy:port
    volumes:
      - ./data:/app/data
    networks:
      - docker_share

networks:
  docker_share:
    external: true
```

2. Start the service:

```bash
docker compose up -d
```

3. Open `http://localhost:8000` in your browser to access the WebUI dashboard.

---

### Docker CLI

```bash
mkdir -p data
docker run -d   --name liber2api   --restart unless-stopped   -p 127.0.0.1:8000:8000   -v $(pwd)/data:/app/data   ghcr.io/likhixang/liber2api:latest
```

---

### Local Development

```bash
# Clone the repository
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

# Install dependencies
pip install -r requirements.txt

# Run test suite
pytest -v

# Start application
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ Configuration

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `8000` | Port on which the application server listens |
| `HOST` | `0.0.0.0` | Host interface to bind |
| `DATA_DIR` | `/app/data` | Directory for persistent SQLite storage |
| `ADMIN_PASSWORD` | `""` | Password protecting WebUI and management APIs |
| `API_KEY` | `""` | Optional initial master API key |
| `PROXY_URL` | `""` | Outbound HTTP/SOCKS forward proxy URL |
| `RESIN_URL` | `""` | Optional Resin reverse gateway endpoint |
| `RESIN_PLATFORM_NAME` | `Default` | Resin platform selector |
| `COOLDOWN_HOURS` | `5` | Duration (in hours) accounts remain in cooldown upon 429/401 quota limit |
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
    messages=[
        {"role": "user", "content": "Hello! Introduce yourself in one sentence."}
    ],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### cURL Chat Completion

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {"role": "user", "content": "Write a quick Python sort function."}
    ],
    "stream": false
  }'
```

### Function Calling (Tools)

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "qwen3.6-35b-a3b",
    "messages": [
      {"role": "user", "content": "What is the current weather in Tokyo?"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_current_weather",
          "description": "Get current weather conditions for a location",
          "parameters": {
            "type": "object",
            "properties": {
              "location": {"type": "string", "description": "City name"}
            },
            "required": ["location"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

---

## 📋 Supported Base Models

- `deepseek-v4.1-flash` / `deepseek-v4.1-flash-1017`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`
- `qwen3.5-122b-a10b`
- `hermes-3-8b`

*Note: Suffixes such as `-thinking` or `-tee` are automatically unified and resolved by the gateway based on client parameters.*

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
