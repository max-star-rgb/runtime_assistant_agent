# 实时视频理解集成设计方案

## 1. 背景与目标

当前 agent 是纯文字交互（客户端 ASR→文字→服务端→文字→TTS→客户端）。需要增加视频理解能力，让用户可以在通话中指着画面问"这是什么"、"帮我搜同款"等。

**核心挑战**：支持实时音视频的大模型（如 Qwen-Omni-Realtime）工具调用能力远弱于文字模型（qwen-plus）。需要双模型协作。

**设计目标**：
- 视频理解延迟 < 2s（通话可接受）
- 文字模型主控对话和工具调用不受影响
- 视觉数据同时作为记忆数据源
- 架构预留升级到 Realtime 长会话的能力

---

## 2. 架构总览

```
客户端
├── 摄像头 → 1FPS JPEG（480p，<60KB）→ video.frame
├── 麦克风 → ASR → message.user（文字）
└── 扬声器 ← TTS ← stream.chunk（文字）

Gateway (8765)
└── 转发 video.frame + message.user → Runtime

Runtime (per-user)
├── 帧缓冲区（环形，最近 5 帧）
│   ├──→ 记忆服务（持续推帧，服务自行判断存储策略）
│   └──→ VisionClient（用户说话时取最近 3 帧）
├── VisionClient（qwen-vl-plus HTTP API）
│   ← 3 帧 + 用户文字 → 视觉描述（1-1.5s）
│   ← 描述不精确时可 fallback 以图搜图（未来）
├── 记忆服务（已有外部服务）
│   ← 持续接收帧（自行判断保留）
│   ← 会话建立时 recall 返回用户近期历史（含视觉记忆）
│   ← 文字模型按需 recall 检索历史
└── 文字模型（qwen-plus tool loop）
    ← 用户文字 + 视觉上下文 + 记忆
    → 回复 + 工具调用
```

---

## 3. 协作模式

**文字模型主控，视觉模型做理解工具。**

- 文字模型（qwen-plus）：主控对话、工具调用（搜索、加购等）
- 视觉模型（qwen-vl-plus）：接收帧+文字，返回画面描述
- 记忆服务：持续接收帧，存储为视觉记忆，支持跨 session 检索

---

## 4. 核心流程

### 4.1 每条消息处理

```
message.user 到达 Runtime：
1. 从帧缓冲区取最近 3 帧
2. 如果有帧数据：
   a. 并行调 VisionClient（3帧 + 用户文字 → 描述）
   b. 超时 2s，超时则跳过视觉上下文
   c. 如果返回相关描述 → 注入 "[视觉上下文] xxx" 到 messages
3. 文字模型正常执行 tool loop
```

### 4.2 会话建立

```
call.incoming → Runtime 创建/复用
→ 首次 run_turn：
  → memory_client.recall(user_id, query)
  → 返回近期记忆（含视觉记忆条目）
  → 注入 system prompt
```

### 4.3 记忆分层

| 范围 | 数据来源 | 检索方式 |
|------|----------|----------|
| 当前 session | 视觉描述在 messages 历史中 | 文字模型直接看到 |
| 跨 session | 记忆服务存储的帧+元数据 | memory_client.recall() |

### 4.4 时序示例

```
t=0s  用户看着一双鞋
t=1s  帧到达服务端 → 推给记忆服务
t=2s  用户："这是什么？"
      → 取最近 3 帧 → VisionClient
      → 1.2s 后返回 "红色耐克Air Max 90运动鞋"
      → 注入文字模型
      → 文字模型回复 "画面里是一双红色耐克Air Max 90"
      → 总延迟 ~2.5s

t=30s 用户："帮我搜同款"
      → VisionClient 返回 "用户仍在看同一双鞋"
      → 文字模型：调 taobao skill 搜索 "耐克Air Max 90 红色"

下次通话：
      → 会话建立 → recall 返回 "上次看过红色耐克Air Max 90"
      → 用户："上次那双鞋帮我买" → 文字模型从记忆中找到
```

---

## 5. 新增组件

### 5.1 协议帧：video.frame

```json
{
  "v": 1,
  "type": "video.frame",
  "user_id": "008622336699",
  "payload": {
    "data": "<base64 JPEG, 480p, <60KB>",
    "timestamp": 1719300000.123
  }
}
```

### 5.2 FrameBuffer（帧缓冲区）

