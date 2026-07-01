# 任务清单：多用户 Runtime 路由（电信通话场景）

> 按顺序完成；每个阶段完成后可独立验证。

## 1. 协议层扩展（protocol.py）

- [x] 1.1 在 Frame TypedDict 新增 user_id 字段（Optional[str]）
- [x] 1.2 新增帧类型常量：call.incoming、call.ready、call.hangup、call.hangup_ack、config.update
- [x] 1.3 在 message.user 的 payload 定义中预留 modality 字段（默认 text）

## 2. 用户配置模块（新增 gateway/user_config.py）

- [x] 2.1 定义 UserConfig dataclass：user_id、language、user_info、agent_profile、tone、last_updated
- [x] 2.2 实现 load_user_config(user_id) -> UserConfig：读取本地 JSON（USER_CONFIG_DIR/users/<user_id>/config.json）
- [x] 2.3 实现 merge_config(local: UserConfig, incoming: dict) -> UserConfig：本地热配置优先合并来电初始配置
- [x] 2.4 实现 save_user_config(config: UserConfig)：异步写入本地 JSON，自动创建目录
- [x] 2.5 从 .env.example 新增 USER_CONFIG_DIR 配置项说明

## 3. RuntimeManager（新增 gateway/runtime_manager.py）

- [x] 3.1 定义 RuntimeManager 类，内含路由表 Dict[str, RuntimeService] 和 asyncio.Lock
- [x] 3.2 实现 get_or_create(user_id, user_config) -> Endpoint：查路由表，无则创建新 RuntimeService 并注册
- [x] 3.3 实现 destroy(user_id)：异步销毁 RuntimeService，从路由表移除，调用 save_user_config 持久化
- [x] 3.4 实现 inject_config(user_id, key, value)：更新内存 UserConfig + 异步写入本地 JSON（用于通道2）
- [x] 3.5 实现 MAX_RUNTIME_INSTANCES 上限检查，超限时抛出异常
- [x] 3.6 为每个 RuntimeService Task 添加异常捕获，异常时自动销毁并记录结构化日志

## 4. RuntimeService 接受用户配置（runtime/runtime.py）

- [x] 4.1 RuntimeService 构造函数新增 user_id: str 和 user_config: UserConfig 参数
- [x] 4.2 将 user_id 注入 SessionState（新增 user_id 字段）
- [x] 4.3 将 UserConfig 传递给 AnthropicSkillsAdapter 以供系统提示注入

## 5. Gateway 新增帧处理（gateway/gateway.py）

- [x] 5.1 在 GatewayService.bridge() 的 _client_to_runtime 中新增 call.incoming 处理分支
- [x] 5.2 新增 call.hangup 处理分支：立即回复 call.hangup_ack，后台触发 RuntimeManager.destroy()
- [x] 5.3 新增 config.update 处理分支：调用 RuntimeManager.inject_config() 或直接写 JSON（用户离线）
- [x] 5.4 在 _runtime_to_client 中，所有转发给客户端的帧附加 user_id 字段
- [x] 5.5 message.user 处理中新增 modality 检查，非 text 时回复 error(unsupported_modality)

## 6. Gateway WS Server 集成（gateway/ws_server.py）

- [x] 6.1 在 serve_gateway_ws() 中创建 RuntimeManager 实例
- [x] 6.2 修改 handler 函数：解析首帧 call.incoming 提取 user_id 和 payload，调用 load_user_config + merge_config
- [x] 6.3 通过 RuntimeManager.get_or_create() 获取 RuntimeEndpoint，再调用 gateway.bridge()
- [x] 6.4 保留 session.open 兼容路径：无 user_id 时生成临时 user_id，走默认配置

## 7. Agent 内置工具 update_user_config（agent_runtime/anthropic_runtime.py）

- [x] 7.1 定义 update_user_config 工具描述（name、description、input_schema: key + value）
- [x] 7.2 在工具注册列表中加入 update_user_config（仅当 user_id 存在时注册）
- [x] 7.3 实现工具执行：更新 SessionState 中的 UserConfig 字段，调用 save_user_config 异步持久化
- [x] 7.4 系统提示中注入 UserConfig：将 language、user_info 等字段拼入 system_prompt

## 8. 日志与验收

- [x] 8.1 所有新增流程补充结构化日志：call_incoming、runtime_created、runtime_destroyed、config_updated
- [x] 8.2 端到端验证：来电 -> call.ready -> 多轮对话 -> 挂断，日志链路完整
- [x] 8.3 验证双层配置合并：本地热配置覆盖来电配置
- [x] 8.4 验证通道1：会话中说「请用英语」，Agent 调用 update_user_config，后续回复切换为英语
- [x] 8.5 验证通道2：发送 config.update 帧，用户在线时立即生效，离线时下次来电生效
- [x] 8.6 验证 user_id 透传：所有 Gateway 响应帧均携带正确的 user_id
- [x] 8.7 验证 modality 预留：发送 modality=audio 时收到 unsupported_modality 错误
- [x] 8.8 验证实例上限：超过 MAX_RUNTIME_INSTANCES 时收到 runtime_limit_reached 错误
