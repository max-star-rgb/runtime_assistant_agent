# 任务清单：本项目实现 Agent Runtime + Skills（对齐 OpenClaw / Agent Skills 标准）

> 执行实现时按顺序勾选；若设计与依赖有变，先更新 `design.md` 再改任务。

## 阶段 A：基线与 Skills 发现

- [x] **A1** 明确 skills 目录契约（每个 skill 文件夹 + 必须 `SKILL.md`；可选 `scripts/ references/ assets/`），并在本项目实现 discovery。（`SkillsRegistry` + 示例 skill）
- [x] **A2** 实现脚本执行器（受限路径、禁 symlink、超时、回传 stdout/stderr/exit code）。（`SkillsExecutor`）
- [x] **A3** 定义 skills 在 Anthropic tool_use 上的映射：skills 作为 prompt 索引 + 通用工具。（`AnthropicAgentRuntime`）
- [x] **A4** 补齐 skills prompt 限额与降级策略（full→compact→截断）。（`SKILLS_MAX_PROMPT_CHARS`/`SKILLS_MAX_IN_PROMPT`）
- [x] **A5** 对齐 frontmatter 解析（metadata、requires/os、`disable-model-invocation`）。
- [x] **A6** 对齐 Agent Skills 标准：name 须匹配父目录名；description 为空则跳过；支持 `license`/`compatibility` 字段。
- [x] **A7** SKILL.md 格式对齐 OpenClaw 通用风格：自然语言 + bash code blocks（非 JSON schema），和 OpenClaw `.pi/skills/` 互通。
- [x] **A8** `SkillFrontmatter` 增加 `enabled` 布尔字段（默认 true），`refresh()` 跳过 `enabled: false` 的 skill，允许禁用而不删除。

## 阶段 B：Gateway（对外 WebSocket）

- [x] **B1** 对外 JSON 帧规范（`run.started` / `stream.chunk` / `run.end` / `run.cancel` 等）。
- [x] **B2** WS 服务：连接、心跳、断连时 cancel 关联 run。
- [x] **B3** Gateway → Runtime 内网 WebSocket 双向流。
- [x] **B4** Gateway 默认过滤 `event.skill`（适配器内部帧），`GATEWAY_FORWARD_EVENT_SKILL=1` 可恢复。

## 阶段 C：Runtime（Agent 核心）

- [x] **C1** Runtime 服务进程：接收 Gateway 流、创建/恢复会话。
- [x] **C2** Run 级取消。
- [x] **C3** 打断（同 session 新消息 → cancel 旧 run → 启新 run）。
- [x] **C4** SkillsRegistry/Executor 接入 RuntimeService。
- [x] **C5** Anthropic tool_use 循环 → **多轮 agentic while loop**（直到 `end_turn` 或 `MAX_TOOL_ROUNDS`）。
- [x] **C6** SSE 真流式（`text_delta` + `input_json_delta`）。
- [x] **C7** 工具对齐 OpenClaw：`read`/`write`/`edit`/`exec`/`web_search`/`web_fetch`，参数名一致。
- [x] **C8** 同批 tool_use 并发执行（`asyncio.gather`）。
- [x] **C9** tool_result 截断（`HARD_MAX_TOOL_RESULT_CHARS = 400,000`）。
- [x] **C10** `web_search` 使用 `ddgs` 库（自动切换 DuckDuckGo/Bing/Brave/Google 后端，避免 bot 检测）。
- [x] **C11** 跨轮次 Skill 锁定：`SessionState.active_skill` + `tool_used_this_turn` 机制；通用工具不阻塞后续 skill 切换。
- [x] **C12** 系统提示 Skill-first rule：匹配到 skill 描述时必须优先使用 skill，禁止用通用工具替代。
- [x] **C13** `browser` 工具（Playwright Chromium）：navigate/snapshot/screenshot/click/type/select 六个核心动作。（`BrowserManager` 单例 + JS DOM snapshot + CSS selector 定位）

## 阶段 D：部署与运维