```python
class FrameBuffer:
    """Per-user 环形帧缓冲区"""
    
    def __init__(self, max_frames: int = 5):
        self._frames: deque[tuple[float, bytes]] = deque(maxlen=max_frames)
    
    def append(self, timestamp: float, data: bytes) -> None:
        """客户端推帧时调用"""
    
    def get_recent(self, count: int = 3) -> list[bytes]:
        """取最近 N 帧"""
    
    def has_frames(self) -> bool:
        """是否有帧数据（判断是否需要调视觉模型）"""
```

- 位置：RuntimeManager 层，per-user
- Gateway 收到 `video.frame` 后转发给对应 user 的 FrameBuffer
- 同时推送给记忆服务

### 5.3 VisionClient（视觉理解客户端）

```python
class VisionClient:
    """视觉理解接口（当前：qwen-vl-plus HTTP）"""
    
    async def understand(
        self,
        frames: list[bytes],   # JPEG 帧列表
        query: str,            # 用户文字
        timeout: float = 2.0,
    ) -> str | None:
        """返回视觉描述，不相关或超时返回 None"""
```

- 内部实现：压缩帧到 480p → base64 → 多图 + 短 prompt → qwen-vl-plus
- prompt："这是最近几秒的连续画面。用户问'{query}'。简洁描述画面中与问题相关的物品（品牌+型号+特征）。如果画面与问题无关，回复'无关'。"
- max_tokens=100，限制输出长度保证速度
- 未来可替换内部实现为 Qwen-Omni-Realtime WebSocket 长会话

### 5.4 记忆服务对接

```python
# 已有接口，不需要新增
memory_client.recall(user_id, query)  # 检索（含视觉记忆）

# 新增：帧推送
memory_client.push_frame(user_id, timestamp, frame_bytes)  # 推帧给记忆服务
```

---

## 6. 关键参数

| 参数 | 值 | 说明 |
|------|-----|------|
| 客户端帧率 | 1 FPS | 每秒发 1 帧 |
| 帧分辨率 | 480p | 客户端压缩 |
| 帧大小 | < 60KB | JPEG quality 70 |
| 帧缓冲容量 | 5 帧 | 覆盖最近 5 秒 |
| 视觉模型 | qwen-vl-plus | HTTP API |
| 视觉超时 | 2s | 超时跳过 |
| 发帧数 | 最近 3 帧 | 多帧理解 |
| 视觉 max_tokens | 100 | 限制输出保证速度 |
| 触发条件 | 有帧数据时 | 无帧不调视觉模型 |

---

## 7. 修改清单

| 文件/组件 | 操作 | 说明 |
|-----------|------|------|
| `protocol.py` | 修改 | 新增 `VIDEO_FRAME = "video.frame"` |
| `gateway/gateway.py` | 修改 | 处理 video.frame 转发 |
| `gateway/runtime_manager.py` | 修改 | per-user FrameBuffer 管理 |
| `agent_runtime/frame_buffer.py` | 新建 | FrameBuffer 环形缓冲区 |
| `agent_runtime/vision_client.py` | 新建 | VisionClient (qwen-vl-plus) |
| `agent_runtime/anthropic_runtime.py` | 修改 | run_turn 中集成视觉调用 |
| `agent_runtime/memory_client.py` | 修改 | 新增 push_frame 方法 |
| `.env.example` | 修改 | 新增 VISION_MODEL、VISION_TIMEOUT 等配置 |

---

## 8. 性能预期

| 场景 | 延迟 |
|------|------|
| 纯文字（无视频） | 1-2s（不变） |
| 视觉理解 + 文字回复 | 2.5-3s（视觉 1.5s + 文字 1s） |
| 视觉 + 工具调用（搜索） | 5-6s（视觉 1.5s + 文字 1s + 搜索 3s） |
| 超时降级（视觉慢） | 2-3s（跳过视觉，纯文字） |

---

## 9. 未来扩展

1. **以图搜图 fallback**：视觉描述不精确时，调百度识图 API 精确匹配
2. **切换 Realtime**：替换 VisionClient 内部为 Qwen-Omni WebSocket 长会话（需解决音频保活问题）
3. **智能触发**：不是每条消息都调视觉，由文字模型/轻量分类器判断是否需要
4. **多模态记忆检索**：记忆服务支持"以图搜图"式检索（用当前帧匹配历史帧）

---

## 11. 记忆服务插件化设计

### 现有记忆服务接口

记忆服务是独立的外部 HTTP 服务，API 规格（v1）：

