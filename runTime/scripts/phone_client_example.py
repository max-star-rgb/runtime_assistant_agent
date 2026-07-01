#!/usr/bin/env python3
"""
电话平台对接客户端示例

演示如何与 Gateway（或 mock_gateway.py）对接：
  1. connect()     — 建立 WebSocket 连接 + call.incoming → call.ready
  2. send_message() — 发送用户消息，async for 接收流式响应
  3. hangup()      — 挂断通话

用法：
  python scripts/phone_client_example.py
  python scripts/phone_client_example.py --url ws://10.0.0.1:8765 --phone 13800138000

关键点：
  - 所有操作必须在同一个 asyncio event loop 中
  - WebSocket 连接在 connect() 中创建，后续 send/recv 复用同一个连接
  - send_message() 是 async generator，用 async for 消费
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any, AsyncIterator

import websockets


class PhoneClient:
    """电话平台 WebSocket 客户端。"""

    def __init__(self, gateway_url: str = "ws://127.0.0.1:8765"):
        self.gateway_url = gateway_url
        self._ws: Any = None
        self.session_id: str | None = None
        self.user_id: str | None = None

    # ------------------------------------------------------------------
    # 1. 连接 + 来电
    # ------------------------------------------------------------------
    async def connect(self, phone_number: str, *, timeout: float = 10) -> str:
        """建立连接并发起来电，返回 session_id。

        时序: connect → call.incoming → 等待 call.ready
        """
        self.user_id = phone_number
        self._ws = await websockets.connect(self.gateway_url)

        # 发送 call.incoming
        await self._ws.send(json.dumps({
            "v": 1,
            "type": "call.incoming",
            "user_id": phone_number,
            "payload": {"language": "zh-CN"},
        }))

        # 等待 call.ready
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        data = json.loads(raw)
        if data.get("type") != "call.ready":
            raise RuntimeError(f"Expected call.ready, got: {data.get('type')}")

        self.session_id = data["session_id"]
        return self.session_id

    # ------------------------------------------------------------------
    # 2. 发送消息 + 流式接收
    # ------------------------------------------------------------------
    async def send_message(self, text: str) -> AsyncIterator[dict]:
        """发送用户消息，yield 流式响应帧。

        时序: message.user → run.started → stream.chunk × N → run.end

        Yields:
            dict with keys:
              - type: "run.started" | "stream.chunk" | "run.end"
              - text: 文本片段（仅 stream.chunk）
              - reason: 结束原因（仅 run.end）
              - expects_reply: 是否等待用户回复（仅 run.end）
              - run_id, session_id, user_id, payload: 原始字段
        """
        if not self._ws or not self.session_id:
            raise RuntimeError("Not connected. Call connect() first.")

        await self._ws.send(json.dumps({
            "v": 1,
            "type": "message.user",
            "user_id": self.user_id,
            "session_id": self.session_id,
            "payload": {"text": text, "modality": "text"},
        }))

        while True:
            raw = await self._ws.recv()
            data = json.loads(raw)
            frame_type = data.get("type", "")

            msg = {
                "type": frame_type,
                "run_id": data.get("run_id"),
                "session_id": data.get("session_id"),
                "user_id": data.get("user_id"),
                "payload": data.get("payload", {}),
            }

            if frame_type == "stream.chunk":
                msg["text"] = (data.get("payload") or {}).get("text", "")

            elif frame_type == "run.end":
                payload = data.get("payload") or {}
                msg["reason"] = data.get("reason", "completed")
                msg["expects_reply"] = payload.get("expects_reply", True)
                yield msg
                break  # 本轮结束

            elif frame_type == "error":
                msg["error"] = data.get("error", {})
                yield msg
                break

            yield msg

    # ------------------------------------------------------------------
    # 3. 挂断
    # ------------------------------------------------------------------
    async def hangup(self, *, timeout: float = 5) -> None:
        """挂断通话，等待 hangup_ack。"""
        if not self._ws:
            return

        await self._ws.send(json.dumps({
            "v": 1,
            "type": "call.hangup",
            "user_id": self.user_id,
            "session_id": self.session_id,
        }))

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("type") == "call.hangup_ack":
                pass  # OK
        except asyncio.TimeoutError:
            pass  # best-effort

        await self._ws.close()
        self._ws = None

    # ------------------------------------------------------------------
    # 4. 取消当前生成
    # ------------------------------------------------------------------
    async def cancel(self) -> None:
        """取消当前正在生成的回复。"""
        if not self._ws or not self.session_id:
            return
        await self._ws.send(json.dumps({
            "v": 1,
            "type": "run.cancel",
            "user_id": self.user_id,
            "session_id": self.session_id,
        }))


# ======================================================================
# 使用示例
# ======================================================================

async def demo(url: str, phone: str) -> None:
    client = PhoneClient(url)

    # 1. 连接
    session_id = await client.connect(phone)
    print(f"[通话接通] session={session_id[:8]}  phone={phone}\n")

    # 2. 多轮对话
    prompts = [
        "你好",
        "帮我搜下华为Mate70Pro手机",
        "谢谢",
    ]

    for text in prompts:
        print(f">>> {text}")
        full_reply = []

        async for msg in client.send_message(text):
            if msg["type"] == "run.started":
                pass  # 可选：显示加载状态
            elif msg["type"] == "stream.chunk":
                chunk = msg["text"]
                full_reply.append(chunk)
                print(chunk, end="", flush=True)
            elif msg["type"] == "run.end":
                print(f"\n[run.end] reason={msg['reason']} expects_reply={msg['expects_reply']}\n")
                if not msg["expects_reply"]:
                    print("[对话结束，无需继续等待用户输入]")
            elif msg["type"] == "error":
                print(f"\n[error] {msg.get('error')}\n")

    # 3. 挂断
    await client.hangup()
    print("[通话结束]")


def main() -> None:
    parser = argparse.ArgumentParser(description="电话平台对接客户端示例")
    parser.add_argument("--url", default="ws://127.0.0.1:8765", help="Gateway WebSocket 地址")
    parser.add_argument("--phone", default="13800138000", help="用户电话号码")
    args = parser.parse_args()

    # 关键：所有操作在同一个 asyncio.run() 中
    asyncio.run(demo(args.url, args.phone))


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
