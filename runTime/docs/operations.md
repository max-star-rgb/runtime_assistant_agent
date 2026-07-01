# 运维说明文档

> 本文档覆盖 openclaw-gateway-runtime 的端口规划、环境变量清单、健康检查与滚动升级。

## 1. 端口规划

| 服务 | 默认绑定地址 | 默认端口 | 说明 |
|------|------------|---------|------|
| Runtime | `127.0.0.1` | `8766` | 仅内网，Gateway 连接 Runtime 专用 |
| Gateway | `0.0.0.0` | `8765` | 对外暴露，WebSocket 客户端连接此端口 |

> 端口当前以函数默认参数形式硬编码，如需修改请直接编辑 `gateway/ws_server.py` 与 `runtime/ws_server.py` 中 `serve_*_ws()` 的参数默认值，或在 compose 中通过命令覆盖。

### 防火墙建议

- **Gateway 8765**：对外开放，按需限制来源 IP（反向代理/VPN/白名单均可）。
- **Runtime 8766**：仅限同机或同 Docker 网络访问，不应暴露到公网。

---

## 2. 环境变量清单

复制 `.env.example` 为 `.env` 并填入真实值。Gateway / Runtime 启动时自动读取，**不覆盖**已有系统变量。

### 2.1 必填：模型提供商

| 变量 | 示例值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | `minimax` | 选择提供商：`anthropic` / `minimax` / `qwen` |

#### Anthropic

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ANTHROPIC_API_KEY` | — | API 密钥（必填） |
| `ANTHROPIC_MODEL` | `claude-3-5-sonnet-latest` | 模型名 |
| `ANTHROPIC_BASE_URL` | — | 自定义 API 地址（可选） |
| `ANTHROPIC_MAX_TOKENS` | `1024` | 最大输出 token |
| `ANTHROPIC_TIMEOUT_S` | `120` | 请求超时秒数 |

#### MiniMax

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MINIMAX_API_KEY` | — | API 密钥（必填） |
| `MINIMAX_MODEL` | `MiniMax-M2.7` | 模型名 |
| `MINIMAX_REGION` | — | `CN` 表示中国区 |
| `MINIMAX_BASE_URL` | `https://api.minimaxi.com/anthropic` | API 地址（CN 区默认值） |
| `MINIMAX_MAX_TOKENS` | `1024` | 最大输出 token |
| `MINIMAX_TIMEOUT_S` | `120` | 请求超时秒数 |

#### Qwen

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QWEN_API_KEY` | — | API 密钥（必填） |
| `QWEN_MODEL` | `qwen3-coder-plus` | 模型名 |
| `QWEN_REGION` | — | 区域标识 |
| `QWEN_BASE_URL` | `https://dashscope.aliyuncs.com/apps/anthropic` | API 地址 |
| `QWEN_MAX_TOKENS` | `8192` | 最大输出 token |
| `QWEN_TIMEOUT_S` | `120` | 请求超时秒数 |

### 2.2 Skills 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SKILLS_PATHS` | — | 额外 skills 目录（分号分隔，Windows） |
| `SKILLS_MAX_PROMPT_CHARS` | `30000` | 提示词 skills 块字符预算 |
| `SKILLS_MAX_IN_PROMPT` | `150` | 提示词最多包含 skill 数量 |

### 2.3 浏览器工具

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `BROWSER_HEADLESS` | `true` | 是否无头模式 |
| `BROWSER_TIMEOUT_S` | `30` | 页面操作超时秒数 |
| `BROWSER_USER_DATA_DIR` | — | 持久化浏览器数据目录（保留 Cookie/登录态） |
| `TAOBAO_COOKIE_FILE` | — | 淘宝 Cookie JSON 文件路径 |

### 2.4 记忆系统

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MEMORY_SERVICE_URL` | — | 外部记忆 REST 服务地址；未设置则使用本地 JSON |
| `MEMORY_LOCAL_DIR` | — | 本地记忆存储目录（未设置则存入系统临时目录） |
| `MEMORY_MAX_MESSAGES` | `20` | 会话消息压缩阈值（超出后自动摘要旧消息） |
| `MEMORY_RECALL_LIMIT` | `10` | 每次检索返回记忆条数上限 |
| `MEMORY_SERVICE_TIMEOUT_S` | `10` | 外部记忆服务请求超时秒数 |

### 2.5 搜索与比价工具

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PRICE_COMPARE_SERVICE_URL` | — | 比价服务 POST 地址；未设置则不注册该工具 |
| `PRICE_COMPARE_API_KEY` | — | Bearer token（可选） |
| `PRICE_COMPARE_TIMEOUT_S` | `45` | 比价请求超时秒数 |
| `SERPER_API_KEY` | — | Serper Google 搜索 API Key；设置后注册 `batch_web_search` 工具 |
| `SERPER_MAX_QUERIES` | `5` | 每次批量搜索最多查询数 |
| `SERPER_TIMEOUT_S` | `20` | Serper 请求超时秒数 |
| `WEB_SEARCH_TIMEOUT_S` | `10` | DuckDuckGo 搜索超时秒数 |
| `WEB_SEARCH_CACHE_TTL` | `300` | DuckDuckGo 结果缓存秒数 |

