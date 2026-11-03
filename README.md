# perch-openai-adapter

把 Perch 的 ACP 会话包装成一个标准的 OpenAI 兼容 API。真流式、真多轮记忆、能读图、工具执行轨迹可见。

纯标准库，零依赖。一个 HTTP 层，一个 ACP 客户端。

```powershell
python perch_openai_adapter.py --port 8791 --mode agents
```

然后用任何 OpenAI 客户端指过去：

```
base_url = http://127.0.0.1:8791/v1
api_key  = unused
```

---

## 为什么是 ACP 而不是 `perch run --json`

实测过。`perch run --json` 是全缓冲的：54,062 字符的答案在 t=8.50s 一次性到达，跨度 0.01s。切开它就是假流式——用户看着我们一点点吐早就拿到的文本。

`perch acp` 走 JSON-RPC 2.0 over stdio，模型产出时逐块发 `session/update` → `agent_message_chunk`。实测 7 块 / 1.88s。这是真流式。

---

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | `stream` 支持 `true` / `false` |
| GET | `/v1/models` | 四个 tier |
| GET | `/v1/conversations` | 活会话状态和轮数 |
| DELETE | `/v1/conversations/<key>` | 关掉一个会话 |
| GET | `/healthz` | 完整能力声明 |

---

## 多轮记忆

会话键取自 `user` 字段，或 `X-Conversation-Id` / `X-Session-Id` 请求头。一个键映射一个长活 ACP 会话，Perch 自己记上下文，调用方只发新消息。

```python
client.chat.completions.create(
    model="standard", user="my-conv-1",
    messages=[{"role": "user", "content": "My access code is KESTREL-7."}],
)
# 第二轮：不用重传历史
client.chat.completions.create(
    model="standard", user="my-conv-1",
    messages=[{"role": "user", "content": "What is my access code?"}],
)
```

有界，不会无限涨：

| 限制 | 默认 | 说明 |
|---|---|---|
| `--max-sessions` | 8 | 超了按 LRU 关最旧的 |
| `--ttl` | 1800s | 空闲超时关闭 |
| `max_turns` | 40 | 单会话 40 轮后回收 |

不传会话键就是每请求独立会话，靠 `messages` 数组传历史——也能用，只是每次都要重传。

---

## 推理和正文分开

Perch 的 ACP 有两条独立文本流，适配层按字段分流：

| ACP 通知 | → | OpenAI 字段 |
|---|---|---|
| `agent_message_chunk` | → | `delta.content` |
| `agent_thought_chunk` | → | `delta.reasoning_content` |
| `tool_call_update` | → | `perch.toolActivity` |

正文里不掺任何东西。工具轨迹走旁路，不污染答案，也不会随历史回传。

---

## 图片

ACP 初始化返回 `promptCapabilities: {image: true}`。按 OpenAI 标准格式发 `image_url` 即可，支持 `data:` URL 和 http URL，png / jpeg / webp / gif 自动识别。

只取最新一轮的图。历史消息里的图会被忽略，因为 ACP 会话已经持有上下文，重发浪费 token。

---

## 工具调用是什么状态

Perch 是 agent，它自己执行工具。ACP 发 `tool_call` / `tool_call_update` 通知，里面带工具名、参数、真实输出。适配层把这些映射成 `perch.toolActivity`。

**不发 OpenAI 的 `tool_calls`。** 那个字段的语义是「客户端你去执行」，而 Perch 已经执行完了。发了会让客户端去找一个叫 `execute` 的工具，报 `unknown tool` 然后卡死。

所以这是「可视化的 agent 执行」，不是客户端驱动的 tool calling。你在 `tools` 参数里配的定义不会让 Perch 去调你的工具，它用自己那套工具集。

---

## 模型

四个 tier，这是 Perch 模型层的真实结构，不是适配层的限制：

```
standard       Standard        ctx=200000
standard 1m    Standard 1M     ctx=1000000
pro            Pro             ctx=200000
pro 1m         Pro 1M          ctx=1000000
```

旧名兼容：`standard_max` → `standard 1m`，`pro@max` → `pro`（忽略 effort 后缀）。乱填回退 `standard`。

---

## 配置

| 环境变量 | 作用 |
|---|---|
| `PERCH_BIN` | `perch` 可执行文件路径。默认从 PATH 找，再回退 npm 全局安装位置 |
| `NODE_BIN` | `node` 可执行文件路径。默认从 PATH 找 |

启动参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8791 | 监听端口 |
| `--mode` | `ask` | `ask` 只读；`agents` 允许写文件和跑命令 |
| `--max-sessions` | 8 | 并发会话上限 |
| `--ttl` | 1800 | 会话空闲超时（秒） |

**`--mode agents` 很重要。** 不加的话默认 `ask`（只读），客户端让它写文件、跑命令都会被拒。

---

## 接 DeepSeek Harness

见 [`HARNESS-WIRING.md`](HARNESS-WIRING.md)，里面有逐字段填法和排错。图片支持的那个坑见 [`HARNESS-IMAGE-FIX.yml`](HARNESS-IMAGE-FIX.yml)。

---

## 已知限制

这些是真限制，不是没做：

- **首 token 3–6 秒。** ACP 会话启动 + 模型首响应。流式改善的是「看到第一个字的时间」，不是总时长。
- **chunk 粒度粗。** 一次 3–6 字符到十几行。Perch 的分块策略，改不了。
- **工具由 Perch 执行。** 客户端能看见、能拿到结果，但不能注入自己的工具让 Perch 调用。
- **usage 是估算。** 字符数 / 4。

---

## 边界

用的是**你自己的 Perch 账号**，跑在**你自己的机器上**，调的是**官方 ACP 入口**（Zed / JetBrains 用的同一协议）。不读 token、不冒充桌面客户端、不调未公开端点。

**别把 8791 暴露到公网。** 这个适配层没有鉴权，任何能连上端口的人都能用你的 Perch 额度。

---

## 文件

| 文件 | 作用 |
|---|---|
| `perch_openai_adapter.py` | HTTP 层，OpenAI 格式 ↔ ACP |
| `perch_acp.py` | ACP 客户端 + `SessionStore`（会话保持）+ `AcpPool`（无状态） |

---

## 前提

- Python 3.10+（用了 `X | None` 类型语法）
- Node.js
- Perch CLI：`npm install -g perchai-cli`，且已登录
