# runTime

本仓库当前包含一套“OpenClaw Runtime + Skills 执行效果尽量不变”的 **Gateway / Runtime 分离部署**实现骨架与 OpenSpec 变更文档。

## 目录

- `openspec/changes/openclaw-gateway-runtime/`: 变更提案、设计与任务清单
- `src/`: Gateway/Runtime 的最小实现骨架（纯标准库，可运行单元测试）
- `tests/`: 取消与打断语义的单元测试
- `skills/`: 本项目默认 skills 根目录（OpenClaw 风格：每个 skill 一个文件夹 + `SKILL.md`）

## 运行与测试

实现分两层：

- **语义层（已实现）**：`GatewayService` / `RuntimeService` + `OpenClawAdapter` seam，保证 **多轮、run 级取消、同 session 打断** 语义稳定。
- **传输层（方向2）**：使用 `websockets` 提供真实的 WebSocket Gateway/Runtime 两个进程服务。

### Windows 安装 Python（建议）

1. 安装 Python 3.11+（来自 python.org 安装包），勾选 **Add python.exe to PATH**。
2. 重新打开终端，确认：

```bash
python --version
pip --version
```

### 安装依赖

```bash
pip install -r requirements.txt
```

### 安装本项目（让 `-m openclaw_gateway_runtime...` 可用）

```bash
pip install -e .
```

### 启动（两个终端）

- 启动 Runtime（内网 WS，默认 `127.0.0.1:8766`）：

```bash
python -m openclaw_gateway_runtime.runtime.ws_server
```

- 启动 Gateway（对外 WS，默认 `0.0.0.0:8765`）：

```bash
python -m openclaw_gateway_runtime.gateway.ws_server
```

### 使用 Docker Compose（可选）

如果你机器上有 Docker Desktop，可在仓库根目录运行：

```bash
docker compose -f deploy/docker-compose.yml up --build
```

### 手工验证（协议层）

外部客户端向 Gateway 发送（JSON 文本帧）：

```json
{"v":1,"type":"message.user","session_id":"s1","payload":{"text":"hello"}}
```

期望收到流式事件（示例，stub 执行引擎会返回 `echo:<text>` 的逐字符 chunk），并可发送：

```json
{"v":1,"type":"run.cancel","session_id":"s1","run_id":"<run_id>"}
```

执行测试（需要可用的 Python 解释器）：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

## 日志与指标（D2）

Gateway / Runtime 进程在 **stderr** 输出 **单行 JSON**（便于接入日志采集与检索）。日志级别由环境变量 `LOG_LEVEL` 控制（默认 `INFO`）。

常见字段：

- `component`：`gateway` 或 `runtime`
- `event`：如 `ws_accept`、`connection_open`、`connection_close`、`run_started`、`run_finished`、`run_cancel`、`run_interrupt`
- Gateway 指标：`connections_active`、`connections_total`
- Runtime 指标：`runs_active`（当前进行中 run 数）、`runs_started_total`、`cancelled_total`（显式 `run.cancel` + 同 session 打断触发的取消计数）

说明：进程内指标实现为 **单线程 asyncio 计数器**（不使用 `threading.Lock`），避免在 Windows 上与 asyncio 混用时出现事件循环卡死。

## E4 验收（Skills 触发执行闭环）

前置：

- 已配置模型（Anthropic 或 MiniMax 中国区）
- 已启动两个进程：Runtime（8766）与 Gateway（8765）

运行一键验收脚本（会要求模型读取 `skills/example/SKILL.md` 并 `exec` 运行 `scripts/hello.py`，最终回复中应包含 `hello ...`）：

```bash
python scripts/e4_skill_demo.py --url ws://127.0.0.1:8765
```

可选：验证取消语义（预期 `reason=cancelled`）：

```bash
python scripts/e4_skill_demo.py --url ws://127.0.0.1:8765 --cancel-after-ms 200
```

### 命令行交互客户端（对话与 skills 手测）

在 **Runtime + Gateway 已启动** 的前提下，可在仓库根目录运行交互式客户端（流式打印模型输出，`[tool]` 行展示工具/skill 事件）：

```bash
python scripts/cli_agent_client.py
python scripts/cli_agent_client.py --url ws://127.0.0.1:8765 --session-id mysession
```

环境变量（可选）：`GATEWAY_WS_URL`、`CLI_SESSION_ID`。内置命令：`/help`、`/cancel`、`/session <id>`、`/new`、`/quit`。

### Skills 路径配置

默认会从本项目的 `skills/` 发现 skills。你也可以用环境变量追加其它目录（用分号分隔，Windows）：

```bash
set SKILLS_PATHS=D:\some\other\skills;D:\more\skills
```

### Skills prompt 限额（A4 / OpenClaw 风格）

为避免 `<available_skills>` 过大导致提示词爆炸，支持两个环境变量（字符数预算为近似值）：

```bash
set SKILLS_MAX_PROMPT_CHARS=30000
set SKILLS_MAX_IN_PROMPT=150
```

当 full 格式超预算时会自动降级为 compact（省略 description），仍超预算则会截断并输出类似 OpenClaw 的告警行（included X of Y）。

### 写在文件里（.env）

