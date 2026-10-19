# Liber2Api

<p align="center">
  <strong>高可用 LibertAI 号池管理与 OpenAI 兼容 2API 代理网关</strong><br>
  带现代暗色 WebUI、人工两步录入、无感 Token 自动轮转、429 智能冷却与无缝故障转移。原生支持 Function Calling（工具调用）与流式输出。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License">
  <img src="https://img.shields.io/badge/docker-amd64%20%7C%20arm64-blue" alt="Docker Multi-Arch">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688.svg" alt="FastAPI">
  <img src="https://img.shields.io/badge/vLLM-Tools%20Native-orange" alt="Tools Native">
</p>

---

## ✨ 核心特性

- 🛠 **原生支持 Function Calling (Tools)**：底层对接官方 vLLM 引擎，无论是流式（Stream）还是非流式，均 100% 原生支持 OpenAI 工具调用格式，完美适配各类 Agent（如 Claude Code, Cline, Dify, LangChain 等）。
- 🖥 **轻量优雅的 WebUI 号池看板**：内嵌现代暗黑风格响应式 Web 控制台，实时透视各账号状态（正常、冷却中、失效）、调用量与成功率。
- ✉️ **极简两步向导人工录入**：拒绝脆弱易封号的批量接码爬虫。在 WebUI 中「输邮箱 ➔ 收 6 位验证码 ➔ 提交入库」，15 秒即可绑定一个真实有效账号。
- 🔄 **官方规范 Token 自动轮转保活**：基于 LibertAI 原生 `Token Rotation` 规范，后台定时自动刷新 `refresh_token` 并更新 `chatApiKey`，**一次录入，长期无感持久保活**。
- 🛡 **429 自动冷却与无缝故障转移 (Failover)**：
  - 当账号遇到 5 小时用量上限（HTTP 429）时，自动将其标记为冷却（默认 5 小时）；
  - **在当前请求内立即自动切换至下一个就绪账号重试**，下游调用方完全无感知；
  - 冷却时间结束后自动解冻恢复调度。
- 🐳 **双架构 Docker 镜像支持**：通过 GitHub Actions 自动化构建，官方原生支持 `linux/amd64` 与 `linux/arm64`。

---

## 🏗 系统架构图

```text
               ┌──────────────────────────────────────────────┐
               │         客户端 / Agent / 第三方聚合平台        │
               │ (NextChat, Cherry Studio, Dify, One API...) │
               └──────────────────────┬───────────────────────┘
                                      │ 标准 OpenAI 协议 (/v1/chat/completions)
                                      ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                              Liber2Api 网关                                 │
│                                                                            │
│   ┌──────────────────────────┐          ┌──────────────────────────────┐   │
│   │       WebUI 管理看板      │          │       负载均衡与调度引擎       │   │
│   │   - 人工两步录入新账号   │          │   - Least-Recently-Used 轮询 │   │
│   │   - 状态/冷却/用量监控   │          │   - 429 自动冷却 (5小时)     │   │
│   │   - 手动连通性与刷新测试 │          │   - 自动故障转移无缝重试     │   │
│   └────────────┬─────────────┘          └──────────────┬───────────────┘   │
│                │                                       │                   │
│                ▼                                       ▼                   │
│   ┌──────────────────────────┐          ┌──────────────────────────────┐   │
│   │      SQLite 账号池存储    │ ◄─────── │     后台自动化保活 Worker    │   │
│   │    (持久化挂载至卷目录)  │          │   - 定期执行 Token 轮转      │   │
│   └──────────────────────────┘          │   - 自动解冻冷却到期账号     │   │
│                                         └──────────────────────────────┘   │
└───────────────────────────────────────┬────────────────────────────────────┘
                                        │ 注入有效 chatApiKey 转发
                                        ▼
                  https://api.libertai.io/v1/chat/completions
                     (Qwen 3.6/3.8, GLM-5.3, DeepSeek V4.1...)
```

---

## 🚀 快速启动

### 方式一：Docker Compose（推荐）

1. 创建 `docker-compose.yml` 文件：

```yaml
services:
  liber2api:
    image: ghcr.io/likhixang/liber2api:latest
    container_name: liber2api
    restart: unless-stopped
    ports:
      # 绑定 127.0.0.1 供反代访问；若需局域网或公网直接访问可改为 8000:8000
      - "127.0.0.1:8000:8000"
    environment:
      - PORT=8000
      - HOST=0.0.0.0
      - DATA_DIR=/app/data
      # 可选：如果希望保护 WebUI 管理面板，取消注释并设置密码
      # - ADMIN_PASSWORD=your_admin_secret
      # 可选：如果希望限制下游客户端调用，取消注释并设置 API_KEY
      # - API_KEY=sk-liber2api-secret
      # 429 触发后的冷却时间（小时），默认 5 小时
      - COOLDOWN_HOURS=5
      # 账号 Token 自动后台保活轮转周期（秒），默认 12 小时 (43200)
      - AUTO_REFRESH_INTERVAL=43200
    volumes:
      - ./data:/app/data
    networks:
      - docker_share

networks:
  docker_share:
    external: true
```

