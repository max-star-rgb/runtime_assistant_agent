# runTime 项目架构与流程图

## 整体架构

```mermaid
graph TB
    subgraph "外部客户端"
        Client[WebSocket Client<br/>CLI / Web UI]
    end

    subgraph "Gateway 进程 (8765)"
        GW[GatewayService<br/>WS 对外服务]
        GWAuth[鉴权 & 路由]
        GWProto[协议映射<br/>JSON 帧]
    end

    subgraph "Runtime 进程 (8766)"
        RT[RuntimeService<br/>WS 内网服务]
        Session[SessionManager<br/>会话管理]
        Agent[AnthropicAgentRuntime<br/>Agent 核心编排]
        
        subgraph "记忆系统"
            MC[MemoryClient<br/>记忆客户端]
            MCLocal[本地 JSON 存储]
            MCRemote[外部 REST API]
        end
        
        subgraph "Skills 系统"
            SR[SkillsRegistry<br/>技能发现]
            SE[SkillsExecutor<br/>脚本执行]
        end
        
        subgraph "工具系统"
            Tools[通用工具<br/>read/write/edit/exec<br/>web_search/web_fetch]
            Browser[browser 工具<br/>Patchright/Playwright]
        end
        
        subgraph "模型接入"
            LLM[LLM Client<br/>Anthropic/MiniMax/Qwen]
        end
    end

    subgraph "外部服务"
        ExtMem[记忆服务<br/>POST /memory/save<br/>GET /memory/recall]
        ExtAPI[第三方 API<br/>maishou88/Serper]
    end

    Client <-->|WebSocket| GW
    GW <-->|内网 WebSocket| RT
    RT --> Session
    Session --> Agent
    
    Agent --> MC
    MC -.->|MEMORY_SERVICE_URL 配置| MCRemote
    MC -.->|默认 fallback| MCLocal
    MCRemote <-->|REST| ExtMem
    
    Agent --> SR
    Agent --> SE
    Agent --> Tools
    Agent --> Browser
    Agent <-->|SSE 流式| LLM
    
    SE -.->|调用 skill 脚本| ExtAPI
    Tools -.->|web_search| ExtAPI

    style MC fill:#e1f5ff
    style Agent fill:#fff4e1
    style GW fill:#f0f0f0
    style RT fill:#f0f0f0
```

## 单次对话流程（含记忆）

```mermaid
sequenceDiagram
    participant Client
    participant Gateway
    participant Runtime
    participant Agent
    participant Memory
    participant LLM
    participant Tools

    Client->>Gateway: message.user (session_id, text)
    Gateway->>Runtime: 转发用户消息
    Runtime->>Agent: run_turn(session_id, user_text)
    
    Note over Agent,Memory: 1. 加载长期记忆
    Agent->>Memory: recall(user_id, query=user_text)
    Memory-->>Agent: memories: [{type, content, relevance}]
    
    Note over Agent: 2. 构建系统提示<br/>注入 <user_memory> 块
    
    Note over Agent: 3. 短期记忆压缩<br/>messages > 20 条时摘要
    
    Agent->>LLM: POST /v1/messages (system, messages, tools)
    
    loop Agentic Loop (最多 25 轮)
        LLM-->>Agent: SSE stream (text_delta / tool_use)
        Agent->>Gateway: stream.chunk
        Gateway->>Client: 流式输出
        
        alt 模型调用工具
            Agent->>Tools: 并发执行 tool_use
            Tools-->>Agent: tool_result
            Agent->>LLM: 继续对话 (tool_result)
        else 模型返回 end_turn
            Note over Agent: 退出循环
        end
    end
    
    Note over Agent,Memory: 4. 保存记忆
    Agent->>Agent: 提取用户偏好/事实/事件
    Agent->>Memory: save(user_id, entries)
    Memory-->>Agent: ok
    
    Agent->>Runtime: run.end (completed)
    Runtime->>Gateway: 转发结束事件
    Gateway->>Client: run.end
```

## Skills 执行流程