| 接口 | 方法 | 路径 | 用途 |
|------|------|------|------|
| Health | GET | `/v1/health` | 服务状态 |
| Upload Media | POST | `/v1/media/upload` | 上传视频/图片/音频，异步提取记忆 |
| Task Status | POST | `/v1/tasks_status` | 查询上传任务状态 |
| Query Memories | POST | `/v1/memories/query` | 检索记忆（文字+图片，支持 direct answer） |
| Add Session History | POST | `/v1/sessions/add_history` | 追加对话历史 |
| Get Session History | POST | `/v1/sessions/get_history` | 获取会话历史 |

### 视频数据进入记忆服务的路径

两条路径并行：

```
客户端推帧（1FPS）
    ↓
帧缓冲区（最近 5 帧）
    ├──→ VisionClient（实时理解，1-2s 响应）
    └──→ 视频片段录制器（每 30s 保存一个片段文件）
              ↓
         POST /v1/media/upload（异步）
              ↓
         记忆服务后台：keyframe extraction → embedding → memory 存储
```

- **实时帧**：给 VisionClient 用，不直接进记忆服务
- **视频片段**：定期（如每 30s）将帧合成为视频文件，通过 upload API 进入记忆服务

### 插件抽象接口

```python
# plugins/memory/base.py
from abc import ABC, abstractmethod
from typing import Any

class MemoryPlugin(ABC):
    """记忆服务插件接口，对齐 Memory Server API v1"""

    # --- 检索 ---
    @abstractmethod
    async def query_memories(
        self,
        user_id: str,
        query: str,
        session_id: str | None = None,
        top_k: int | None = None,
        direct_answer: bool = False,
        query_time: str | None = None,
        options: dict | None = None,
    ) -> dict:
        """对应 POST /v1/memories/query
        返回 {answer, results, total_results, ...}
        """

    @abstractmethod
    async def get_session_history(
        self,
        user_id: str,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """对应 POST /v1/sessions/get_history
        返回 {entries, total_entries, ...}
        """

    # --- 存储 ---
    @abstractmethod
    async def add_session_history(
        self,
        user_id: str,
        session_id: str,
        entries: list[dict],
    ) -> dict:
        """对应 POST /v1/sessions/add_history
        entries: [{role, content, timestamp?, metadata?}, ...]
        """

    @abstractmethod
    async def upload_media(
        self,
        user_id: str,
        session_id: str,
        files: list[dict],
    ) -> dict:
        """对应 POST /v1/media/upload
        files: [{file_id, file_url, filename, media_type, start_time, metadata?}, ...]
        返回 {task_id, status, accepted_count}
        """

    # --- 任务状态 ---
    async def get_task_status(self, user_id: str, task_id: str) -> dict:
        """对应 POST /v1/tasks_status"""
        return {"status": "not_implemented"}

    # --- 实时帧（可选扩展）---
    async def push_frame(self, user_id: str, timestamp: float, frame: bytes) -> None:
        """实时帧推送。当前不进记忆服务（帧只给 VisionClient 用）。
        未来如果记忆服务支持实时流接入，可在此实现。"""
        pass

    # --- 生命周期 ---
    async def close(self) -> None:
        """清理资源（HTTP session 等）"""
        pass
```

### 目录结构

```
plugins/
└── memory/
    ├── __init__.py           — 插件加载器（get_memory_plugin()）
    ├── base.py               — MemoryPlugin 抽象接口
    ├── local_json.py         — 本地 JSON 实现（开发/测试用）
    └── rest_api.py           — REST API 实现（对接 Memory Server）
```

### 加载机制

```python
# plugins/memory/__init__.py
import os
from .base import MemoryPlugin

_REGISTRY: dict[str, type[MemoryPlugin]] = {}

def register(name: str):
    """装饰器，注册插件实现"""
    def decorator(cls):
        _REGISTRY[name] = cls
        return cls
    return decorator

def get_memory_plugin() -> MemoryPlugin:
    plugin_name = os.environ.get("MEMORY_PLUGIN", "rest")
    # 延迟导入，触发注册
    from . import local_json, rest_api  # noqa: F401
    cls = _REGISTRY.get(plugin_name)
    if cls is None:
        raise ValueError(f"Unknown memory plugin: {plugin_name}. Available: {list(_REGISTRY)}")
    return cls()
```

### .env 配置

