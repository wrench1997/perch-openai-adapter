# Perch → OpenAI 兼容 API 适配层

把 Perch 订阅的额度变成标准 OpenAI API。**真流式、真工具调用、真多轮记忆、能读图。**

---

## 四个之前说"做不到"的，现在都修了

### 1. 工具调用 → 现在能看见，而且带真实结果

之前说"Harness 的工具注册表不生效"，这是对的，但结论错了。Perch 是 agent，它**自己执行工具**，ACP 会发 `tool_call` / `tool_call_update` 通知，里面有：

```json
{
  "sessionUpdate": "tool_call_update",
  "toolCallId": "chatcmpl-tool-98bb...",
  "title": "Shell command · Get-ChildItem -Force -LiteralPath 'C:\\Users\\Texie\\Desktop'...",
  "kind": "execute",
  "status": "completed",
  "rawInput": {
    "command": "Get-ChildItem -LiteralPath 'C:\\Users\\Texie\\Desktop' -File -Force",
    "cwd": "C:\\Users\\Texie\\Desktop"
  },
  "content": [{"type": "content", "content": {"type": "text", "text": "...实际输出..."}}]
}
```

适配层把它映射成 OpenAI 的 `tool_calls` 格式发出去：

```
tool_calls surfaced: 1
  name=execute
  args={"command": "Get-ChildItem -LiteralPath 'C:\\Users\\Texie\\Desktop' -File -Force; ..."}
```

**所以现在的状态是：** 工具由 Perch 决定和执行，但客户端能完整看到用了什么工具、传了什么参数、拿回什么结果。不是客户端驱动的 tool calling，是「可视化的 agent 执行」。

**一个坑我踩了：** ACP 把一个工具调用拆成多条通知——第一条带 `title`/`kind`/`rawInput`，后面几条只有 `status` 和 `content`。必须按 `toolCallId` 合并再发，否则客户端看到的是空调用（我第一版就是这样，显示成 `name=tool, args={"title":"tool"}`）。

### 2. 上下文不共享 → 现在有真会话记忆

之前每个请求独立 ACP 会话，历史全靠 `messages` 数组重传。现在加了 `SessionStore`：一个会话键映射一个长活 ACP 会话，Perch 自己记住上下文。

```python
# 第一轮
client.chat.completions.create(
    model="standard", user="my-conv-1",
    messages=[{"role": "user", "content": "My access code is KESTREL-7."}],
)
# 第二轮：只发新消息，不用重传历史
client.chat.completions.create(
    model="standard", user="my-conv-1",
    messages=[{"role": "user", "content": "What is my access code?"}],
)
```

实测：

```
turn 1: 'Acknowledged.'          sessionTurns=1
turn 2: 'KESTREL-7'              sessionTurns=2
remembered across turns: YES
```

会话键来源：`user` 字段，或 `X-Conversation-Id` / `X-Session-Id` 请求头。

**有界，不会无限涨：**

| 限制 | 默认 | 说明 |
|---|---|---|
| `--max-sessions` | 8 | 超了按 LRU 关掉最旧的 |
| `--ttl` | 1800s | 空闲超时关闭 |
| `max_turns` | 40 | 单会话 40 轮后回收，防止撑爆上下文 |

查看和清理：

```
GET    /v1/conversations           # 列出活会话和轮数
DELETE /v1/conversations/<key>     # 关掉一个
```

### 3. 启动依赖 → 现在自愈
`/healthz` 报告完整状态，客户端探测就知道能不能用：

```json
{
  "ok": true,
  "transport": "acp",
  "streaming": true,
  "toolCalls": "surfaced",
  "multiTurn": "session-backed",
  "sessions": {"sessions": 2, "maxSessions": 8, "turns": {...}}
}
```

ACP 会话按需创建，死了自动重建（`SessionStore.get()` 会检查 `alive()`）。不用预热。

### 4. 图片 → 现在能读，截图分析可用

ACP 初始化时返回 `promptCapabilities: {image: true}`，所以图片是支持的。之前不行是因为适配层只取了 `content` 里的 `text` 部分，`image_url` 被丢掉了。

现在按 OpenAI 标准格式发就行：

```python
client.chat.completions.create(
    model="standard",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "这张截图里写了什么？"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,..."}},
        ],
    }],
)
```

支持 `data:` URL 和 http URL，mime 类型自动识别（png / jpeg / webp / gif）。

**实测**（造了张写着 `IMG-OK-7391` 的点阵图）：

| 测试 | 结果 |
|---|---|
| 非流式读图 | `'IMC-OK-7391'` — 读到了 |
| 流式读图 | `'A blocky, pixel-font rendering of the code "IMC-OK-7391"...'` |
| 截图分析 | 正确判断「纯图形，不是 UI 截图，没有窗口边框、按钮、输入框」 |
| 纯文本回归 | `'TEXT-ONLY-OK'` |

截图分析那条还自己补了一句：低分辨率点阵字体下 `3` 和 `9` 容易糊在一起，所以读法有不确定性。这是真在看图。

