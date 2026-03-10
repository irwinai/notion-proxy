# Notion AI Proxy

本地反向代理，将 Notion AI 封装为 HTTP API。

## 两种方案

| 方案 | 文件 | 原理 | 优点 | 缺点 |
|------|------|------|------|------|
| **HTTP 直接请求** | `notion_proxy_http.py` | curl_cffi 模拟浏览器直接调 Notion API | 速度快、不操控 UI | 启动时需自动触发一条消息捕获模板 |
| DOM 操控 | `notion_proxy.py` | CDP 操控 Notion UI + fetch hook 读流 | 最稳定 | 速度慢、会操控界面 |

> 💡 推荐使用 **HTTP 直接请求方案**，速度更快且不干扰 Notion 界面。

## ✨ 零配置

**不需要填写任何用户信息。** 不需要 API Key、Token、User ID、Space ID、邮箱等任何配置。所有认证信息通过 CDP 从 Notion 客户端自动获取。

## 前置条件

- Notion 桌面客户端（macOS）
- 以 CDP 调试模式启动 Notion：
  ```bash
  open -a Notion --args --remote-debugging-port=9333
  ```
- **HTTP 方案额外要求**: 启动前需打开 Notion AI 聊天页面
- Python 3.9+

## 安装

```bash
pip install -r requirements.txt
```

## 启动

```bash
# 推荐: HTTP 直接请求方案
python3 notion_proxy_http.py

# 备选: DOM 操控方案
python3 notion_proxy.py

# 服务运行在 http://0.0.0.0:8100
```

## API 接口

### 健康检查

```bash
curl http://localhost:8100/health
```

### 聊天（同步）

```bash
curl -X POST http://localhost:8100/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "你好"}'
```

**响应:**
```json
{"reply": "你好！有什么我可以帮你的吗？"}
```

### 聊天（流式 SSE）

```bash
curl -N -X POST http://localhost:8100/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "你好"}'
```

**响应（逐 token 推送）:**
```
data: {"token": "你好"}
data: {"token": "！有什么"}
data: {"token": "我可以帮你的吗？"}
data: [DONE]
```

## 技术细节

### HTTP 直接请求方案

1. 启动时通过 CDP 注入 `fetch` hook
2. 自动触发一条 AI 消息，捕获 Notion 原生请求体作为模板
3. 后续请求基于模板 `deepcopy` + 替换 message/traceId 等动态字段
4. 通过 `curl_cffi`（Chrome TLS 指纹模拟）直接发送 HTTP 请求
5. 解析 NDJSON patch 流提取 AI 回复文本

> ⚠️ 关键发现：Notion 后端要求 `threadId` 必须是系统内已知 ID，标准 UUID 会被拒绝。因此必须通过捕获原生请求获取合法的 threadId。

### DOM 操控方案

1. 注入 `fetch` hook 拦截 AI 响应流
2. 操控 DOM 在 Notion AI 面板中输入并发送消息
3. 读取 NDJSON patch 流，解析提取 AI 回复文本

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CDP_PORT` | `9333` | Notion CDP 调试端口 |

## 注意事项

- 同一时间只支持一个请求（内部加锁）
- 需要保持 Notion 客户端运行
- HTTP 方案启动后首次请求会稍慢（需等模板捕获完成）
