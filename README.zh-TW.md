# Liber2Api

<p align="center">
  <a href="README.md">English</a> •
  <a href="README.zh-CN.md">简体中文</a> •
  <a href="README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <strong>高可用 LibertAI 帳號池管理與 OpenAI 相容 2API 反向代理閘道器。</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/OpenAI-API%20Compatible-green" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg" alt="Python 3.11+">
</p>

---

## 概述

Liber2Api 為 LibertAI 模型提供標準 OpenAI 相容反向代理閘道器，具備多帳號負載平衡、配額自動冷卻、無感容災重試及 Web 管理儀表板。

---

## ✨ 核心特性

- **OpenAI 相容閘道器**：原生提供 `/v1/chat/completions` 與 `/v1/models` 端點。
- **原生工具調用**：完整支援 OpenAI `tools` 與 `tool_calls` 規範（串流與非串流）。
- **思維鏈平滑處理**：正規化串流思考內容輸出，確保用戶端解析穩定。
- **高可用帳號池管理**：LRU 輪詢調度、429/401 自動冷卻隔離及請求級無縫容災重試。
- **API Key 管理**：支援建立、啟停與監控多用戶端 API Key 及用量統計。
- **外掛化信箱閘道器**：支援介接 Cloudflare Email Workers、Webhooks 等端點進行自動接碼建號。
- **代理路由支援**：支援通用正向代理與住宅代理黏性工作階段路由。

---

## 🚀 快速開始

### Docker Compose（推薦）

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

啟動指令：

```bash
docker compose up -d
```

啟動後存取 `http://localhost:8000` 進入管理儀表板。

---

### Docker CLI 直接運行

```bash
mkdir -p data
docker run -d   --name liber2api   --restart unless-stopped   -p 127.0.0.1:8000:8000   -v $(pwd)/data:/app/data   ghcr.io/likhixang/liber2api:latest
```

---

### 本機開發直接運行

```bash
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

pip install -r requirements.txt
pytest -v
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ 環境變數設定

| 變數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `PORT` | `8000` | 服務監聽埠號 |
| `HOST` | `0.0.0.0` | 服務繫結位址 |
| `DATA_DIR` | `/app/data` | 資料持久化儲存目錄（SQLite 資料庫） |
| `ADMIN_PASSWORD` | `""` | 儀表板管理密碼 |
| `API_KEY` | `""` | 初始系統 API Key |
| `PROXY_URL` | `""` | 正向 HTTP/SOCKS 代理位址 |
| `RESIN_URL` | `""` | Resin 反向閘道器位址 |
| `RESIN_PLATFORM_NAME` | `Default` | Resin 平台名稱 |
| `COOLDOWN_HOURS` | `5` | 限額冷卻時長（小時） |
| `REQUEST_TIMEOUT` | `120.0` | 上游請求逾時時間（秒） |

---

## 🔌 調用範例

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

## 📋 支援的模型

- `deepseek-v4.1-flash`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`
- `qwen3.5-122b-a10b`
- `hermes-3-8b`

---

## 📄 授權條款

[MIT License](LICENSE)