```mermaid
sequenceDiagram
    participant Agent
    participant LLM
    participant SkillsRegistry
    participant SkillsExecutor
    participant Script

    Note over Agent: 系统提示包含<br/><available_skills> 索引

    Agent->>LLM: 用户请求匹配 skill 描述
    LLM-->>Agent: tool_use: read(SKILL.md)
    
    Note over Agent: 激活 Skill 锁定<br/>active_skill = skill_id
    
    Agent->>SkillsRegistry: 读取 SKILL.md
    SkillsRegistry-->>Agent: skill 指令内容
    Agent->>LLM: tool_result (SKILL.md 内容)
    
    LLM-->>Agent: tool_use: exec(scripts/main.py)
    Agent->>SkillsExecutor: 执行脚本 (路径约束/超时)
    SkillsExecutor->>Script: subprocess.run
    Script-->>SkillsExecutor: stdout/stderr/exit_code
    SkillsExecutor-->>Agent: 执行结果
    
    Agent->>LLM: tool_result (脚本输出)
    LLM-->>Agent: 最终回复 (end_turn)
    
    Note over Agent: 释放 Skill 锁定<br/>tool_used_this_turn = False
```

## 浏览器工具流程（电商场景）

```mermaid
sequenceDiagram
    participant Agent
    participant Browser
    participant Page
    participant Site

    Note over Agent: 用户: "帮我把商品加入购物车"
    
    Agent->>Browser: set_cookies_from_file(taobao.json)
    Browser-->>Agent: 已设置 N 个 cookie
    
    Agent->>Browser: navigate(商品搜索页)
    Browser->>Page: goto(url)
    Page->>Site: HTTP GET
    
    alt 检测到验证码
        Note over Browser: 页面标题/正文含<br/>"验证码"/"访问异常"
        Browser->>Browser: 等待用户手动处理<br/>(headed 模式)
    end
    
    Site-->>Page: HTML
    Page-->>Browser: 页面加载完成
    Browser-->>Agent: status=200, title="..."
    
    Agent->>Browser: evaluate(JS 提取商品列表)
    Browser->>Page: page.evaluate(script)
    Page-->>Browser: [{title, url, price}]
    Browser-->>Agent: JSON 结果
    
    Agent->>Browser: navigate(商品详情页)
    Browser->>Page: goto(product_url)
    Page-->>Browser: 加载完成
    
    Agent->>Browser: click(selector="#add-to-cart")
    Browser->>Page: page.click(selector)
    Page->>Site: 加购请求
    Site-->>Page: 成功
    Page-->>Browser: ok
    Browser-->>Agent: "ok"
    
    Agent->>Browser: navigate(购物车页)
    Browser->>Page: goto(cart_url)
    Page-->>Browser: 购物车内容
    Browser-->>Agent: 验证成功
```

## 记忆系统详细流程

```mermaid
graph TB
    subgraph "Turn 开始"
        A[用户输入] --> B[MemoryClient.recall]
        B --> C{MEMORY_SERVICE_URL<br/>配置?}
        C -->|是| D[GET /memory/recall<br/>外部服务]
        C -->|否| E[本地 JSON 文件<br/>关键词匹配]
        D --> F[返回相关记忆]
        E --> F
        F --> G[注入 system_prompt<br/><user_memory> 块]
    end
    
    subgraph "Turn 进行中"
        G --> H[messages 压缩检查]
        H --> I{messages 数量<br/>> MEMORY_MAX_MESSAGES?}
        I -->|是| J[前半部分摘要为<br/>summary message]
        I -->|否| K[保持原样]
        J --> L[Agentic Loop]
        K --> L
    end
    
    subgraph "Turn 结束"
        L --> M[提取记忆条目]
        M --> N[正则匹配:<br/>偏好/事实/事件]
        N --> O[MemoryClient.save]
        O --> P{MEMORY_SERVICE_URL<br/>配置?}
        P -->|是| Q[POST /memory/save<br/>外部服务]
        P -->|否| R[追加到本地<br/>user_id.json]
        Q --> S[完成]
        R --> S
    end

    style G fill:#e1f5ff
    style M fill:#e1f5ff
    style H fill:#fff4e1
```

## 跨轮次 Skill 锁定机制

