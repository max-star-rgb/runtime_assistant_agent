# runTime 项目设计方案与架构文档

## 1. 项目概述

**runTime** 是一个基于 Python 3.11+ asyncio 的 Gateway/Runtime 分层架构，用于 OpenClaw 兼容的 Agent 执行。支持多 LLM 提供商（Anthropic、MiniMax、Qwen），通过 WebSocket 进行 JSON Frame 通信，面向电话通话助理场景设计。

核心特点：
- Gateway/Runtime 分离：多用户路由与 Agent 逻辑解耦
- 确定性 Skill 引擎：SKILL.md 中的 Steps YAML 可程序化执行，无需 LLM 逐步驱动
- TTS 分流：`<detail>`/`<link>`/`<pic>` 标签标记只显示不朗读的内容
- 流式响应：按标点分句，适配 TTS 逐句朗读

---

## 2. 系统架构

```
外部客户端 (WebSocket)
        |
    Gateway (端口 8765)
    - 接受外部 WebSocket 连接
    - 多用户路由、会话管理
    - 通话路由 (call.incoming/call.hangup)
    - 旧连接驱逐 (evict)
    - 流式 chunk 节流 (10ms)
        |
  (内部 InMemoryDuplex)
        |
    Runtime (端口 8766)
    - 会话状态管理
    - Run 生命周期与取消
    - LLM 集成 (tool loop)
    - Skill 编排与执行
        |
    LLM API + Skills Engine + Browser/Search/Memory
```

---

## 3. 包结构

```
src/openclaw_gateway_runtime/
├── protocol.py          — Frame 类型定义、协议常量
├── transport.py         — InMemoryDuplex 双向通道、Endpoint
├── ws.py                — WebSocket 适配器
├── gateway/
│   ├── gateway.py       — GatewayService：帧转发、bridge、evict
│   ├── ws_server.py     — 外部 WebSocket 服务器
│   ├── runtime_manager.py — RuntimeManager：per-user 实例池、idle reaper
│   └── user_config.py   — UserConfig：语言、profile、持久化
├── runtime/
│   ├── runtime.py       — RuntimeService：run 生命周期、取消
│   ├── ws_server.py     — 内部 WebSocket 服务器
│   └── openclaw_adapter.py — Adapter 接口 + 多种实现
├── skills/
│   ├── registry.py      — SkillsRegistry：发现、过滤、工具方法
│   ├── engine.py        — SkillEngine：确定性步骤执行
│   ├── skill_md.py      — SKILL.md 解析（YAML frontmatter + body）
│   └── executor.py      — SkillsExecutor：脚本运行器
├── agent_runtime/
│   ├── anthropic_runtime.py — AnthropicAgentRuntime：tool loop + 流解析
│   ├── anthropic_client.py  — LLM 客户端（多 provider）
│   ├── anthropic_sse.py     — SSE 流重建
│   ├── browser_client.py    — Playwright 浏览器自动化
│   ├── ddg_search_client.py — DuckDuckGo 搜索
│   ├── memory_client.py     — 长期记忆（REST/本地 JSON）
│   └── prompts/
│       ├── system_zh.md     — 中文 System Prompt
│       └── system_en.md     — 英文 System Prompt
└── infra/
    ├── structured_log.py    — JSON 结构化日志
    ├── metrics.py           — 运行时指标
    └── dotenv.py            — .env 加载
```

---

## 4. 协议设计

### Frame 结构

```json
{
  "v": 1,
  "type": "message.user",
  "session_id": "uuid",
  "turn_id": "uuid",
  "run_id": "uuid",
  "user_id": "phone_number",
  "payload": {},
  "reason": "completed",
  "error": {"code": "...", "message": "..."}
}
```

### 核心 Frame 类型

| 方向 | 类型 | 说明 |
|------|------|------|
| Client→Gateway | `call.incoming` | 来电，触发 Runtime 创建/复用 |
| Gateway→Client | `call.ready` | 会话就绪，返回 session_id |
| Client→Gateway | `message.user` | 用户消息 |
| Runtime→Client | `run.started` | 新 Run 开始 |
| Runtime→Client | `stream.chunk` | 流式文本 (text/detail/link/pic) |
| Runtime→Client | `event.tool` | 工具调用事件 |
| Runtime→Client | `run.end` | Run 结束 (reason + expects_reply) |
| Client→Gateway | `run.cancel` | 显式取消当前 Run |
| Client→Gateway | `call.hangup` | 挂断通话 |

### stream.chunk payload

```json
{
  "text": "最便宜京东3699元。",
  "display_only": false,
  "content_type": "text"
}
```

| content_type | display_only | 说明 |
|---|---|---|
| `"text"` | `false` | 口语文字，送 TTS 朗读 |
| `"detail"` | `true` | 详细数据（商品列表），只显示 |
| `"link"` | `true` | 链接，只显示 |
| `"pic"` | `true` | 图片 URL，只显示 |