**只取最新一轮的图。** 历史消息里的图会被忽略，因为 ACP 会话已经持有上下文，重发会浪费 token。

---

## 启动

```powershell
python perch_openai_adapter.py --port 8791 --max-sessions 8
```

---

## 接 DeepSeek Harness

```powershell
$env:OPENAI_BASE_URL = 'http://127.0.0.1:8791/v1'
$env:OPENAI_API_KEY  = 'unused'
& "$env:LOCALAPPDATA\Programs\DeepSeek Harness\DeepSeek Harness.exe"
```

必须从同一个 shell 启动。详见 `HARNESS-WIRING.md`。

---

## 输出格式：推理和正文必须分开

**这是之前正文里混进碎碎念的原因。**

Perch 的 ACP 有两条独立的文本流：

| ACP 通知 | 内容 | 实测 |
|---|---|---|
| `agent_thought_chunk` | 推理过程 | 6 个块：`Simple arithmetic. 17*23 = ...` |
| `agent_message_chunk` | 最终答案 | 1 个块：`17 × 23 = ... = **391**.` |

Harness 的 `openai-completions` 读取器按字段分流：

```js
// 正文
textBlock.text += choice.delta.content;
stream.push({ type: "text_delta", ... });

// 推理（llama.cpp 用 reasoning_content，其他兼容端点用 reasoning）
const reasoningFields = ["reasoning_content", "reasoning", "reasoning_text"];
...
stream.push({ type: "thinking_delta", ... });
```

适配层现在按这个映射：

| ACP | → | OpenAI 字段 | Harness 渲染 |
|---|---|---|---|
| `agent_message_chunk` | → | `delta.content` | 正文 |
| `agent_thought_chunk` | → | `delta.reasoning_content` | 思考块（单独折叠） |
| `tool_call_update` | → | `perch.toolActivity` | 不进正文 |

**实测结果：**

```
流式：
  content          : '17 × 20 = 340, plus 17 × 3 = 51.\n\n**391**'
  reasoning_content: 'Simple arithmetic. 17*23 = 391.\n\nThe user asks to think step by step...'

非流式：
  keys             : ['content', 'reasoning_content', 'role']
  content          : '100.\n\n25 × 4 = 25 × 2 × 2 = ...'
  reasoning_content: 'Simple arithmetic. 25*4 = 100...'
  perch.thoughtChunks: 17
```

**不再往正文里塞任何东西。** 之前那个 `---\nTools Perch ran:` 块已经删了——它污染答案，还会随对话历史回传。工具信息只在 `perch.toolActivity` 里。

---

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | `stream` 支持 true/false |
| GET | `/v1/models` | 4 个 tier |
| GET | `/v1/conversations` | 活会话状态 |
| DELETE | `/v1/conversations/<key>` | 关掉一个会话 |
| GET | `/healthz` | 完整能力声明 |

---

## 验证记录

| 测试 | 结果 |
|---|---|
| 工具调用（流式） | 不发 `tool_calls`，轨迹追加到正文 |
| 工具调用（非流式） | `perch.toolActivity` 带 kind/status/command |
| 多轮记忆 | 第 2 轮答出 `KESTREL-7`，`sessionTurns=2` |
| 会话状态 | 2/8 活会话，轮数正确 |
| 纯流式回归 | 7.68s，`'Saffron.\nCobalt.'` |
| 非流式 | 正常 |
| 图片（非流式） | 读出 `IMG-OK-7391` |
| 图片（流式） | 正确描述图像内容 |
| 截图分析 | 判断「纯图形，非 UI 截图」 |
| 纯文本回归 | `'TEXT-ONLY-OK'` |

---

## 仍然的限制（这些是真限制，不是没做）

**首 token 3–6 秒。** ACP 会话启动 + 模型首响应。流式改善的是「看到第一个字的时间」，不是总时长。

**chunk 粒度粗。** 一次 3–6 字符到十几行。Perch 的分块策略，改不了。

**工具是 Perch 执行的，不是客户端。** 客户端能看见、能拿到结果，但不能通过 `tools` 参数注入自己的工具让 Perch 调用。Perch 用它自己的工具集。

**不能发 `tool_calls`。** 这是踩过的坑：OpenAI 的 `tool_calls` 意思是「客户端你去执行」，而 Perch 已经自己执行完了。发了会让客户端去找一个叫 `execute` 的工具，报 `unknown tool` 然后卡死。所以工具轨迹走旁路（`perch.toolActivity` + 正文末尾），不走 `tool_calls`。

**usage 是估算。** 字符数 /4。

---

## 文件

| 文件 | 作用 |
|---|---|
| `perch_openai_adapter.py` | HTTP 层，OpenAI 格式 ↔ ACP |
| `perch_acp.py` | ACP 客户端 + `SessionStore`（会话保持）+ `AcpPool`（无状态） |

---

## 边界

用的是**你自己的 Perch 账号**，跑在**你自己的机器上**，调的是**官方 ACP 入口**（Zed / JetBrains 用的同一协议）。不读 token、不冒充桌面客户端、不调未公开端点。

别把 8791 暴露到公网。
