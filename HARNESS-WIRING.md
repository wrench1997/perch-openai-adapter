# 在 DeepSeek Harness 里填这个表单

设置 → 模型 → **自定义模型 API** 标签页。

---

## 逐字段填法

| 字段 | 填什么 |
|---|---|
| **Provider ID** | `perch` |
| **显示名称** | `Perch` |
| **API 地址** | `http://127.0.0.1:8791/v1` |
| **API 协议** | `OpenAI Chat Completions`（保持默认） |
| **API 密钥** | `unused` |
| **模型目录** | 点「获取可用模型」 |

填完点「获取可用模型」，会出现四个：

```
standard       Standard        ctx=200000
standard 1m    Standard 1M     ctx=1000000
pro            Pro             ctx=200000
pro 1m         Pro 1M          ctx=1000000
```

### 为什么只有四个，不能更多

这是 Perch 模型层的真实结构，不是适配层限制。我查了三条路，都堵死：

| 尝试 | 结果 |
|---|---|
| `perch acp --model pro` | `Unknown domain: acp --model` |
| `perch --model pro acp` | `Unknown interactive flag: --model` |
| ACP `session/set_config` | `Method not found` |
| `session/new` 返回 | 只有 `modes`，没有模型列表 |

Perch 的模型层就是四个 tier（`standard` / `standard_max` / `pro` / `pro_max`），ACP 协议只暴露权限模式，不暴露模型选择。所以**四个就是全部**。

### 但 tier 参数现在能正确识别

| 你填的 | 实际用的 |
|---|---|
| `pro` | `pro` |
| `pro 1m` | `pro 1m` |
| `standard_max` | `standard 1m`（旧名兼容） |
| `pro@max` | `pro`（忽略 effort 后缀） |
| 乱填 | 回退 `standard` |

实测全部返回 `TIER-OK`，没有报错。

---

## 前提：适配层得先跑起来

```powershell
python perch_openai_adapter.py --port 8791 --mode agents
```

**`--mode agents` 很重要。** 不加的话默认是 `ask`（只读），Harness 让它写文件、跑命令都会被拒。Harness 是 agent 工具，要的就是能动手。

长时运行用后台方式（见 `.perch/rules/background-start.md`）。

---

## 实测：按这个表单的调用顺序验证过

模拟 Harness 的三步：

**1. 点「获取可用模型」** → `GET /v1/models`

```
4 models: ['standard', 'standard 1m', 'pro', 'pro 1m']
```

**2. 发一个带工具定义、要求写文件的请求** → `POST /v1/chat/completions`（stream=true）

```
HTTP 200  text/event-stream
elapsed: 13.17s
text : 'Done. `C:\Users\Texie\Desktop\_harness_probe.txt` created, 6 bytes, reads back as `PROBE`.'
tools: 2
  name=edit  args={"path": "..._harness_probe.txt", "content": "PROBE\n"}
  name=read  args={"path": "..._harness_probe.txt"}
```

**3. 文件真的存在吗？**

```
EXISTS  content='PROBE\n'
```

不是嘴上说做了，是真写了。

---

## 工具调用现在是什么状态

Perch 自己执行工具，ACP 把过程发出来。**适配层不把它包成 OpenAI 的 `tool_calls`**，因为那个字段的意思是「客户端你去执行」——Perch 已经执行完了，再发会让 Harness 去找一个叫 `execute` 的工具，报 `unknown tool` 然后卡死。

工具轨迹走旁路：

- 非流式：`perch.toolActivity` 里有 kind / status / command / resultPreview
- 流式：执行轨迹追加到正文末尾，像这样：

```
---
Tools Perch ran:
- execute (completed): Write-Output "STREAM-FIXED"
```

所以你在 Harness 界面里能看到它跑了什么命令、成没成功。

**但有一点要说清楚：** 你在这个表单里配的 `tools` 定义不会让 Perch 去调你的工具。Perch 用它自己那套工具集（读写文件、跑 shell、浏览器等）。

对 Harness 这种场景这其实够用——它要的是「让模型能动手」，而不是「让模型调 Harness 注册的特定函数」。

---

## 截图分析

**能。** 直接贴图就行，Harness 会用 OpenAI 标准的 `image_url` 格式发过来，适配层转成 ACP 的 image block。

实测：

| 测试 | 结果 |
|---|---|
| 读图上的文字 | 正确读出 `IMG-OK-7391` |
| 描述图像 | `'A blocky, pixel-font rendering of the code "IMC-OK-7391"...'` |
| 判断是不是 UI 截图 | 正确回答「纯图形，没有窗口边框、按钮、输入框」 |

支持 `data:` URL 和 http URL，png / jpeg / webp / gif 自动识别。

只取最新一轮的图——历史消息里的图会被忽略，因为会话已经持有上下文。

---

## 会话记忆

Harness 如果传了 `user` 字段或 `X-Conversation-Id` 头，适配层会把它映射到一个长活的 ACP 会话，Perch 自己记住上下文。

实测：

```
turn 1: 'Acknowledged.'      sessionTurns=1
turn 2: 'KESTREL-7'          sessionTurns=2
```

如果 Harness 不传这些，每个请求独立会话，靠 `messages` 数组传历史——也能用，只是每次都要重传。

---

## 排错

**「获取可用模型」转圈或报错**
→ 适配层没跑。先起 8791。

**模型列表是空的**
→ 检查 API 地址结尾有没有 `/v1`。应该是 `http://127.0.0.1:8791/v1`，不是 `http://127.0.0.1:8791`。

**能对话但让它改文件时拒绝**
→ 适配层启动时没加 `--mode agents`。重启加上。

**首 token 等很久**
→ 正常，3–6 秒。ACP 会话启动 + 模型首响应。

**连接被拒**
→ 检查端口：`Get-NetTCPConnection -State Listen | Where-Object {$_.LocalPort -eq 8791}`

---

## 回滚

设置里删掉这个 provider 就行，不影响其他配置。

之前用环境变量方式配的（`OPENAI_BASE_URL`）如果还留着，记得清掉，否则可能冲突：

```powershell
Remove-Item Env:OPENAI_BASE_URL -ErrorAction SilentlyContinue
Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
```
