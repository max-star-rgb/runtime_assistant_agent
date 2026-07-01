#!/usr/bin/env python3
"""
双模型协作测试脚本：
1. 用 qwen-vl-plus 做视觉理解（图片→描述）
2. 用 dashscope SDK 测试 Qwen-Omni-Realtime WebSocket 接口
3. 把描述注入 qwen-plus 上下文，验证工具调用

用法：
  python scripts/test_vision_collab.py [图片路径]

需要：pip install dashscope>=1.25.17 aiohttp
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from openclaw_gateway_runtime.infra.dotenv import load_dotenv
load_dotenv()

API_KEY = os.environ.get("QWEN_API_KEY", "")
os.environ.setdefault("DASHSCOPE_API_KEY", API_KEY)
TEXT_MODEL = os.environ.get("QWEN_MODEL", "qwen-plus")
TEXT_BASE_URL = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic")


# ============================================================
# Part 1: Qwen-Omni-Realtime 视觉理解 (dashscope SDK)
# ============================================================

def query_omni_realtime(image_bytes: bytes, question: str) -> str:
    """用 dashscope SDK 连接 Qwen-Omni-Realtime，发送图片+问题"""
    try:
        from dashscope.audio.qwen_omni import (
            OmniRealtimeConversation,
            OmniRealtimeCallback,
            MultiModality,
        )
    except ImportError:
        print("[Omni] dashscope SDK 未安装或版本过低，跳过 Realtime 测试")
        return ""

    result_text = []
    done_event = threading.Event()
    error_msg = [None]

    class VisionCallback(OmniRealtimeCallback):
        def on_open(self):
            print("[Omni] WebSocket 连接已建立")

        def on_event(self, message: str):
            try:
                event = json.loads(message)
                event_type = event.get("type", "")
                if event_type == "response.text.delta":
                    result_text.append(event.get("delta", ""))
                elif event_type == "response.done":
                    done_event.set()
                elif event_type == "error":
                    error_msg[0] = json.dumps(event.get("error", event), ensure_ascii=False)
                    done_event.set()
            except Exception:
                pass

        def on_error(self, message):
            error_msg[0] = str(message)
            done_event.set()

        def on_close(self, status_code, msg):
            if not done_event.is_set():
                done_event.set()

    callback = VisionCallback()
    conversation = OmniRealtimeConversation(
        model="qwen-omni-turbo-latest",
        callback=callback,
    )

    print("[Omni] 正在连接...")
    conversation.connect()

    # 配置：只要文字输出
    conversation.update_session(
        output_modalities=[MultiModality.TEXT],
        instructions="你是视觉理解助手。简洁描述图片中的物品（名称、品牌、型号、颜色）。",
    )
    time.sleep(0.5)

    # 发送图片
    image_b64 = base64.b64encode(image_bytes).decode()
    print(f"[Omni] 发送图片 ({len(image_bytes)} bytes)...")
    conversation.append_video(image_b64)

    # 发送静音音频（Omni 需要至少一个音频帧）
    silence = b'\x00' * 3200
    conversation.append_audio(base64.b64encode(silence).decode())
    conversation.commit()

    # 触发响应
    print(f"[Omni] 发送问题: {question}")
    conversation.create_response(instructions=question)

    # 等待
    done_event.wait(timeout=15)
    conversation.close()

    if error_msg[0]:
        print(f"[Omni] 错误: {error_msg[0]}")
        return ""

    result = "".join(result_text)
    if result:
        print(f"[Omni] 回复: {result[:200]}")
    else:
        print("[Omni] 未收到回复")
    return result


# ============================================================
# Part 2: qwen-vl-plus 视觉理解 (HTTP API，作为 fallback)
# ============================================================

async def query_vl_model(image_bytes: bytes, question: str) -> str:
    """用 qwen-vl-plus (非 realtime) 做图片理解"""
    from openclaw_gateway_runtime.agent_runtime.anthropic_client import AnthropicClient, AnthropicConfig
    from openclaw_gateway_runtime.agent_runtime.anthropic_sse import AnthropicStreamBuilder

    cfg = AnthropicConfig(api_key=API_KEY, model='qwen-vl-plus', base_url=TEXT_BASE_URL, max_tokens=512, timeout_s=30)
    client = AnthropicClient(cfg)
    img_b64 = base64.b64encode(image_bytes).decode()

    payload = {
        'model': 'qwen-vl-plus',
        'max_tokens': 512,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': img_b64}},
                {'type': 'text', 'text': question}
            ]
        }],
    }

    builder = AnthropicStreamBuilder()
    async for ev in client.messages_create_stream(payload):
        builder.feed(ev)
    resp = builder.response
    return ''.join(b.get('text', '') for b in (resp.get('content') or []) if b.get('type') == 'text')


# ============================================================
# Part 3: qwen-plus 文字模型 + 工具调用
# ============================================================

async def query_text_model(user_text: str, vision_context: str | None) -> str:
    """文字模型带视觉上下文，验证工具调用"""
    from openclaw_gateway_runtime.agent_runtime.anthropic_client import AnthropicClient, AnthropicConfig
    from openclaw_gateway_runtime.agent_runtime.anthropic_sse import AnthropicStreamBuilder

    cfg = AnthropicConfig(api_key=API_KEY, model=TEXT_MODEL, base_url=TEXT_BASE_URL, max_tokens=512, timeout_s=30)
    client = AnthropicClient(cfg)

    enriched_text = f"{user_text}\n[视觉上下文] {vision_context}" if vision_context else user_text

    payload = {
        "model": TEXT_MODEL,
        "max_tokens": 512,
        "system": "你是购物助理。用户想搜索商品时，调用 search_product 工具。从视觉上下文中提取商品关键词。",
        "messages": [{"role": "user", "content": enriched_text}],
        "tools": [{
            "name": "search_product",
            "description": "搜索商品价格",
            "input_schema": {
                "type": "object",
                "properties": {"keyword": {"type": "string", "description": "搜索关键词"}},
                "required": ["keyword"],
            }
        }],
    }

    print(f"\n[Text] 调用 {TEXT_MODEL}...")
    builder = AnthropicStreamBuilder()
    async for ev in client.messages_create_stream(payload):
        builder.feed(ev)

    resp = builder.response
    for block in resp.get("content", []):
        if block.get("type") == "text":
            print(f"[Text] 回复: {block['text']}")
        elif block.get("type") == "tool_use":
            print(f"[Text] 工具调用: {block['name']}({block['input']})")

    return ""


# ============================================================
# Main
# ============================================================

async def main():
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    image_path = sys.argv[1] if len(sys.argv) > 1 else None

    if image_path:
        image_bytes = Path(image_path).read_bytes()
        print(f"使用本地图片: {image_path} ({len(image_bytes)} bytes)")
    else:
        import aiohttp
        test_url = "https://img.alicdn.com/bao/uploaded/i4/2127574297/O1CN01npD0op1hc4zHQQ0pQ_!!2127574297.jpg"
        print("下载测试图片...")
        async with aiohttp.ClientSession() as session:
            async with session.get(test_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    print(f"下载成功: {len(image_bytes)} bytes")
                else:
                    print(f"下载失败: {resp.status}")
                    return

    # === Test A: Qwen-Omni Realtime ===
    print("\n" + "=" * 60)
    print("Test A: Qwen-Omni-Realtime (WebSocket 长会话)")
    print("=" * 60)
    t0 = time.time()
    omni_desc = query_omni_realtime(image_bytes, "这是什么东西？描述物品名称、品牌、型号。")
    t1 = time.time()
    print(f"[耗时] Omni Realtime: {t1-t0:.1f}s")

    # === Test B: qwen-vl-plus (HTTP, fallback) ===
    print("\n" + "=" * 60)
    print("Test B: qwen-vl-plus (HTTP API)")
    print("=" * 60)
    t2 = time.time()
    vl_desc = await query_vl_model(image_bytes, "这是什么东西？描述物品名称、品牌、型号。")
    t3 = time.time()
    print(f"[VL] 回复: {vl_desc[:150]}")
    print(f"[耗时] qwen-vl-plus: {t3-t2:.1f}s")

    # 选择可用的视觉描述
    vision_desc = omni_desc or vl_desc
    if not vision_desc:
        print("\n两个视觉模型都失败了！")
        return

    # === Test C: 文字模型 + 工具调用 ===
    print("\n" + "=" * 60)
    print("Test C: qwen-plus 带视觉上下文 → 工具调用")
    print("=" * 60)
    t4 = time.time()
    await query_text_model("帮我搜下同款", vision_desc)
    t5 = time.time()
    print(f"[耗时] 文字模型: {t5-t4:.1f}s")

    # === 总结 ===
    print("\n" + "=" * 60)
    print("总结")
    print("=" * 60)
    print(f"  Omni Realtime: {t1-t0:.1f}s {'✓' if omni_desc else '✗'}")
    print(f"  qwen-vl-plus:  {t3-t2:.1f}s {'✓' if vl_desc else '✗'}")
    print(f"  文字+工具:     {t5-t4:.1f}s ✓")
    best_vision = t1 - t0 if omni_desc else t3 - t2
    print(f"  总延迟（视觉+文字）: {best_vision + (t5-t4):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
