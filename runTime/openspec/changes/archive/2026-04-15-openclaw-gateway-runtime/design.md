# 设计：本项目内 Agent Runtime + Skills（对齐 OpenClaw / Agent Skills 标准）与 Gateway 分离部署

## 1. 上下文与约束

- **Runtime 自主实现**：本项目内实现 Agent Runtime 的核心编排能力（会话、多轮、tool/skill 执行、取消/打断）。Gateway 只做 **I/O、鉴权、路由、协议映射**。
- **分离部署**：Gateway 与 Runtime 为不同进程/容器，通过 **内网 WebSocket** 通信；禁止假设同进程共享内存。
- **交互**：多轮会话；任意时刻可对 **当前 run** 取消；新用户输入可 **打断** 正在进行的生成并开启新 run。
- **Skills 架构**：采用 [Agent Skills 标准](https://agentskills.io/specification) + OpenClaw 目录约定：每个 skill 一个文件夹，必须有 `SKILL.md`（YAML frontmatter + 自然语言指令），可选 `scripts/ references/ assets/`。
- **SKILL.md 格式**：frontmatter 必须含 `name`（kebab-case，须匹配父目录名）与 `description`（决定模型何时加载）；可选 `license`、`compatibility`、`metadata`、`allowed-tools`、`disable-model-invocation`。正文为自由格式 Markdown + bash code blocks（和 OpenClaw 实际写法一致）。

## 2. 逻辑架构

```text
                    ┌──────────────────┐
  External Client   │     Gateway      │
  (WebSocket) ◀────▶│  TLS / Auth /    │
                    │  WS 帧 ↔ 内部流  │
                    └────────┬─────────┘
                             │  内网 WebSocket
                    ┌────────▼──────────────────────────────┐
                    │         Runtime (本项目)              │
                    │                                      │
                    │  ┌─────────────────────────────────┐ │
                    │  │   AnthropicAgentRuntime         │ │
                    │  │   - Session / Run 管理          │ │
                    │  │   - 多轮 agentic loop           │ │
                    │  │   - 取消/打断                    │ │
                    │  └──┬──────────────────────────┬───┘ │
                    │     │                          │     │
                    │  ┌──▼──────────┐        ┌─────▼───┐  │
                    │  │ SkillsReg   │        │ Memory  │  │
                    │  │ + Executor  │        │ Client  │  │
                    │  └─────────────┘        └────┬────┘  │
                    │                              │       │
                    └───────────────────────────────┼──────┘
                                                   │
                         ┌─────────────────────────┼─────────────┐
                         │                         │             │
                    ┌────▼────┐              ┌─────▼──────┐      │
                    │ Local   │              │  External  │      │
                    │ JSON    │              │  Memory    │      │
                    │ Files   │              │  Service   │      │
                    │(default)│              │  (REST)    │      │
                    └─────────┘              └────────────┘      │
                                                                 │
                    ┌────────────────────────────────────────────┘
                    │
              ┌─────▼──────┐
              │   LLM API  │
              │ Anthropic/ │
              │ MiniMax/   │
              │ Qwen       │
              └────────────┘
```

**核心组件**：

- **Gateway**：WebSocket 服务器，处理外部连接、鉴权、协议转换
- **AnthropicAgentRuntime**：会话管理、多轮对话、工具编排、记忆注入
- **SkillsRegistry + Executor**：技能发现、脚本执行
- **MemoryClient**：可插拔记忆后端（本地文件 / 外部 REST 服务）
- **BrowserManager**：Patchright/Playwright 浏览器自动化（单例）
- **LLM API**：多提供商统一接口（Anthropic 兼容）

## 3. 标识与生命周期


| 标识           | 职责                                              |
| ------------ | ----------------------------------------------- |
| `session_id` | 多轮对话上下文、技能与 OpenClaw 会话状态的作用域。                  |
| `turn_id`    | 单次用户输入轮次，便于日志与审计（可选但推荐）。                        |
| `run_id`     | **一次**从「开始生成」到「自然结束或被取消」的执行实例；**取消与打断均针对 run**。 |


**打断（interrupt）**：当同 `session_id` 上在新 `run` 开始前要求停止当前输出时，映射为对**当前 `run_id` 的 cancel**，再提交新用户消息对应的新 `run_id`。

**取消（cancel）**：客户端显式发送 `cancel` 携带 `run_id`（或「当前 run」约定）；Gateway 透传至 Runtime；Runtime 向模型/工具链传播取消。

## 4. 对外 WebSocket 协议

### 客户端 → Gateway

- `session.open` / `session.resume`：`session_id`、可选元数据。
- `message.user`：用户文本；生成新 `turn_id` 与 `run_id`（由 Runtime 分配后回传）。
- `run.cancel`：`run_id`。
- `ping`：保活。

### Gateway → 客户端

- `run.started`：`run_id`、`turn_id`。
- `stream.chunk`：增量内容。
- `event.tool`：工具调用/结果事件（name、input、result、phase）。
- `run.end`：`run_id`、原因（`completed` | `cancelled` | `error`）。
- `error`：错误码与消息。

Gateway 默认过滤 `event.skill`（适配器内部生命周期帧），不转发给外部客户端。可通过 `GATEWAY_FORWARD_EVENT_SKILL=1` 恢复。

## 5. Gateway ↔ Runtime 内部协议

- 内网 **WebSocket**（双向流），Runtime 的流式事件实时到达 Gateway，再转发给外部 WS。
- 每条流携带：`session_id`、用户消息。
- **取消**：`run.cancel` 帧透传。

## 6. 会话粘滞与扩缩容

**当前实现（MVP）**：方案 A — 同一 WebSocket 连接内保持 session 多轮。

## 7. Skills 发现与执行

- **发现**：扫描 `skills/` 根目录（可通过 `SKILLS_PATHS` 追加），每个子目录若含 `SKILL.md` 则视为一个 skill。
- **校验**：`name` 必须匹配父目录名（不匹配时 warn 并回退到目录名）；`description` 为空则跳过。
- **Frontmatter**：解析 `name`、`description`、`license`、`compatibility`、`metadata`（含 `openclaw.requires`/`os` 等过滤条件）、`disable-model-invocation`。
- **脚本**：`scripts/` 下文件可通过 `exec` 工具执行（路径约束、超时、禁 symlink）。
- **资源**：`references/` 按需通过 `read` 工具加载。

## 8. 工具体系（对齐 OpenClaw tool-catalog）

### 核心工具（始终注册）


| 工具               | 对应 OpenClaw          | 参数                                                             |
| ---------------- | -------------------- | -------------------------------------------------------------- |
| `**read`**       | `read`               | `file_path`、`offset`(行)、`limit`(行)                             |
| `**write**`      | `write`              | `file_path`、`content`                                          |
| `**edit**`       | `edit`               | `file_path`、`old_string`、`new_string`                          |
| `**exec**`       | `exec`               | `command`、`workdir`、`timeout`；Windows 下 `encoding="utf-8"`     |
| `**list_dir**`   | —                    | `path`、`max_entries`                                           |
| `**grep**`       | —                    | `pattern`、`path`、`max_matches`                                 |
| `**web_search**` | `web_search`（ddgs 库） | `query`、`count`、`region`、`safeSearch`                          |
| `**web_fetch**`  | `web_fetch`          | `url`、`max_bytes`、`timeout_s`                                  |
| `**browser**`    | `browser`            | `action`（navigate/snapshot/screenshot/click/type/select）+ 动作参数 |


### `browser` 工具详细设计

基于 **Patchright**（反检测 Playwright fork）+ Chromium 的浏览器自动化工具，对齐 OpenClaw browser tool 的核心交互模型。自动回退到标准 Playwright。

**架构**：`BrowserManager` 单例，使用 `launch_persistent_context()` 实现持久化会话（cookie/localStorage 自动保存到 `BROWSER_USER_DATA_DIR`），每个 `session_id` 独立 Page（tab）。

**动作**：


| 动作                        | 参数                                              | 返回                                  |
| ------------------------- | ----------------------------------------------- | ----------------------------------- |
| **navigate**              | `url`(必填), `wait_until`(可选)                     | 状态码、标题；自动检测验证码并等待用户手动处理             |
| **snapshot**              | 无必填                                             | accessibility tree 文本（带 `[ref]` 编号） |
| **screenshot**            | `full_page`(可选 bool)                            | 截图文件路径                              |
| **click**                 | `selector`(CSS) 或 `ref`(快照编号)                   | "ok"                                |
| **type**                  | `selector`/`ref`, `text`(必填), `clear_first`(可选) | "ok"                                |
| **select**                | `selector`/`ref`, `value`/`label`               | "ok"                                |
| **set_cookies**           | `cookies`(JSON数组)                               | 已设置数量                               |
| **set_cookies_from_file** | `file_path`                                     | 已设置数量                               |
| **get_cookies**           | 无                                               | 当前 cookie JSON                      |
| **wait**                  | `timeout_ms` 或 `selector`                       | "ok"                                |
| **evaluate**              | `script`(JavaScript)                            | 脚本执行结果 JSON                         |


**反检测策略**：

- 优先使用 Patchright（反检测 Playwright fork），自动回退到标准 Playwright
- 使用 `launch_persistent_context()` 而非 `browser.launch()` + `new_context()`
- `viewport: None`（真实窗口大小）、不自定义 `userAgent`
- 验证码自动检测（页面标题/正文关键词匹配）+ headed 模式下等待用户手动处理

**环境变量**：`BROWSER_HEADLESS`(true/false)、`BROWSER_TIMEOUT_S`、`BROWSER_USER_DATA_DIR`

**典型工作流**：

- 基础：navigate → snapshot → click/type/select → snapshot 验证
- 电商：set_cookies_from_file → navigate → evaluate（提取数据）→ navigate（产品页）→ click（加购）

### 可选工具（按环境变量启用）


| 工具                     | 启用条件                        | 用途                  |
| ---------------------- | --------------------------- | ------------------- |
| `**price_compare`**    | `PRICE_COMPARE_SERVICE_URL` | 自建比价 API            |
| `**batch_web_search**` | `SERPER_API_KEY`            | Serper 批量 Google 搜索 |


### 工具调用逻辑

- **Agentic while loop**：`run_turn()` 循环调用模型，直到 `stop_reason == "end_turn"` 或无 `tool_use`，最多 `MAX_TOOL_ROUNDS`（默认 25）轮。
- **并发执行**：同一批 `tool_use` 中的多个工具调用通过 `asyncio.gather` 并发执行。
- **tool_result 截断**：超过 `HARD_MAX_TOOL_RESULT_CHARS`（400,000）的结果自动截断。
- **取消**：每轮循环检查 `CancelToken`，在网络/工具调用边界中断。
- **Skill-first rule**：系统提示明确要求匹配到 skill 时必须优先使用 skill，通用工具仅用于无 skill 覆盖的场景。

### Skills 与工具的关系

- 系统提示注入 `<available_skills>` 列表（name/description/location/baseDir）。
- 模型用 `read` 加载 SKILL.md，按其中的自然语言指令调用 `exec`/`web_search` 等通用工具。
- **不是**每个 skill 一个专用 tool。
- SKILL.md 中路径相对于 skill 目录，模型须基于 baseDir 解析。

### 跨轮次 Skill 锁定

- **问题**：skill 可能需要多轮用户交互（如确认、补充信息），模型在新 turn 中可能切换到其他 skill。
- **两层机制**：
  1. **系统提示软约束**：Skill lock rule 指令。
  2. **运行时硬约束**：`SessionState.active_skill` + `tool_used_this_turn` 标志。
- **锁定时机**：模型通过 `read` 加载某 skill 的 `SKILL.md` 时激活。
- **释放条件**：上一轮 turn 中没有任何工具调用（`tool_used_this_turn == False`），表明 skill 工作流已完成。
- **通用工具不阻塞**：`web_search` 等通用工具在无 active_skill 时不设置 `tool_used_this_turn`，确保不阻塞后续 skill 切换。

## 9. Skills 发现增强

- `**enabled` 字段**：`SkillFrontmatter` 新增 `enabled: bool`（默认 `True`），frontmatter 中设置 `enabled: false` 可禁用 skill 而不删除目录。`SkillsRegistry.refresh()` 在解析后检查 `fm.enabled`。

## 10. 模型接入

通过 `LLM_PROVIDER` 环境变量显式选择模型提供商（不再按优先级逐个尝试 key）：


| 提供商       | LLM_PROVIDER 值 | 关键配置                                  |
| --------- | -------------- | ------------------------------------- |
| Anthropic | `anthropic`    | `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL` |
| MiniMax   | `minimax`      | `MINIMAX_API_KEY`、`MINIMAX_BASE_URL`  |
| Qwen      | `qwen`         | `QWEN_API_KEY`、`QWEN_BASE_URL`        |


所有提供商均使用 Anthropic 兼容 `/v1/messages` + SSE 流式接口。MiniMax/Qwen 使用 Bearer auth，Anthropic 使用 x-api-key。

## 11. 安全

- 文件操作限制在 repo root + `EXTRA_WORKSPACE_DIRS`。
- `exec` workdir 同样受限；Windows 下显式 `encoding="utf-8"` 避免乱码。
- `web_fetch` 仅 http/https。
- `edit` old_string 必须唯一。
- `write` 自动创建父目录。
- `browser` 使用独立 Chromium 实例，与用户浏览器隔离。

## 12. 可观测性

- 结构化日志（stderr 单行 JSON）：`session_id`、`turn_id`、`run_id`。
- 指标：活跃连接、进行中 run、取消计数。

## 13. 记忆系统

### 架构

```text
┌──────────────────────────────────────┐
│         AnthropicAgentRuntime        │
│                                      │
│  run_turn() start ──► MemoryClient   │
│    │  recall(user_id, query)         │
│    │       ▼                         │
│    │  _system_prompt(memories=[...]) │
│    │  ... agentic loop ...           │
│    │                                 │
│  run_turn() end ──► MemoryClient     │
│    │  save(user_id, entries)         │
│    │                                 │
│  messages 压缩 ◄── _compress_messages│
└──────────┬───────────────────────────┘
           │
   ┌───────▼──────────┐
   │   MemoryClient   │
   │   (pluggable)    │
   ├──────────────────┤
   │ MEMORY_SERVICE_  │──► External REST API
   │ URL configured?  │    POST /memory/save
   │                  │    GET  /memory/recall
   ├──────────────────┤    DELETE /memory/forget
   │ No URL → local   │
   │ JSON file store  │──► MEMORY_LOCAL_DIR/{user_id}.json
   └──────────────────┘
```

### 记忆分两层

- **短期记忆（会话级）**：`SessionState.messages` 为基础，增加 `_compress_messages()` 机制——当消息数超过 `MEMORY_MAX_MESSAGES`（默认 20）时，自动将前半部分消息摘要为一条 summary message，保持上下文窗口可控。
- **长期记忆（跨会话）**：通过 `MemoryClient` 存取。每次 turn 开始时从外部服务检索相关记忆注入 system prompt（`<user_memory>` 块）；每次 turn 结束后通过正则模式匹配从用户输入中提取关键信息（偏好、个人事实、购买事件）存入记忆服务。

### 外部记忆服务 REST 接口

```
POST /memory/save
Body: { "user_id": "...", "session_id": "...",
        "entries": [{ "type": "fact|preference|event",
                      "content": "...", "importance": 0.0-1.0 }] }

GET  /memory/recall?user_id=...&query=...&limit=10
Response: { "memories": [{ "content": "...", "type": "...",
                           "created_at": "...", "relevance": 0.8 }] }

DELETE /memory/forget?user_id=...&before=...
```

任何后端（Redis、SQLite、向量数据库、云服务）均可实现此接口。

### SessionState 扩展

```python
@dataclass
class SessionState:
    messages: list[dict[str, Any]]
    active_skill: str | None = None
    tool_used_this_turn: bool = False
    user_id: str = "default"        # 用户标识，用于跨会话记忆隔离
    memory_loaded: bool = False     # 本轮是否已加载记忆
```

### 记忆提取模式

使用正则匹配从用户文本中提取记忆条目（不依赖额外 LLM 调用）：


| 类型         | 模式示例                  |
| ---------- | --------------------- |
| preference | 我喜欢/偏好/想要/习惯/常用...    |
| fact       | 我叫/住在/来自/的名字/地址/电话... |
| event      | 我刚买/已经下单/昨天收到/上次退...  |


### 配置项


| 环境变量                       | 默认值    | 说明                |
| -------------------------- | ------ | ----------------- |
| `MEMORY_SERVICE_URL`       | （空）    | 外部记忆服务地址，不设则用本地文件 |
| `MEMORY_LOCAL_DIR`         | 系统临时目录 | 本地记忆存储路径          |
| `MEMORY_MAX_MESSAGES`      | 20     | 会话消息压缩阈值          |
| `MEMORY_RECALL_LIMIT`      | 10     | 每次检索返回的记忆条数上限     |
| `MEMORY_SERVICE_TIMEOUT_S` | 10     | 外部记忆服务超时          |


## 14. 风险与待决项


| 风险                         | 缓解                                                                         |
| -------------------------- | -------------------------------------------------------------------------- |
| Skills 执行带来系统风险            | 路径约束、超时、禁 symlink、输出截断；后续引入更强沙箱。                                           |
| DuckDuckGo 人机验证            | 使用 `ddgs` 库自动切换后端（DuckDuckGo/Bing/Brave/Google）；可配 Serper 作备选。             |
| 会话粘滞与滚动升级                  | 优雅下线 drain；或后续引入外置状态。                                                      |
| MiniMax/Qwen tool_use 兼容性  | SSE builder 已处理 `input_json_delta` 与预填 `input` 的合并。                        |
| browser 工具 Playwright 版本兼容 | snapshot 实现双策略：Playwright >=1.59 用 aria_snapshot，旧版回退 JS DOM 遍历。           |
| 第三方 skill API 不稳定          | taobao(maishou) skill 依赖 maishou88.com API，已更新至最新端点；signTimestamp 过期需跟踪上游。 |
| 外部记忆服务不可用                  | MemoryClient 所有操作均 try/except，服务宕机时静默降级（不阻塞正常对话）。本地文件后端作为零依赖 fallback。     |
| 记忆提取精度有限                   | 当前基于正则模式匹配，无法捕获所有隐含偏好；后续可引入 LLM 辅助提取。                                      |


