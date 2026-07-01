# 设计：多用户 Runtime 路由（电信通话场景）

## Context

本项目当前为单 Gateway + 单 Runtime 双进程架构，所有客户端连接路由到同一 Runtime 实例，通过 session_id 区分会话。

新需求：电信通话场景，每路来电携带用户手机号（user_id），需独立 Agent 配置与跨会话记忆。

关键约束：
- GatewayService.bridge() 已抽象为 transport-agnostic，只依赖 Endpoint 接口
- RuntimeService 已是纯 asyncio 实现，可在同进程内多实例化
- MemoryClient 已支持按 user_id 隔离（本地文件路径含 user_id）

## Goals / Non-Goals

**Goals:**
- Gateway 按 user_id 路由消息到专属 RuntimeService 实例
- 来电（call.incoming）时自动创建 RuntimeService，挂断（call.hangup）时销毁并持久化
- 双层用户配置：来电初始配置 + 本地热配置 JSON，两层合并注入 RuntimeService
- Agent 通过 update_user_config 工具修改配置并持久化（通道1：Agent 自主识别）
- 外部系统通过 config.update 帧主动下发配置变更（通道2：外部注入）
- 协议帧统一携带 user_id，回传响应时透传
- modality 字段预留，为未来音视频输入提供扩展点
- 结构化日志完整覆盖所有新增流程

**Non-Goals:**
- 容器化部署（Demo 阶段同进程协程，架构已预留替换接口）
- 预热池、弹性扩缩容（Demo 规模不需要）
- 多 Gateway 实例路由表同步（单机部署）
- ASR/TTS 集成（输入输出均为文字）
- 用户鉴权（Demo 阶段信任 user_id）

## Decisions

### D1：同进程协程模式（非独立进程）

RuntimeManager 在 Gateway 进程内直接实例化 RuntimeService 协程。

理由：冷启动 < 10ms（vs 子进程 2-3s），满足通话首次响应要求；未来替换容器化只需换 RuntimeManager 实现，其余代码不动。

替代：子进程模式（完全隔离，但启动慢）—— 留作生产化路径。

### D2：RuntimeManager 作为依赖注入，GatewayService 保持不变

RuntimeManager 在 serve_gateway_ws() 中创建，通过 handler 闭包传入：

```
serve_gateway_ws()
  runtime_manager = RuntimeManager(config)
  handler(client_ws):
    user_id  <- call.incoming
    rt_ep    = await runtime_manager.get_or_create(user_id, user_config)
    await gateway.bridge(client_ep, rt_ep)
```

GatewayService.bridge() 签名不变，无需修改。

### D3：双层配置合并，本地热配置优先

合并优先级（高 -> 低）：本地 JSON > call.incoming.payload > 系统默认值

存储路径：USER_CONFIG_DIR/users/<user_id>/config.json

配置字段（示例）：
```json
{
  "user_id": "13800138000",
  "language": "zh-CN",
  "user_info": "VIP用户",
  "agent_profile": "customer_service",
  "tone": "friendly",
  "last_updated": "2026-04-15T10:00:00Z"
}
```

### D4：update_user_config 为 Agent 内置工具（通道1）

在 AnthropicAgentRuntime 中注册内置工具，Agent 自主识别用户配置变更意图（如「请换英文」）并调用：
- 更新 RuntimeService 内存中的 UserConfig
- 异步写入本地 JSON，下次来电生效

工具定义：
- name: update_user_config
- description: 当用户要求修改 Agent 行为设定时调用（切换语言、调整语气等）
- input: key（配置键）、value（新值）

### D5：config.update 帧由 Gateway 直接注入（通道2）

外部系统发送帧格式：

```json
{"v":1,"type":"config.update","user_id":"13800138000","payload":{"key":"language","value":"en-US"}}
```

用户在线：路由到对应 RuntimeService 注入配置。
用户离线：直接写本地 JSON，下次来电生效。

### D6：call.hangup 触发异步清理，不阻塞响应

Gateway 收到 call.hangup 后立即回 call.hangup_ack，后台异步执行：
保存记忆 -> 写热配置 -> 销毁 RuntimeService -> 清理路由表

## Risks / Trade-offs

- [同进程异常传播] RuntimeService 未捕获异常可能影响 Gateway 进程 -> 为每个实例的 Task 加 try/except，异常时记录日志并销毁该实例，不影响其他用户
- [路由表内存丢失] Gateway 重启后路由表清空，在线用户断线 -> Demo 阶段可接受（重拨即可）；生产化时引入 Redis 持久化
- [热配置并发写] Agent 工具调用与外部 config.update 可能并发写同一文件 -> 同一 asyncio 事件循环内串行执行，无竞态
- [首轮记忆未就绪] 冷启动后记忆异步加载，第一轮对话使用默认配置 -> Demo 阶段可接受，通话开场通常是寒暄

## Migration Plan

1. 新增 protocol.py 帧类型和字段（user_id、modality，向后兼容，旧帧仍可接受）
2. 新增 gateway/runtime_manager.py 和 gateway/user_config.py
3. 修改 gateway/ws_server.py handler，引入 RuntimeManager
4. 修改 gateway/gateway.py，新增 call.* 和 config.update 帧处理分支
5. 修改 runtime/runtime.py，构造函数接受 user_id 和 UserConfig
6. 修改 agent_runtime/anthropic_runtime.py，注册 update_user_config 工具
7. 保留 session.open/resume 兼容路径（非通话客户端仍可用）

## Open Questions

- USER_CONFIG_DIR 是否通过环境变量配置？建议默认与 MEMORY_LOCAL_DIR 同目录下的 users/ 子目录
- call.incoming 的 agent_profile 字段是否需映射到不同 system_prompt 模板？Demo 阶段直接将 user_info 拼入 system_prompt，后续按需扩展
