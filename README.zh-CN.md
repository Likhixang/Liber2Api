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
  <img src="https://img.shields.io/badge/docker-amd64%20%7C%20arm64-blue" alt="Docker Multi-Arch">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/OpenAI-API%20Compatible-green" alt="OpenAI Compatible">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB.svg" alt="Python 3.11+">
</p>

---

## 概述

**Liber2Api** 旨在将 LibertAI 旗下的开源大模型推理能力桥接至任何标准的 OpenAI 兼容客户端、Agent 框架及开发者 SDK（例如 NextChat、Cherry Studio、Dify、LangChain、Cline 及 Claude Code 等）。

系统支持聚合管理多个 LibertAI 账号，通过最少最近使用（LRU）算法分发请求，自动处理 429/401 额度冷却与无缝容灾重试，并提供自适应明暗主题的响应式 WebUI 看板。

---

## ✨ 核心特性

- **标准 OpenAI 兼容网关**：原生提供 `/v1/chat/completions` 与 `/v1/models` 接口，即插即用接入各类 AI 应用。
- **原生工具调用 (Function Calling)**：无论流式还是非流式均原生支持标准 OpenAI `tools` 与 `tool_calls` 规范。
- **流式思维链平滑与防抖 (Stream Normalizer)**：
  - 原子化拆分单个 SSE 数据块中混杂的 `reasoning` 与 `content`。
  - 强制单向状态迁移（思考 ➔ 正文输出），彻底杜绝由于客户端状态机抖动而反复进入思考块的现象。
  - 双写支持 `delta.reasoning` 与 `delta.reasoning_content`，完美兼容 DeepSeek、OpenAI 及各开源客户端。
- **高可用号池与容灾管理**：
  - 账号池 LRU 智能负载均衡分发。
  - 自动感知 429 / 401 额度耗尽，自动进入 5 小时冷却周期。
  - 单次调用在上游触发限额时自动无缝切换至下一个健康账号，客户端完全无感知。
- **多客户端 API Key 管理**：
  - WebUI 中轻松创建、启停与监控客户端密钥，支持按密钥统计调用量。
  - 支持一键切换全局强制鉴权与免密公开模式。
- **自定义邮箱 API 接码网关**：
  - 插件化邮箱接口架构，支持挂载 Cloudflare Email Worker、私有 Webhook 及自定义 HTTP 接口。
  - 支持配置优先级、固定域名与凭据自动转发。
- **正向代理与住宅代理集成**：
  - 支持通用 HTTP/SOCKS 正向代理路由。
  - 支持 Resin 代理粘性会话路由（`Platform.Account:TOKEN`），并在注册链路动态分配独立出口 IP。
- **现代化响应式 Web 控制台**：
  - 支持深色、浅色与跟随系统三种主题。
  - 一键掩码隐藏敏感密钥与 Base URL，复制时自动还原完整内容。
  - 深度适配桌面、平板与移动端屏幕，避免排版拉厚或挤压。
- **多架构 Docker 镜像**：官方自动化构建支持 `linux/amd64` 与 `linux/arm64`。

---

## 🚀 快速启动

### 方式一：Docker Compose（推荐）

1. 创建 `docker-compose.yml`：

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
      # 可选：设置 WebUI 管理面板密码
      # - ADMIN_PASSWORD=your_secure_password
      # 可选：配置默认正向代理
      # - PROXY_URL=http://user:pass@proxy:port
    volumes:
      - ./data:/app/data
    networks:
      - docker_share

networks:
  docker_share:
    external: true
```

2. 启动容器：

```bash
docker compose up -d
```

3. 在浏览器打开 `http://localhost:8000` 即可进入管理看板。

---

### 方式二：Docker CLI 直接运行

```bash
mkdir -p data
docker run -d   --name liber2api   --restart unless-stopped   -p 127.0.0.1:8000:8000   -v $(pwd)/data:/app/data   ghcr.io/likhixang/liber2api:latest
```

---

### 方式三：本地开发直接运行

```bash
# 克隆仓库
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

# 安装依赖
pip install -r requirements.txt

# 运行自动化测试
pytest -v

# 启动服务
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ 环境变量

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `8000` | 服务监听端口 |
| `HOST` | `0.0.0.0` | 服务绑定地址 |
| `DATA_DIR` | `/app/data` | 数据持久化存储目录（SQLite 数据库） |
| `ADMIN_PASSWORD` | `""` | 管理看板访问密码；配置后 Web 页面及管理接口需鉴权 |
| `API_KEY` | `""` | 初始系统 API Key（建议直接在 Web 界面中添加管理） |
| `PROXY_URL` | `""` | 正向 HTTP/SOCKS 代理地址 |
| `RESIN_URL` | `""` | Resin 反向网关地址 |
| `RESIN_PLATFORM_NAME` | `Default` | Resin 平台名称 |
| `COOLDOWN_HOURS` | `5` | 触发 429/401 额度超限时的账号自动冷却时长（小时） |
| `REQUEST_TIMEOUT` | `120.0` | 请求上游的超时时间（秒） |

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
    messages=[
        {"role": "user", "content": "你好！请用一句话介绍你自己。"}
    ],
    stream=True
)

for chunk in response:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

### cURL 对话请求

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "deepseek-v4.1-flash",
    "messages": [
      {"role": "user", "content": "请写一个快速排序函数。"}
    ],
    "stream": false
  }'
```

### 工具调用 (Function Calling)

```bash
curl -X POST http://localhost:8000/v1/chat/completions   -H "Content-Type: application/json"   -H "Authorization: Bearer sk-liber2api-xxx"   -d '{
    "model": "qwen3.6-35b-a3b",
    "messages": [
      {"role": "user", "content": "查询东京当前的天气。"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_current_weather",
          "description": "获取指定地区的天气信息",
          "parameters": {
            "type": "object",
            "properties": {
              "location": {"type": "string", "description": "城市名"}
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

## 📋 支持的基座模型

- `deepseek-v4.1-flash` / `deepseek-v4.1-flash-1017`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`
- `qwen3.5-122b-a10b`
- `hermes-3-8b`

*注：类似 `-thinking` 或 `-tee` 后缀已由网关自动规范化合并，无需在客户端单独拼装。*

---

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源发布。