2. 启动服务：

```bash
docker compose up -d
```

3. 访问 WebUI 管理面板：浏览器打开 `http://<你的IP>:8000`。

---

### 方式二：Docker CLI 直接运行

```bash
mkdir -p data
docker run -d \
  --name liber2api \
  --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v $(pwd)/data:/app/data \
  ghcr.io/likhixang/liber2api:latest
```

---

### 方式三：本地开发直接运行

```bash
# 1. 克隆代码
git clone https://github.com/Likhixang/Liber2Api.git
cd Liber2Api

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行测试
pytest -v

# 4. 启动服务
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## ⚙️ 环境变量配置

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `8000` | 容器/服务监听端口 |
| `HOST` | `0.0.0.0` | 服务绑定地址 |
| `DATA_DIR` | `/app/data` | 数据持久化目录（存储 SQLite 数据库） |
| `ADMIN_PASSWORD` | `""` (留空) | WebUI 管理密码。设置后，WebUI 接口调用需校验 `x-admin-password` |
| `API_KEY` | `""` (留空) | 下游客户端请求 2API 必须提供的 Key；留空则任何人均可直接调用 |
| `COOLDOWN_HOURS` | `5` | 单个账号触发 429 后的冷却时长（小时） |
| `AUTO_REFRESH_INTERVAL` | `43200` | 后台定时保活刷新 Token 的周期（秒，默认 12 小时） |
| `MAX_FAILOVER_RETRIES` | `3` | 单次请求在上游异常时最大无感切换重试账号数 |
| `REQUEST_TIMEOUT` | `120.0` | 上游转发请求超时时间（秒） |

---

## 🔌 客户端接入指南

### 1. 基础配置参数

* **接口地址 (Base URL)**: `http://<你的服务器IP>:8000/v1`
* **API Key**: 若未配置 `API_KEY` 环境变量，可随意填写（如 `any-key`）；若已配置，则填写对应的 Key。

### 2. cURL 调用示例 (普通对话)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "qwen3.6-35b-a3b",
    "messages": [
      {"role": "user", "content": "你好，请用一句话介绍你自己！"}
    ],
    "stream": false
  }'
```

### 3. cURL 调用示例 (工具调用 Function Calling)

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer any-key" \
  -d '{
    "model": "qwen3.6-35b-a3b",
    "messages": [
      {"role": "user", "content": "帮我查一下东京现在的天气"}
    ],
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "get_weather",
          "description": "查询指定城市的天气",
          "parameters": {
            "type": "object",
            "properties": {
              "location": {"type": "string", "description": "城市名称"}
            },
            "required": ["location"]
          }
        }
      }
    ],
    "tool_choice": "auto"
  }'
```

### 4. 支持的模型列表

你可通过 `GET /v1/models` 获取实时在架模型列表，常用模型包括：

* `qwen3.6-35b-a3b` / `qwen3.6-35b-a3b-thinking`
* `qwen3.8-27b` / `qwen3.8-27b-thinking`
* `deepseek-v4.1-flash` / `deepseek-v4.1-flash-1017-thinking`
* `glm-5.3-flash` / `glm-5.3-flash-thinking`
* `glm-5.3` / `glm-5.3-thinking`
* `hermes-3-8b-tee`

---

## ❓ 常见问题

### Q1: 账号入库后会自动过期吗？
**不会。**
系统后台内置了定时保活协程。由于 LibertAI 服务端支持标准的 Token Rotation（每次通过 `POST /auth/refresh` 用当前 `refresh_token` 即可置换出一对全新的 `access_token` 和 `refresh_token`），系统会在旧凭证过期前自动轮换，无需人工干预。

### Q2: 账号触发 429 后会怎样？
当账号遇到服务端的滑动窗口用量超限（429）时，Liber2Api 会将该账号置入冷却队列（默认 5 小时），并**在同一个用户请求内自动拉取下一个空闲的就绪账号继续执行**，调用方不会报错。冷却到期后，系统自动唤醒该账号重回就绪号池。

---

## 📄 License

本项目基于 [MIT License](LICENSE) 开源发布。
