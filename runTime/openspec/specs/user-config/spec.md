## ADDED Requirements

### Requirement: 双层用户配置合并
RuntimeService 初始化时 SHALL 将来电初始配置与本地热配置 JSON 合并，本地热配置优先级高于来电配置，来电配置高于系统默认值。

#### Scenario: 本地热配置存在时覆盖来电配置
- **WHEN** call.incoming 携带 language=zh-CN，但本地 config.json 中 language=en-US
- **THEN** RuntimeService SHALL 使用 en-US 作为最终语言配置

#### Scenario: 本地热配置不存在时使用来电配置
- **WHEN** call.incoming 携带 language=zh-CN，本地无 config.json
- **THEN** RuntimeService SHALL 使用 zh-CN 作为最终语言配置

### Requirement: Agent 内置工具 update_user_config（通道1）
AnthropicAgentRuntime SHALL 注册内置工具 update_user_config，当用户在会话中要求修改 Agent 行为设定时，Agent 可调用该工具更新配置。

#### Scenario: Agent 更新语言配置
- **WHEN** 用户说「请用英语回答」，Agent 调用 update_user_config(key=language, value=en-US)
- **THEN** RuntimeService SHALL 更新内存中的 UserConfig.language，并异步写入本地 config.json

#### Scenario: 配置更新持久化
- **WHEN** update_user_config 工具被调用
- **THEN** 系统 SHALL 在本地 USER_CONFIG_DIR/users/<user_id>/config.json 中持久化新值，下次来电生效

### Requirement: 外部系统配置下发（通道2）
Gateway SHALL 处理 config.update 帧，将配置变更注入目标用户的 RuntimeService（用户在线）或直接写入本地 JSON（用户离线）。

#### Scenario: 用户在线时注入配置
- **WHEN** Gateway 收到 config.update 帧且目标 user_id 在路由表中
- **THEN** Gateway SHALL 将配置变更注入对应 RuntimeService，立即生效

#### Scenario: 用户离线时持久化配置
- **WHEN** Gateway 收到 config.update 帧且目标 user_id 不在路由表中
- **THEN** Gateway SHALL 直接写入本地 config.json，下次来电时生效

### Requirement: 用户配置注入系统提示
RuntimeService SHALL 将 UserConfig 中的 user_info、language 等字段注入 Agent 系统提示，使 Agent 行为符合用户设定。

#### Scenario: 语言设定注入
- **WHEN** UserConfig.language = en-US
- **THEN** Agent 系统提示 SHALL 包含使用英语回答的指令，Agent 后续回复使用英语