你可以在项目根目录创建 `.env` 来保存环境变量（例如 `ANTHROPIC_API_KEY`）。Gateway/Runtime 启动时会自动读取 `.env`（如果存在），且**不会覆盖**已在系统里设置的同名环境变量。

- 复制 `.env.example` 为 `.env`，再填入真实值
- `.env` 已加入 `.gitignore`，避免误提交

## 接入真实 OpenClaw（A1/A2/C4/E4 前置）

如果你希望 Runtime 走 **OpenClaw 原生 agent+skills 执行路径**（而不是 stub），需要：

- 安装 **Node.js 22.12+**（OpenClaw `openclaw.mjs` 的最低要求）
- 准备一个 **已构建 dist/** 的 OpenClaw 源码树（否则 `openclaw.mjs` 会报 “missing dist/entry.(m)js”）

然后设置环境变量指向你的 OpenClaw 源码目录（需包含 `openclaw.mjs`）：

```bash
set OPENCLAW_REPO_PATH=D:\git\openclaw2\openclaw
```

启动 Runtime 时会自动优先使用 `CliOpenClawAdapter` 调用 OpenClaw CLI。

## 接入 Anthropic tool_use（C5）

设置环境变量后，Runtime 会自动切换到 `AnthropicSkillsAdapter`：

```bash
set ANTHROPIC_API_KEY=你的key
set ANTHROPIC_MODEL=claude-3-5-sonnet-latest
```

tools 会从本项目 `skills/` 自动生成（默认形式：`skill.<skill_id>.run`），模型返回 `tool_use` 后会执行对应 skill 的 `scripts/` 脚本，再以 `tool_result` 回注模型继续生成。

> 说明：为对齐 OpenClaw 的技能体验，本项目已将 skills 暴露方式升级为“skills 索引 + 通用工具（read_file/exec）”，而不是每个 skill 一个专用 tool。

## 适配 MiniMax（Anthropic 兼容接口）

MiniMax 提供 Anthropic 兼容的 `v1/messages`，所以本项目无需引入新协议：**只使用 MiniMax** 时，**不要设置** `ANTHROPIC_API_KEY`（或保持为空），只配置 `MINIMAX_*`。Runtime 会选用 `AnthropicSkillsAdapter`，请求发往 `MINIMAX_BASE_URL`。

```bash
set MINIMAX_API_KEY=你的key
set MINIMAX_MODEL=MiniMax-M2.7
set MINIMAX_REGION=CN
set MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic
set MINIMAX_MAX_TOKENS=1024
set MINIMAX_TIMEOUT_S=120
```

说明：

- 中国区常用域名是 `https://api.minimaxi.com/anthropic`（`MINIMAX_REGION=CN` 时若未指定 `MINIMAX_BASE_URL` 也会默认该地址）。
- 国际站常用域名是 `https://api.minimax.io/anthropic`。
- `MINIMAX_MAX_TOKENS` / `MINIMAX_TIMEOUT_S` 仅在不走 Anthropic 时优先使用；未设置则回退到 `ANTHROPIC_MAX_TOKENS` / `ANTHROPIC_TIMEOUT_S`。
- `.env` 里若保留示例占位符 `your_minimax_key_here`，会被视为未配置，不会启用模型适配器。

## 云端多平台比价 + 落地链接（`price_compare`）

目标：**比价在云端完成**，**加购在京东 / 淘宝 / 拼多多 App 内由用户完成**（打开返回的 `landing_url`）。

1. **自建比价 HTTP 服务**（独立部署）：接收 `POST` JSON，例如 `{"query":"…","platforms":["jd","taobao","pdd"]}`，返回你定义的 JSON（建议含每条报价的 `platform`、`price`、`title`、**`landing_url`**）。数据源须合规（开放平台、联盟、采购的数据服务等）。
2. **配置 Runtime 环境变量**：`PRICE_COMPARE_SERVICE_URL`（完整 URL，仅此地址会被调用）、可选 `PRICE_COMPARE_API_KEY`（Bearer）、`PRICE_COMPARE_TIMEOUT_S`。
3. 设置 `PRICE_COMPARE_SERVICE_URL` 后，Agent 可使用工具 **`price_compare`**；未设置则**不注册**该工具。
4. **深链 / 加购**：由各平台与联盟规则决定；由你的比价服务生成可跳转官方 App 的链接，Agent 只负责展示与引导。

说明见 `skills/price-compare-links/SKILL.md`，请求实现见 `src/openclaw_gateway_runtime/agent_runtime/price_compare_client.py`。

### 无比价 API 时：Serper 批量搜索（`batch_web_search`）

若尚未部署比价服务，可配置 **`SERPER_API_KEY`**（[Serper](https://serper.dev)），Runtime 会注册工具 **`batch_web_search`**：对多条查询调用 Google 搜索 JSON API，返回每条结果的标题/链接/摘要。适合写 `site:jd.com` 等查询辅助找商品页，**不是**结构化实时比价，用户仍须在 App 内查看价格与加购。详见 `skills/web-search-batch/SKILL.md` 与 `serper_batch_client.py`。

### Anthropic 真流式（C6）

Anthropic 响应使用 SSE 解析为增量 `stream.chunk`（text_delta），体验更接近 OpenClaw 的“边生成边看”。
