## MODIFIED Requirements

### Requirement: session_id 由 RuntimeManager 分配
原行为：session_id 由客户端在 session.open 帧中传入。
新行为：session_id SHALL 由 RuntimeManager 在创建 RuntimeService 时自动生成，通过 call.ready 帧返回给客户端。客户端后续消息中的 session_id 为可选（用于校验），如未提供则使用 RuntimeManager 分配的值。

#### Scenario: 来电时自动分配 session_id
- **WHEN** Gateway 收到 call.incoming 并创建 RuntimeService
- **THEN** Gateway SHALL 生成唯一 session_id 并在 call.ready 帧中返回

#### Scenario: 后续消息 session_id 兼容
- **WHEN** 客户端在 message.user 中携带 session_id
- **THEN** Gateway SHALL 使用该 session_id（与分配值一致时正常处理；不一致时记录警告但继续处理）

## ADDED Requirements

### Requirement: session.open 兼容路径保留
非通话客户端 SHALL 仍可使用 session.open 帧发起会话，Gateway 使用默认配置创建 RuntimeService 并分配 user_id（基于连接 ID 生成）。

#### Scenario: 旧客户端兼容
- **WHEN** 客户端发送 session.open 帧（无 user_id）
- **THEN** Gateway SHALL 生成临时 user_id，创建 RuntimeService，按原有流程处理