### 2.6 运维与调试

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_LEVEL` | `INFO` | 日志级别：`DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `OPENCLAW_STRUCTURED_LOG` | `1` | 设为 `0` 禁用结构化 JSON 日志，改为普通文本 |
| `MAX_TOOL_ROUNDS` | `25` | 单次 turn 工具循环上限，防止死循环 |
| `EXTRA_WORKSPACE_DIRS` | — | 追加允许 read/write/exec 访问的目录（管道 `\|` 分隔） |
| `EXTRA_PATH` | — | 追加到进程 PATH（如 uv、node 等工具路径） |
| `GATEWAY_FORWARD_EVENT_SKILL` | `0` | 设为 `1` 将 `event.skill` 帧转发给 WebSocket 客户端 |
| `OPENCLAW_REPO_PATH` | — | OpenClaw 源码目录（启用 CLI 透传适配器） |
| `OPENCLAW_ARGS` | — | OpenClaw CLI 额外参数 |
| `PYTHONUNBUFFERED` | `1` | 建议设为 `1`，保证日志实时输出 |

---

## 3. 健康检查

两个服务均为纯 WebSocket 进程，**没有**独立的 HTTP 健康检查端点。

### 3.1 WebSocket Ping/Pong

向 Gateway 发送 JSON 帧，收到 `pong` 即表示 Gateway 与 Runtime 链路均正常：

```json
{"v":1,"type":"ping"}
```

预期响应：

```json
{"v":1,"type":"pong"}
```

### 3.2 进程存活检查（本机部署）

```bash
# 检查 Runtime 端口是否监听
netstat -ano | findstr :8766

# 检查 Gateway 端口是否监听
netstat -ano | findstr :8765
```

### 3.3 Docker Compose 健康检查（推荐添加）

在 `deploy/docker-compose.yml` 的 `runtime` 与 `gateway` 服务中加入 healthcheck 配置。

### 3.4 日志指标监控

两个进程在 **stderr** 输出结构化 JSON 日志，可通过日志采集工具（如 Loki、ELK）检索以下字段：

| 字段 | Gateway | Runtime |
|------|---------|---------|
| `connections_active` | 当前活跃连接数 | — |
| `connections_total` | 累计连接数 | — |
| `runs_active` | — | 当前进行中 run 数 |
| `runs_started_total` | — | 累计启动 run 数 |
| `cancelled_total` | — | 取消（显式+打断）总次数 |

示例日志行：

```json
{"ts":"2026-04-15T10:00:00Z","level":"INFO","component":"runtime","event":"run_started","run_id":"r-abc","session_id":"s1","runs_active":1}
```

---

## 4. 滚动升级

### 4.1 单机双进程部署

**步骤**：

1. **停止 Gateway**（客户端连接会断开）：

```bash
# 找到 Gateway 进程 PID
netstat -ano | findstr :8765

# 终止进程
taskkill /PID <pid> /F
```

2. **更新代码**（git pull / 复制新版本）

3. **重启 Runtime**：

```bash
# 终止旧 Runtime
netstat -ano | findstr :8766
taskkill /PID <pid> /F

# 启动新 Runtime
python -m openclaw_gateway_runtime.runtime.ws_server
```

4. **重启 Gateway**：

```bash
python -m openclaw_gateway_runtime.gateway.ws_server
```

**影响**：Gateway 停止期间所有客户端连接断开，需客户端重连。

### 4.2 Docker Compose 滚动升级

**无停机升级（需多实例 + 负载均衡）**：

当前 `docker-compose.yml` 为单实例部署，**不支持**零停机滚动升级。如需零停机，建议：

1. **使用 Kubernetes** 或 **Docker Swarm**，配置多副本 + 滚动更新策略
2. **Gateway 前置负载均衡**（如 Nginx / HAProxy），将流量分发到多个 Gateway 实例
3. **Runtime 使用会话粘滞**（通过 `session_id` 哈希路由到固定 Runtime 实例）

**简单重启（有停机）**：

```bash
# 拉取最新代码
git pull

# 重建并重启容器
docker compose -f deploy/docker-compose.yml up --build -d

# 查看日志
docker compose -f deploy/docker-compose.yml logs -f
```

**影响**：重启期间服务不可用（约 10-30 秒），所有客户端连接断开。

### 4.3 生产环境建议

