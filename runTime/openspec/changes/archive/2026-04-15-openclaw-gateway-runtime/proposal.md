# 提案：在本项目实现 OpenClaw 风格 Agent Runtime + Skills（对齐 OpenClaw 工具体系与 Agent Skills 标准）

## 摘要

在本项目中实现 **OpenClaw 风格的 Agent Runtime 核心能力**，使 skills 的"使用体验"**对齐 OpenClaw**，工具体系对齐 OpenClaw tool-catalog 核心梯队：

- **Skills 目录架构**：遵循 [Agent Skills 标准](https://agentskills.io/specification)，每个 skill 为一个文件夹，含 `SKILL.md`（YAML frontmatter + 自然语言指令）与可选 `scripts/ references/ assets/`。
- **渐进式 disclosure**：模型常驻仅看到 skills 的 name/description/location/baseDir（技能索引），当决定使用某个 skill 时才通过 `read` 工具读取 `SKILL.md` 正文，再通过 `exec` 等通用工具运行脚本。
- **工具对齐 OpenClaw**：`read`、`write`、`edit`、`exec`、`web_search`（DuckDuckGo）、`web_fetch`、**`browser`（Playwright）**，参数名与 OpenClaw 一致。
- **浏览器自动化**：`browser` 工具提供 navigate/snapshot/screenshot/click/type/select 六个核心动作，对齐 OpenClaw browser tool 的交互模型（accessibility tree + ref 编号定位）。
- **多轮 agentic loop**：tool_use → 执行 → tool_result → 再调模型，直到模型返回 `end_turn`（最多 `MAX_TOOL_ROUNDS` 轮），和 OpenClaw pi-agent-core 行为对齐。
- **并发工具执行**：同一批 tool_use 中的多个调用通过 `asyncio.gather` 并发。
- **SKILL.md 格式互通**：OpenClaw 的 `.pi/skills/*/SKILL.md` 可直接放入本项目 `skills/` 目录使用。
- **跨轮次 Skill 锁定**：skill 执行跨越多轮用户交互时保持锁定，防止模型中途切换到其他 skill。
- **多模型支持**：通过 `LLM_PROVIDER` 显式选择 Anthropic / MiniMax / Qwen 模型提供商。
- **记忆能力**：短期会话记忆（超阈值自动摘要压缩）+ 长期跨会话记忆（通过可插拔的外部 REST 服务接口存取，内置本地 JSON 文件后端作为默认）。

## 动机

- **边缘与算力解耦**：Gateway 处理连接/鉴权/协议；Runtime 专注模型编排/工具/Skills，便于分别扩缩容与升级。
- **技能化**：将"每次都解释如何做"的提示工程，转为可复用的 Skills 操作手册，让 agent 自动发现并使用。
- **交互完整性**：多轮上下文、用户打断当前生成、显式取消某次执行（run），在协议与 Runtime 中显式建模。
- **生态兼容**：SKILL.md 格式与 OpenClaw、Anthropic Skills、pi-coding-agent 互通。
- **浏览器能力**：Agent 需要在无法通过 API 获取信息时，直接操作浏览器完成页面交互。
- **记忆连续性**：Agent 需要在多次会话之间保持对用户偏好、重要事实的记忆，提升个性化体验。

## 目标

1. **双组件架构**：Gateway（WS 对外）与 Runtime 进程分离。
2. **协议完备**：run 级取消与打断；流式输出；工具事件。
3. **工具对齐 OpenClaw 核心梯队**：read/write/edit/exec/web_search/web_fetch/browser。
4. **多轮 agentic loop**：模型自主决定何时停止工具调用。
5. **Skills 标准互通**：OpenClaw SKILL.md 可直接复用；支持 `enabled` 字段禁用 skill。
6. **可观测**：session_id / turn_id / run_id 在日志中可关联。
7. **跨轮次 Skill 锁定**：skill 执行期间强制唯一，完成后自动释放。
8. **多 LLM 提供商**：Anthropic / MiniMax / Qwen 通过统一接口切换。
9. **记忆系统**：短期会话摘要 + 长期跨会话记忆，可插拔外部记忆服务。

## 非目标

- OpenClaw 完整 browser 功能（多 profile、远程 CDP、cookie 管理、PDF 导出、网络拦截等）。
- 完整多租户计费与配额。
- 替代 OpenClaw 自带 UI。
- 完整复刻 OpenClaw 全量功能（多渠道、完整配置系统、沙箱）。

## 成功标准

- 端到端验证：多轮对话、取消、打断、skill 触发执行、多轮工具循环、并发搜索比价。
- 端到端验证：browser 工具可 navigate → snapshot → click/type → 确认页面变化。
- OpenClaw 的 SKILL.md 放入 `skills/` 可被发现和使用。
- 跨轮次 skill 锁定：skill 执行中新 turn 仍锁定在当前 skill；通用工具不阻塞后续 skill 切换。
- 记忆系统：长期记忆跨会话持久化并在新 turn 中正确注入；短期记忆超阈值时自动压缩。

## 依赖与假设

- 模型：通过 `LLM_PROVIDER` 指定（Anthropic / MiniMax / Qwen），均使用 Anthropic 兼容 `/v1/messages` 接口。
- 浏览器：Playwright + Chromium（需 `playwright install chromium`）；可选 Patchright 替代以增强反检测。
- 记忆：内置本地 JSON 文件后端（零额外依赖）；可选对接外部记忆服务（REST API）。
- OpenClaw 源码可参考；本项目不强依赖 OpenClaw 可执行产物。
- 运行环境可部署至少两个独立服务（Gateway、Runtime）。

---

*本变更由 OpenSpec「spec-driven」流程管理；实现阶段执行 `tasks.md`。*
