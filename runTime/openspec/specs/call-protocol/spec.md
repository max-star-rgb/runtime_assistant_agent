## ADDED Requirements

### Requirement: call.incoming 帧
客户端 SHALL 通过发送 call.incoming 帧发起通话，帧中包含 user_id 和初始用户配置。

#### Scenario: 标准来电帧
- **WHEN** 客户端发送 {"v":1,"type":"call.incoming","user_id":"13800138000","payload":{"language":"zh-CN","user_info":"VIP用户"}}
- **THEN** Gateway SHALL 解析 user_id 和 payload，触发 RuntimeManager 创建或复用实例

### Requirement: call.ready 帧
Gateway SHALL 在 RuntimeService 就绪后回复 call.ready 帧，包含分配的 session_id。

#### Scenario: 就绪回复
- **WHEN** RuntimeService 创建完成
- **THEN** Gateway SHALL 回复 {"v":1,"type":"call.ready","user_id":"13800138000","session_id":"s-xxx"}

### Requirement: call.hangup 帧
客户端 SHALL 通过发送 call.hangup 帧结束通话；Gateway SHALL 立即回复 call.hangup_ack。

#### Scenario: 挂断确认
- **WHEN** 客户端发送 {"v":1,"type":"call.hangup","user_id":"13800138000"}
- **THEN** Gateway SHALL 立即回复 {"v":1,"type":"call.hangup_ack","user_id":"13800138000"}

### Requirement: 所有帧携带 user_id
Gateway 回传给客户端的所有响应帧 SHALL 携带 user_id 字段，以便客户端按用户路由。

#### Scenario: stream.chunk 携带 user_id
- **WHEN** Agent 生成流式输出 stream.chunk
- **THEN** Gateway SHALL 在转发给客户端时附加 user_id 字段

### Requirement: modality 字段预留
message.user 帧的 payload SHALL 支持 modality 字段，当前有效值为 text；其他值 SHALL 返回 error 帧，错误码 unsupported_modality。

#### Scenario: 文字消息
- **WHEN** 客户端发送 message.user 且 payload.modality = text（或未设置）
- **THEN** Gateway SHALL 正常处理该消息

#### Scenario: 不支持的 modality
- **WHEN** 客户端发送 message.user 且 payload.modality = audio
- **THEN** Gateway SHALL 回复 error 帧，错误码 unsupported_modality，消息 audio modality not yet supported