1. **多实例部署**：
   - Gateway：至少 2 个实例，前置 Nginx/HAProxy 做 WebSocket 负载均衡
   - Runtime：根据并发 session 数量水平扩展（每个 Runtime 可处理数百并发 session）

2. **会话粘滞**：
   - 方案 A（当前实现）：Gateway 与 Runtime 一对一绑定，通过负载均衡器的 IP Hash 保证同一客户端连接到同一 Gateway
   - 方案 B（未实现）：Runtime 共享 session 状态（Redis/数据库），任意 Gateway 可连接任意 Runtime

3. **监控告警**：
   - 监控 `runs_active` 指标，设置阈值告警（如单实例超过 100 并发 run）
   - 监控 `cancelled_total` 异常增长（可能表示客户端频繁打断或超时）
   - 监控 Gateway `connections_active`，及时发现连接泄漏

4. **依赖服务高可用**：
   - LLM API：配置重试机制（当前 `ANTHROPIC_TIMEOUT_S` 控制单次超时）
   - 记忆服务：使用外部 REST 服务时需保证其高可用
   - 浏览器工具：Playwright/Patchright 需足够资源（CPU/内存），建议独立部署

---

## 5. 故障排查

### 5.1 Gateway 无法连接 Runtime

**症状**：Gateway 日志显示 `connection_failed` 或 `runtime_unreachable`

**排查**：

1. 确认 Runtime 已启动且监听 `127.0.0.1:8766`：

```bash
netstat -ano | findstr :8766
```

2. 检查 Gateway 配置的 `runtime_url`（默认 `ws://127.0.0.1:8766`）

3. 防火墙/安全组是否阻止本地回环连接

### 5.2 模型请求超时

**症状**：Runtime 日志显示 `llm_timeout` 或客户端长时间无响应

**排查**：

1. 检查 `ANTHROPIC_TIMEOUT_S` / `MINIMAX_TIMEOUT_S` 配置（默认 120 秒）
2. 检查网络连接到 LLM API 的延迟（`ping` / `curl` 测试）
3. 检查 LLM API 配额是否耗尽或触发限流
4. 增大超时配置或切换到备用 LLM 提供商

### 5.3 Skills 脚本执行失败

**症状**：日志显示 `skill_exec_error` 或工具返回错误

**排查**：

1. 检查 `SKILLS_PATHS` 是否正确指向 skills 目录
2. 确认 skill 的 `scripts/` 目录存在且脚本有执行权限
3. 检查脚本依赖的环境变量（如 `EXTRA_PATH`）是否配置
4. 查看 Runtime 日志中的 `stderr` 输出，定位脚本报错原因

### 5.4 记忆系统无法加载

**症状**：日志显示 `memory_recall_failed` 或记忆功能不生效

**排查**：

1. 检查 `MEMORY_SERVICE_URL` 配置（如使用外部服务）
2. 检查 `MEMORY_LOCAL_DIR` 权限（如使用本地存储）
3. 检查外部记忆服务的健康状态和网络连通性
4. 查看 `MEMORY_SERVICE_TIMEOUT_S` 是否过短

### 5.5 浏览器工具启动失败

**症状**：日志显示 `browser_init_error` 或 Playwright 报错

**排查**：

1. 确认已安装 Playwright 浏览器：

```bash
playwright install chromium
```

2. 检查系统资源（内存至少 2GB 可用）
3. 检查 `BROWSER_USER_DATA_DIR` 路径权限
4. Windows 环境确认 Chromium 依赖库已安装

---

## 6. 性能调优

### 6.1 并发能力

- **单 Runtime 实例**：可处理 100-500 并发 session（取决于 LLM API 响应速度）
- **瓶颈**：LLM API 调用为 I/O 密集型，CPU 占用低，主要受网络延迟影响

### 6.2 内存优化

- **会话消息压缩**：`MEMORY_MAX_MESSAGES=20` 控制单 session 保留消息数，超出后自动摘要
- **工具结果截断**：硬编码 `HARD_MAX_TOOL_RESULT_CHARS=400000`，防止超大工具输出撑爆内存

### 6.3 网络优化

- **LLM API**：使用地理位置接近的 API 端点（如 MiniMax CN 区）
- **WebSocket**：Gateway 与客户端之间启用压缩（需客户端支持 `permessage-deflate`）

---

## 附录：快速启动检查清单

- [ ] 已安装 Python 3.11+
- [ ] 已安装依赖：`pip install -r requirements.txt`
- [ ] 已安装本项目：`pip install -e .`
- [ ] 已配置 `.env` 文件（至少设置 `LLM_PROVIDER` 和对应 API Key）
- [ ] 已安装 Playwright 浏览器（如需浏览器工具）：`playwright install chromium`
- [ ] Runtime 已启动并监听 8766 端口
- [ ] Gateway 已启动并监听 8765 端口
- [ ] 使用 ping/pong 验证连通性
- [ ] 查看日志确认无错误
