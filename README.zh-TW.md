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
  <img src="https://img.shields.io/badge/docker-amd64%20%7C%20arm64-blue" alt="Docker Multi-Arch">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/OpenAI-API%20Compatible-green" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg" alt="Python 3.11+">
</p>

---

## 概述

**Liber2Api** 旨在將 LibertAI 旗下的開源大模型推理能力橋接至任何標準的 OpenAI 相容用戶端、Agent 框架及開發者 SDK（例如 NextChat、Cherry Studio、Dify、LangChain、Cline 及 Claude Code 等）。

系統支援集中管理多個 LibertAI 帳號，透過最少最近使用（LRU）演算法排程分發請求，自動處理 429/401 配額冷卻與無感容災重試，並提供自適應明暗主題的響應式 WebUI 儀表板。

---

## ✨ 核心特性

- **標準 OpenAI 相容閘道器**：原生提供 `/v1/chat/completions` 與 `/v1/models` 端點，即插即用接入各類 AI 應用。
- **原生工具調用 (Function Calling)**：無論串流還是非串流均原生支援標準 OpenAI `tools` 與 `tool_calls` 規範。
- **串流思維鏈平滑與防抖 (Stream Normalizer)**：
  - 原子化拆分單個 SSE 資料塊中混雜的 `reasoning` 與 `content`。
  - 強制單向狀態遷移（思考 ➔ 正文輸出），徹底杜絕由於用戶端狀態機抖動而反覆進入思考塊的問題。
  - 雙寫支援 `delta.reasoning` 與 `delta.reasoning_content`，相容 DeepSeek、OpenAI 及各開源用戶端。
- **高可用帳號池與容災管理**：
  - 帳號池 LRU 智慧負載平衡分發。
  - 自動感知 429 / 401 配額耗盡，自動進入 5 小時冷卻週期。
  - 單次請求在上游觸發限額時自動無縫容災切換至下一個就緒帳號，調用端完全無感知。
- **多用戶端 API Key 管理**：
  - WebUI 中輕鬆建立、啟停與監控用戶端金鑰，支援依金鑰統計調用次數。
  - 支援一鍵切換全域強制鑑權與免密公開模式。
- **自訂信箱 API 接碼閘道器**：
  - 外掛化信箱介面架構，支援掛載 Cloudflare Email Worker、私有 Webhook 及自訂 HTTP 端點。
  - 支援配置優先權、固定網域與認證憑據自動轉發。
- **正向代理與住宅代理整合**：
  - 支援通用 HTTP/SOCKS 正向代理路由。
  - 支援 Resin 代理黏性工作階段路由（`Platform.Account:TOKEN`），並在註冊流程動態分配獨立出口 IP。
- **現代化響應式 Web 控制台**：
  - 支援深色、淺色與跟隨系統三種主題。
  - 一鍵遮罩隱藏敏感金鑰與 Base URL，複製時自動還原完整內容。
  - 深度適配桌面、平板與行動裝置螢幕，避免版面被拉厚或擠壓。
- **多架構 Docker 映像檔**：官方自動化構建支援 `linux/amd64` 與 `linux/arm64`。

---

## 🚀 快速開始

### 方式一：Docker Compose（推薦）

1. 建立 `docker-compose.yml`：

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
      # 可選：設定 WebUI 管理密碼
      # - ADMIN_PASSWORD=your_secure_password
      # 可選：配置預設正向代理
      # - PROXY_URL=http://user:pass@proxy:port
    volumes:
      - ./data:/app/data
    networks:
      - docker_share

networks:
  docker_share:
    external: true
```

2. 啟動容器：

```bash
docker compose up -d
```

3. 在瀏覽器開啟 `http://localhost:8000` 即可進入管理儀表板。

---

### 方式二：Docker CLI 直接運行

```bash
mkdir -p data
docker run -d   --name liber2api   --restart unless-stopped   -p 127.0.0.1:8000:8000   -v $(pwd)/data:/app/data   ghcr.io/likhixang/liber2api:latest
```

---

### 方式三：本機開發直接運行

```bash
# 複製儲存庫
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

# 安裝相依套件
pip install -r requirements.txt

# 執行自動化測試
pytest -v

# 啟動服務
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ 環境變數設定

| 變數名稱 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `PORT` | `8000` | 服務監聽埠號 |
| `HOST` | `0.0.0.0` | 服務繫結位址 |
| `DATA_DIR` | `/app/data` | 資料持久化儲存目錄（SQLite 資料庫） |
| `ADMIN_PASSWORD` | `""` | 管理儀表板存取密碼；設定後 Web 頁面與管理介面需認證 |
| `API_KEY` | `""` | 初始系統 API Key（建議直接在 Web 介面中新增管理） |
| `PROXY_URL` | `""` | 正向 HTTP/SOCKS 代理位址 |
| `RESIN_URL` | `""` | Resin 反向閘道器位址 |
| `RESIN_PLATFORM_NAME` | `Default` | Resin 平台名稱 |
| `COOLDOWN_HOURS` | `5` | 觸發 429/401 配額超限時的帳號自動冷卻時長（小時） |
| `REQUEST_TIMEOUT` | `120.0` | 請求上游的逾時時間（秒） |

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
    messages=[
        {"role": "user", "content": "你好！請用一句話介紹你自己。"}
    ],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### cURL 對話請求

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {"role": "user", "content": "請寫一個快速排序函式。"}
    ],
    "stream": false
  }'
```

### 工具調用 (Function Calling)

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "qwen3.6-35b-a3b",
    "messages": [
      {"role": "user", "content": "查詢東京目前的天氣。"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_current_weather",
          "description": "取得指定地區的天氣資訊",
          "parameters": {
            "type": "object",
            "properties": {
              "location": {"type": "string", "description": "城市名稱"}
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

## 📋 支援的基底模型

- `deepseek-v4.1-flash` / `deepseek-v4.1-flash-1017`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`
- `qwen3.5-122b-a10b`
- `hermes-3-8b`

*註：類似 `-thinking` 或 `-tee` 後綴已由閘道器自動正規化合併，無須在用戶端個別組裝。*

---

## 📄 授權條款

本專案採用 [MIT License](LICENSE) 開源發布。
