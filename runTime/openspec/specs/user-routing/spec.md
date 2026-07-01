## ADDED Requirements

### Requirement: Gateway 按 user_id 路由到专属 RuntimeService
Gateway SHALL 维护一个内存路由表（user_id -> RuntimeService），所有消息帧通过 user_id 查表路由到对应实例。

#### Scenario: 来电时创建新 RuntimeService
- **WHEN** Gateway 收到 call.incoming 帧且路由表中无对应 user_id
- **THEN** Gateway SHALL 创建新的 RuntimeService 实例并注册到路由表，分配 session_id 后回复 call.ready

#### Scenario: 来电时复用已有 RuntimeService
- **WHEN** Gateway 收到 call.incoming 帧且路由表中已有对应 user_id
- **THEN** Gateway SHALL 直接使用已有实例，回复 call.ready

#### Scenario: 会话消息路由
- **WHEN** Gateway 收到携带 user_id 的 message.user 帧
- **THEN** Gateway SHALL 将消息路由到该 user_id 对应的 RuntimeService 实例

#### Scenario: user_id 无对应实例
- **WHEN** Gateway 收到 message.user 但路由表中无该 user_id
- **THEN** Gateway SHALL 回复 error 帧，错误码 no_runtime

#### Scenario: 挂断时清理路由
- **WHEN** Gateway 收到 call.hangup 帧
- **THEN** Gateway SHALL 立即回复 call.hangup_ack，并在后台异步销毁 RuntimeService 并从路由表中移除该 user_id

#### Scenario: 最大实例数限制
- **WHEN** 创建新 RuntimeService 时当前实例数已达 MAX_RUNTIME_INSTANCES 上限
- **THEN** Gateway SHALL 回复 error 帧，错误码 runtime_limit_reached

#### Scenario: RuntimeService 异常崩溃
- **WHEN** 某个 RuntimeService 实例内部抛出未捕获异常
- **THEN** Gateway SHALL 记录错误日志，销毁该实例并从路由表中移除，不影响其他用户的 RuntimeService 实例