---

## 5. 核心组件设计

### 5.1 GatewayService

- **bridge()**: 为每个客户端建立双向帧转发
- **evict 机制**: 同一 user_id 新连接进来时驱逐旧 bridge，防止消息被旧连接消费
- **_evict frame**: 注入唤醒帧到共享 queue，让旧 bridge 退出
- **节流**: stream.chunk 发送后 sleep 10ms
- **过滤**: 不转发 `_evict`、`run_not_found`、内部 event.tool（可配置）

### 5.2 RuntimeManager

- **per-user 实例池**: 每个 user_id 对应一个 RuntimeService 实例
- **idle reaper**: 后台任务，每 30s 扫描，超时（600s）销毁
- **hangup grace**: 挂断后保留实例 300s，用户回拨可复用
- **config 更新**: 新连接的 language/tone 等配置更新到已有实例

### 5.3 RuntimeService

- **Run 生命周期**: 接收 message.user → 自动中断旧 run → 启动新 run
- **CancelToken**: 基于 asyncio.Event 的取消令牌
- **Adapter 选择**: LLM_PROVIDER → AnthropicAgentRuntime / StubEchoAdapter

### 5.4 AnthropicAgentRuntime (Tool Loop)

```
for _round in range(MAX_TOOL_ROUNDS):
    1. 构建 payload (model, system, messages, tools)
    2. 流式调用 LLM API
    3. DetailTagParser 解析标签
    4. SentenceBuffer 按标点分句
    5. 解析 tool_uses
    6. 拦截 end_turn → 提取 expects_reply
    7. force_confirm_wait 保护（问句+工具→丢弃工具等确认）
    8. 并行执行工具
    9. 处理 run_skill（引擎执行）
    10. 结果注入 messages，继续下一轮
```

**expects_reply 推断优先级**:
1. LLM 显式调 `end_turn(expects_reply=X)` → 使用 LLM 值
2. `force_confirm_wait` 触发 → 强制 true
3. skill 活跃中（`active_skill` 不为空）→ 强制 true
4. LLM 没调 end_turn + 文字以 `？`/`?` 结尾 → true
5. 默认 → false

### 5.5 SkillEngine (确定性执行)

- **解析**: 从 SKILL.md 的 `## Steps` YAML 块提取步骤定义
- **变量替换**: `{param}` 从用户参数，`${step_id.path}` 引用前序步骤输出
- **步骤执行**: exec（shell 命令）或 browser（Playwright 动作）
- **分组**: `group` 字段区分不同动作（search / add_to_cart）
- **默认分组**: action=null 时执行第一个 group（防止误触发加购）
- **重试**: exec 步骤内置 1 次重试（针对 API 瞬时错误）
- **keepalive**: 步骤执行超 20s 自动发"在查了~"等提示
- **detail 直发**: 输出含 `<link>`/`<pic>` 时直接发 `<detail>` 给前端（不经 LLM）
- **错误处理**: `on_error` 指定错误处理步骤 + `max_retries`

### 5.6 DetailTagParser (TTS 标签解析)

- 状态机解析 `<detail>`、`<link>`、`<pic>` 标签
- 跨 chunk 边界处理（内部 buffer）
- `<detail>`: 剥离标签，透传内容
- `<link>`, `<pic>`: 保留标签原样透传（strip_tags=False）
- 过滤幻觉 token: `<end_turn .../>`, `end_turn(...)`, `<|end|>` 等

### 5.7 SentenceBuffer (TTS 分句)

- 按标点分割: `，。！？,!?\n`
- URL 感知: 不在 URL 中间的 `.` 分割
- link/pic/detail 类型: 完整缓冲不分割
- flush: 类型切换或流结束时输出剩余内容

---

## 6. Skills 系统

### Skill 目录结构

```
skills/taobao/
├── SKILL.md          — 技能描述 + Steps YAML
└── scripts/
    └── main.py       — 搜索/详情脚本
```

### SKILL.md 格式

```yaml
---
name: taobao
description: 商品价格全网对比 + 加入购物车
metadata:
  openclaw:
    requires: { bins: ["uv"] }
---
# 买手技能
## Steps
```yaml
- id: search
  group: search
  action: exec
  command: "python -m uv run scripts/main.py search ..."
  params: [keyword, source]
  defaults: {source: "0"}
  timeout: 45
```

### 执行流程

```
用户意图 → LLM 确认 → 用户确认 → LLM 调 run_skill
    → SkillEngine.execute()
        → 按 group 过滤步骤
        → 逐步执行 (exec/browser)
        → yield event.tool (进度)
        → yield stream.chunk (keepalive)
        → _collect_output()
        → 提取 <pic>/<link> 直发前端
        → 返回精简数据给 LLM 格式化
    → LLM 口语总结
    → expects_reply 设定