```bash
# 选择插件
MEMORY_PLUGIN=rest              # local / rest

# REST 实现配置
MEMORY_SERVICE_URL=http://localhost:9000
MEMORY_SERVICE_TIMEOUT_S=10

# 视频片段上传配置
MEMORY_VIDEO_SEGMENT_SECONDS=30    # 每 30s 一个片段
MEMORY_VIDEO_UPLOAD_ENABLED=true   # 是否启用自动上传
```

### REST 实现示例

```python
# plugins/memory/rest_api.py
@register("rest")
class RestMemoryPlugin(MemoryPlugin):
    def __init__(self):
        self._base_url = os.environ.get("MEMORY_SERVICE_URL", "http://localhost:9000")
        self._timeout = int(os.environ.get("MEMORY_SERVICE_TIMEOUT_S", "10"))
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        return self._session

    async def query_memories(self, user_id, query, **kwargs):
        session = await self._get_session()
        payload = {"user_id": user_id, "query": query, **kwargs}
        async with session.post(f"{self._base_url}/v1/memories/query", json=payload) as resp:
            return await resp.json()

    async def upload_media(self, user_id, session_id, files):
        session = await self._get_session()
        payload = {"user_id": user_id, "session_id": session_id, "files": files}
        async with session.post(f"{self._base_url}/v1/media/upload", json=payload) as resp:
            return await resp.json()

    # ... 其他方法类似
```

### 对接 AnthropicAgentRuntime

```python
# 改动前（当前）
from .memory_client import get_memory_client
mc = get_memory_client()
memories = await asyncio.to_thread(mc.recall, user_id, query=user_text)

# 改动后（插件化）
from plugins.memory import get_memory_plugin
mc = get_memory_plugin()
result = await mc.query_memories(user_id, query=user_text, top_k=10)
memories = result.get("results", [])
```

### 视频片段录制与上传

```python
# agent_runtime/video_recorder.py
class VideoSegmentRecorder:
    """从帧缓冲区定期生成视频片段并上传到记忆服务"""

    def __init__(self, memory_plugin: MemoryPlugin, segment_seconds: int = 30):
        self._plugin = memory_plugin
        self._segment_seconds = segment_seconds
        self._frames: list[tuple[float, bytes]] = []

    def add_frame(self, timestamp: float, frame: bytes):
        self._frames.append((timestamp, frame))
        # 检查是否达到片段时长
        if self._frames and (timestamp - self._frames[0][0]) >= self._segment_seconds:
            asyncio.create_task(self._flush_segment())

    async def _flush_segment(self):
        frames = self._frames
        self._frames = []
        # 合成视频文件（ffmpeg 或 opencv）
        video_path = await self._encode_video(frames)
        # 上传到记忆服务
        await self._plugin.upload_media(
            user_id=self._user_id,
            session_id=self._session_id,
            files=[{
                "file_id": f"segment-{int(frames[0][0])}",
                "file_url": f"file://{video_path}",
                "filename": f"segment_{int(frames[0][0])}.mp4",
                "media_type": "video",
                "start_time": datetime.fromtimestamp(frames[0][0]).isoformat(),
            }]
        )
```

### 会话建立时的记忆加载

```python
# RuntimeService 或 AnthropicAgentRuntime 中
async def _load_initial_context(self, user_id, session_id):
    mc = get_memory_plugin()
    
    # 获取用户近期历史（含视觉记忆）
    history = await mc.get_session_history(user_id, limit=20)
    
    # 查询用户近期关注的事物
    recent = await mc.query_memories(
        user_id=user_id,
        query="用户最近关注/看到的物品和事件",
        top_k=5,
        options={"strategy": "long_context"}
    )
    
    # 注入 system prompt
    # ...
```


| 测试 | 模型 | 结果 | 耗时 |
|------|------|------|------|
| 手机识别（商品图） | qwen-vl-plus | "Redmi 14R 5G" ✓ | 0.9s |
| 车型识别（实拍） | qwen-vl-plus | "大众Polo" ✓ | 0.9s |
| 车型识别（雪景实拍） | qwen-vl-plus | "奥迪A6" ✓ | 1.3s |
| 多帧理解（5帧） | qwen-vl-plus | 正确识别 ✓ | 1.8s |
| 视觉→文字工具调用 | qwen-vl + qwen-plus | search_product("Redmi 14R 5G") ✓ | 2.2s |
| 上下文记忆（帮我买之前的） | qwen-plus | add_to_cart("Redmi 14R 5G") ✓ | 1.1s |