- [x] **D1** 会话粘滞（方案 A）+ 单机双进程 / compose。
- [x] **D2** 结构化日志与基础指标。
- [x] **D3** 多 LLM 提供商：`LLM_PROVIDER` 显式选择 Anthropic / MiniMax / Qwen；统一 Anthropic 兼容接口 + SSE 流式。
- [x] **D5** `exec` 命令在 Windows 下显式 `encoding="utf-8", errors="replace"`，修复中文输出乱码。
- [x] **D6** `EXTRA_PATH` 环境变量支持，将自定义目录（如 uv/node）追加到 PATH。
- [ ] **D4** 运维说明文档（端口、环境变量清单、健康检查、滚动升级）。

## 阶段 F：记忆系统

- [x] **F1** 新建 `memory_client.py`：`MemoryClient` 类 + 本地 JSON 文件存储后端（`_LocalStore`）+ 远程 REST 后端（`_RemoteStore`），通过 `MEMORY_SERVICE_URL` 自动选择。
- [x] **F2** `SessionState` 增加 `user_id`（用户标识）和 `memory_loaded`（本轮记忆加载状态）字段。
- [x] **F3** `_system_prompt()` 支持 `memories` 参数，在系统提示末尾注入 `<user_memory>` 块。
- [x] **F4** `run_turn()` turn 开始时调用 `MemoryClient.recall()` 检索相关记忆；turn 结束时调用 `_save_turn_memories()` 提取并保存记忆。
- [x] **F5** 短期记忆压缩：`_compress_messages()` 在 messages 超过 `MEMORY_MAX_MESSAGES` 阈值时自动将旧消息摘要化。
- [x] **F6** `.env.example` 添加记忆相关配置项（`MEMORY_SERVICE_URL`、`MEMORY_LOCAL_DIR`、`MEMORY_MAX_MESSAGES`、`MEMORY_RECALL_LIMIT`、`MEMORY_SERVICE_TIMEOUT_S`）。

## 阶段 E：验收

- [x] **E1** 多轮对话（连续三轮用户消息，上下文正确）。
- [x] **E2** 取消（长生成中 `run.cancel`，确认无后续 chunk 且原因为 `cancelled`）。
- [x] **E3** 打断（流式输出中途发送新消息，旧 run 停止，新 run 正常开始）。
- [x] **E4** Skills 触发执行（skill 被发现→读 SKILL.md→执行脚本→回注结果→最终回复）。
- [x] **E5** 端到端比价：taobao(maishou) skill 通过 maishou88.com API 搜索商品，返回精确价格和优惠券。
- [x] **E6** 多轮工具循环：模型在一次 turn 中连续使用 3+ 轮工具直到 end_turn。
- [x] **E7** 跨轮次 skill 锁定：Turn 1 触发 taobao skill → Turn 2 后续查询仍锁定在同一 skill。
- [x] **E8** 通用工具不阻塞 skill 切换：Turn 1 使用 web_search → Turn 2 可正常激活 taobao skill。
- [x] **E9** browser 工具端到端：navigate example.com → snapshot 获取 ref 列表 → click ref 点击链接 → snapshot 确认跳转。

## Skills 清单

| Skill | 状态 | 用途 |
|-------|------|------|
| `example` | 启用 | 冒烟测试（bundled script） |
| `workspace-nav` | 启用 | 浏览/读写/编辑文件 |
| `datetime` | 启用 | UTC 时间（bundled script） |
| `json-tools` | 启用 | JSON 格式化（bundled script） |
| `taobao` (maishou) | 启用 | 电商全网比价（maishou88.com API） |
| `taobao-cart` | 启用 | 浏览器自动加入购物车（Patchright 反检测 + cookie 注入） |
| `price-compare-links` | **禁用** (`enabled: false`) | web_search 比价替代方案（已被 taobao skill 取代） |

## 依赖清单

| 包 | 最低版本 | 用途 |
|----|----------|------|
| `websockets` | 14.0 | Gateway/Runtime WS 通信 |
| `ddgs` | 9.13.0 | web_search 工具（DuckDuckGo 搜索库） |
| `playwright` | 1.52.0 | browser 工具（需 `playwright install chromium`） |
| `patchright` | 1.58.0 | 反检测浏览器引擎（Playwright fork，可选） |
| `setuptools` | — | 包构建 |

---

*全部完成后，可归档本变更（OpenSpec archive 流程）。*