```

---

## 7. LLM 集成

### Provider 选择

| LLM_PROVIDER | API 地址 | 模型示例 |
|---|---|---|
| anthropic | api.anthropic.com | claude-3-5-sonnet |
| minimax | api.minimaxi.com/anthropic | MiniMax-M2.7 |
| qwen | dashscope.aliyuncs.com/apps/anthropic | qwen-plus, qwen-max |

### 推荐模型

| 模型 | 速度 | 遵从度 | 说明 |
|---|---|---|---|
| qwen-max | 0.6s | 100% | 最佳综合 |
| qwen-plus | 0.7s | 100% | 稳定可靠 |
| MiniMax-M2.7 | 1.2s | 100% | 备选 |

### System Prompt 结构

- 角色定义（通话助理风格）
- 打招呼规则
- 系统工具列表
- Skill 优先/锁定规则
- TTS 回复风格（`<detail>`/`<link>` 标签使用）
- end_turn 规则（expects_reply 语义）
- 执行规则（确认意图→执行、禁止凭知识判断）
- 动态语言选择（user_config.language）

---

## 8. 配置管理

### .env 关键变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| LLM_PROVIDER | (必填) | anthropic/minimax/qwen |
| QWEN_MODEL | qwen-plus | 模型名 |
| GATEWAY_PORT | 8765 | Gateway 监听端口 |
| MAX_RUNTIME_INSTANCES | 20 | 最大并发实例数 |
| RUNTIME_IDLE_TIMEOUT_S | 300 | 空闲销毁超时 |
| BROWSER_HEADLESS | true | 浏览器无头模式 |
| PROMPT_LANG | zh | 默认 prompt 语言 |
| WEB_SEARCH_TIMEOUT_S | 10 | 搜索超时 |

### 动态语言选择

优先级: call.incoming payload `language` > .env `PROMPT_LANG` > 默认 "zh"

归一化: "zh-CN" → "zh", "en-US" → "en"

---

## 9. 部署

### Docker (单容器)

```yaml
# deploy/docker-compose.yml
name: openclaw
services:
  openclaw-runtime:
    build: { context: .., dockerfile: deploy/Dockerfile }
    ports: ["${GATEWAY_PORT:-8765}:8765"]
    env_file: [../.env]
    volumes:
      - ../logs:/app/logs
      - ../.env:/app/.env:ro
      - ../cookies:/app/cookies:ro
      - openclaw-memory:/app/data/memory
    restart: unless-stopped
```

### 进程管理 (supervisord)

- `runtime`: python -m openclaw_gateway_runtime.runtime.ws_server
- `gateway`: python -m openclaw_gateway_runtime.gateway.ws_server
- 崩溃自动重启 (autorestart=true, startretries=999)

### 启动命令

```bash
cd deploy && docker compose up --build -d
```

---

## 10. 测试

### 约定

- 基类: `unittest.IsolatedAsyncioTestCase`
- 禁用日志: `os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")`
- 传输: `InMemoryDuplex.create_pair()` 模拟 Gateway↔Runtime
- Adapter: 注入 `StubEchoAdapter()` 避免 LLM 依赖

### 运行

```bash
# 全部测试
python -m unittest discover -s tests -p "test_*.py" -v

# 单个测试
python -m unittest tests.test_skill_engine -v

# 集成测试（需要 LLM 配置）
python scripts/cli_agent_client.py ws://127.0.0.1:8765
```

---

## 11. 消息流示例：购物比价

```
1. Client → Gateway: call.incoming {user_id: "138xxx", language: "zh"}
2. Gateway: RuntimeManager.get_or_create() → 创建/复用实例
3. Gateway → Client: call.ready {session_id: "s1"}
4. Client → Gateway: message.user {text: "帮我搜华为Mate70 Pro"}
5. Runtime: 启动 Run, 调 Adapter.run_turn()
6. Adapter → LLM: round 0 (msg_count=1)
7. LLM: "帮你搜华为Mate70 Pro？" (确认意图)
8. Adapter: expects_reply=true (问号推断)
9. → Client: stream.chunk "帮你搜华为Mate70 Pro？" + run.end
10. Client → Gateway: message.user {text: "好"}
11. Adapter → LLM: round 0 (msg_count=3)
12. LLM: 调 read(SKILL.md) → 检测到 Steps → engine_hint
13. LLM: 调 run_skill(skill_id="taobao", action="search", params={keyword: "华为Mate70 Pro"})
14. Engine: 执行 search 步骤 → API 调用 → 返回商品列表
15. Engine: 提取 <pic>/<link> → 直发 detail chunk 给前端
16. Engine: 去掉 <pic>/<link> 后的精简数据 → 返回给 LLM
17. Adapter → LLM: round 2 (格式化结果)
18. LLM: "最便宜京东4199元，12+256G。还有拼多多4099。你看要哪个？"
19. → Client: stream.chunk (口语总结) + run.end (expects_reply=true)
```
