# 提案：多用户 Runtime 路由与按用户隔离（电信通话场景）

## Why

当前 Gateway 将所有外部连接路由到同一个 Runtime 实例，无法支持按用户隔离的 Agent 配置与记忆。为支撑电信通话场景（每路来电对应一个用户号码，需独立 Agent 设定、热配置更新与跨会话记忆），需要在 Gateway 引入 RuntimeManager，实现每用户一个 RuntimeService 实例的路由模型。

## What Changes

- **新增 call.incoming / call.ready / call.hangup 协议帧**：替代原有 session.open，携带 user_id 与初始用户配置
- **所有协议帧新增 user_id 字段**：Gateway 回传响应时携带 user_id，客户端可按用户路由
- **新增 RuntimeManager**：Gateway 内维护 user_id to RuntimeService 路由表，来电时查找或创建对应实例，挂断时销毁并持久化配置与记忆
- **新增双层用户配置模型**：来电携带初始配置（call.incoming.payload）+ 本地热配置 JSON（跨会话持久化），两层合并后注入 RuntimeService
- **Agent 注册内置工具 update_user_config**：会话中用户要求修改设定（如切换语言）时，Agent 调用该工具更新内存配置并持久化
- **新增 config.update 消息帧**：外部系统可主动下发配置变更到指定用户的 RuntimeService
- **协议帧 modality 预留字段**：message.user.payload 增加 modality（当前仅 text），为未来音视频输入预留扩展口
- **Demo 阶段采用同进程协程模式**：RuntimeManager 在 Gateway 进程内管理多个 RuntimeService 协程实例，无需独立进程或容器，未来可无缝替换为容器化实现

## Capabilities

### New Capabilities

- user-routing：Gateway 按 user_id 路由消息到对应 RuntimeService 实例，维护路由表生命周期（创建/查找/销毁）
- user-config：双层用户配置模型（来电初始配置 + 本地热配置），支持会话内 Agent 自主更新与外部系统下发更新
- call-protocol：面向电信通话场景的协议帧扩展（call.incoming/ready/hangup、config.update、modality 预留字段）

### Modified Capabilities

- session-management：原 session.open/resume 被 call.incoming/ready 取代；session_id 由 RuntimeManager 在创建时分配，不再由客户端传入

## Impact

- **gateway/ws_server.py**：handler 中引入 RuntimeManager，call.incoming 触发路由/创建逻辑
- **gateway/gateway.py**：GatewayService.bridge() 接口不变，新增 call.* 和 config.update 帧处理分支
- **runtime/runtime.py**：RuntimeService 构造函数接受 user_id 与 UserConfig 参数
- **agent_runtime/anthropic_runtime.py**：注册内置工具 update_user_config
- **protocol.py**：新增帧类型与 user_id、modality 字段
- **新增文件**：gateway/runtime_manager.py、gateway/user_config.py
- **无新外部依赖**：配置持久化使用本地 JSON 文件（与记忆系统一致）