```mermaid
stateDiagram-v2
    [*] --> NoSkill: 初始状态
    
    NoSkill --> SkillLocked: read(SKILL.md)<br/>active_skill = skill_id
    
    SkillLocked --> SkillLocked: 工具调用<br/>tool_used_this_turn = True
    
    SkillLocked --> SkillLocked: 新 turn 开始<br/>且上轮有工具调用
    
    SkillLocked --> NoSkill: 新 turn 开始<br/>且上轮无工具调用<br/>(skill 完成)
    
    NoSkill --> NoSkill: 通用工具调用<br/>(web_search 等)<br/>不设置 active_skill
    
    note right of SkillLocked
        系统提示注入:
        [Active skill: xxx]
        Continue following its instructions.
        Do NOT start a different skill.
    end note
    
    note right of NoSkill
        Skill-first rule:
        匹配到 skill 描述时
        必须优先使用 skill
    end note
```

## 多 LLM 提供商切换

```mermaid
graph LR
    A[LLM_PROVIDER 环境变量] --> B{选择提供商}
    
    B -->|anthropic| C[AnthropicClient<br/>x-api-key auth<br/>api.anthropic.com]
    B -->|minimax| D[AnthropicClient<br/>Bearer auth<br/>api.minimaxi.com]
    B -->|qwen| E[AnthropicClient<br/>Bearer auth<br/>dashscope.aliyuncs.com]
    
    C --> F[统一 Anthropic 兼容接口<br/>/v1/messages + SSE]
    D --> F
    E --> F
    
    F --> G[AnthropicAgentRuntime<br/>tool_use 循环]
    
    style F fill:#e1f5ff
```

## 部署架构

```mermaid
graph TB
    subgraph "单机部署 (Docker Compose)"
        GW[Gateway 容器<br/>:8765]
        RT[Runtime 容器<br/>:8766]
        GW <-->|内网| RT
    end
    
    subgraph "外部依赖"
        LLM[LLM API<br/>Anthropic/MiniMax/Qwen]
        Mem[记忆服务<br/>可选]
        API[第三方 API<br/>可选]
    end
    
    Client[客户端] <-->|WebSocket| GW
    RT <-->|HTTPS| LLM
    RT <-.->|REST| Mem
    RT <-.->|HTTP| API
    
    style GW fill:#f0f0f0
    style RT fill:#f0f0f0
```

## 配置项总览

| 类别 | 环境变量 | 说明 |
|------|----------|------|
| **模型** | `LLM_PROVIDER` | anthropic / minimax / qwen |
| | `ANTHROPIC_API_KEY` | Anthropic API 密钥 |
| | `MINIMAX_API_KEY` | MiniMax API 密钥 |
| | `QWEN_API_KEY` | Qwen API 密钥 |
| **Skills** | `SKILLS_PATHS` | 额外 skills 目录 (分号分隔) |
| | `SKILLS_MAX_PROMPT_CHARS` | 提示词字符预算 (默认 30000) |
| | `SKILLS_MAX_IN_PROMPT` | 最多包含 skill 数 (默认 150) |
| **浏览器** | `BROWSER_HEADLESS` | true/false (默认 true) |
| | `BROWSER_TIMEOUT_S` | 页面操作超时 (默认 30) |
| | `BROWSER_USER_DATA_DIR` | 持久化数据目录 |
| | `TAOBAO_COOKIE_FILE` | 淘宝 cookie 文件路径 |
| **记忆** | `MEMORY_SERVICE_URL` | 外部记忆服务地址 |
| | `MEMORY_LOCAL_DIR` | 本地存储目录 |
| | `MEMORY_MAX_MESSAGES` | 消息压缩阈值 (默认 20) |
| | `MEMORY_RECALL_LIMIT` | 检索记忆条数 (默认 10) |
| **工具** | `PRICE_COMPARE_SERVICE_URL` | 比价服务地址 |
| | `SERPER_API_KEY` | Serper 搜索 API |
| **运维** | `LOG_LEVEL` | 日志级别 (默认 INFO) |
| | `MAX_TOOL_ROUNDS` | 工具循环上限 (默认 25) |
| | `EXTRA_WORKSPACE_DIRS` | 额外工作目录 |
| | `EXTRA_PATH` | 额外 PATH 目录 |

---

*生成时间: 2026-04-13*
*项目版本: openclaw-gateway-runtime (45/46 tasks complete - 新增记忆系统)*
