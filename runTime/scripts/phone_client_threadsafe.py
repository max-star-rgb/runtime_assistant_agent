#!/usr/bin/env python3
"""
电话平台 WebSocket 客户端 — 兼容多线程服务器架构。

解决问题：
  WebSocket 连接在线程 A 创建，recv() 在线程 B 调用 →
  "The future belongs to a different loop" 错误。

方案：
  用一个专属后台线程跑 WebSocket event loop，所有 WS 操作都路由到该线程。
  外部（任意线程）通过 thread-safe 的同步方法调用。

用法：
  client = PhoneClient("ws://127.0.0.1:8765")
  client.connect("13800138000")

  for msg in client.send_message("你好"):
      if msg["type"] == "stream.chunk":
          print(msg["text"], end="")
      elif msg["type"] == "run.end":
          print(f"\\n结束, expects_reply={msg['expects_reply']}")

  client.hangup()
  client.close()
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from typing import Any, Iterator


class PhoneClient:
    """线程安全的电话平台 WebSocket 客户端。

    内部维护一个专属 event loop 线程，所有 WebSocket 操作
    都在该线程中执行，避免跨线程 loop 不匹配。

    使用单一 reader 协程持续读取 WebSocket 消息，避免并发 recv。
    """

    def __init__(self, gateway_url: str = "ws://127.0.0.1:8765"):
        self.gateway_url = gateway_url
        self.session_id: str | None = None
        self.user_id: str | None = None

        # 专属 event loop + 后台线程
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._ws: Any = None
        # 单一 reader 协程将所有收到的消息放入此 queue
        self._inbox: asyncio.Queue[dict | None] = asyncio.Queue()
        self._reader_task: Any = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro: Any, timeout: float = 30) -> Any:
        """在专属 loop 中执行协程，阻塞等待结果。线程安全。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ------------------------------------------------------------------
    # 1. 连接
    # ------------------------------------------------------------------
    def connect(self, phone_number: str, *, timeout: float = 10) -> str:
        """建立连接并发起来电，返回 session_id。"""
        return self._run_coro(self._async_connect(phone_number, timeout=timeout), timeout=timeout + 5)

    async def _async_connect(self, phone_number: str, *, timeout: float = 10) -> str:
        import websockets

        self.user_id = phone_number
        self._inbox = asyncio.Queue()
        self._ws = await websockets.connect(self.gateway_url)

        await self._ws.send(json.dumps({
            "v": 1,
            "type": "call.incoming",
            "user_id": phone_number,
            "payload": {"language": "zh-CN"},
        }))

        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        data = json.loads(raw)
        if data.get("type") != "call.ready":
            raise RuntimeError(f"Expected call.ready, got: {data.get('type')}")

        self.session_id = data["session_id"]
        # Start the single reader coroutine
        self._reader_task = asyncio.ensure_future(self._reader_loop())
        return self.session_id

    async def _reader_loop(self) -> None:
        """Single reader coroutine — only this coroutine calls ws.recv()."""
        try:
            while True:
                raw = await self._ws.recv()
                data = json.loads(raw)
                await self._inbox.put(data)
        except Exception:
            # Connection closed or error — signal end
            await self._inbox.put(None)

    # ------------------------------------------------------------------
    # 2. 发送消息 — 同步迭代器（任意线程可调用）
    # ------------------------------------------------------------------
    def send_message(self, text: str) -> Iterator[dict]:
        """发送消息，返回同步迭代器，yield 流式响应帧。

        可在任意线程中调用：
            for msg in client.send_message("你好"):
                if msg["type"] == "stream.chunk":
                    print(msg["text"])
        """
        q: queue.Queue[dict | None] = queue.Queue()
        # 在专属 loop 中启动异步生产者
        asyncio.run_coroutine_threadsafe(
            self._async_produce(text, q), self._loop
        )
        # 同步消费
        while True:
            msg = q.get()
            if msg is None:
                break
            yield msg

    async def _async_produce(self, text: str, q: queue.Queue) -> None:
        """异步发送消息并将响应帧放入队列。从 _inbox 读取（不直接 recv）。"""
        try:
            await self._ws.send(json.dumps({
                "v": 1,
                "type": "message.user",
                "user_id": self.user_id,
                "session_id": self.session_id,
                "payload": {"text": text, "modality": "text"},
            }))

            while True:
                data = await self._inbox.get()
                if data is None:
                    q.put({"type": "error", "error": {"code": "connection_closed", "message": "WebSocket closed"}})
                    break

                frame_type = data.get("type", "")

                msg: dict[str, Any] = {
                    "type": frame_type,
                    "run_id": data.get("run_id"),
                    "session_id": data.get("session_id"),
                    "user_id": data.get("user_id"),
                    "payload": data.get("payload", {}),
                }

                if frame_type == "stream.chunk":
                    msg["text"] = (data.get("payload") or {}).get("text", "")
                    msg["display_only"] = (data.get("payload") or {}).get("display_only", False)
                    msg["content_type"] = (data.get("payload") or {}).get("content_type", "text")

                elif frame_type == "run.end":
                    payload = data.get("payload") or {}
                    msg["reason"] = data.get("reason", "completed")
                    msg["expects_reply"] = payload.get("expects_reply", True)
                    q.put(msg)
                    break

                elif frame_type == "error":
                    msg["error"] = data.get("error", {})
                    q.put(msg)
                    break

                elif frame_type == "run.started":
                    # Skip run.started for cancelled runs being replaced
                    pass

                q.put(msg)
        except Exception as e:
            q.put({"type": "error", "error": {"code": "client_error", "message": str(e)}})
        finally:
            q.put(None)  # 结束信号

    # ------------------------------------------------------------------
    # 2b. 发送消息 — 异步版本（如果调用方本身就在 async 上下文中）
    # ------------------------------------------------------------------
    async def async_send_message(self, text: str):
        """异步版本，用 async for 消费。

        必须在 PhoneClient 的专属 loop 中调用（或通过 run_coroutine_threadsafe）。
        """
        await self._ws.send(json.dumps({
            "v": 1,
            "type": "message.user",
            "user_id": self.user_id,
            "session_id": self.session_id,
            "payload": {"text": text, "modality": "text"},
        }))

        while True:
            data = await self._inbox.get()
            if data is None:
                yield {"type": "error", "error": {"code": "connection_closed", "message": "WebSocket closed"}}
                break

            frame_type = data.get("type", "")

            msg: dict[str, Any] = {
                "type": frame_type,
                "run_id": data.get("run_id"),
                "session_id": data.get("session_id"),
                "user_id": data.get("user_id"),
                "payload": data.get("payload", {}),
            }

            if frame_type == "stream.chunk":
                msg["text"] = (data.get("payload") or {}).get("text", "")
                msg["display_only"] = (data.get("payload") or {}).get("display_only", False)
                msg["content_type"] = (data.get("payload") or {}).get("content_type", "text")
            elif frame_type == "run.end":
                payload = data.get("payload") or {}
                msg["reason"] = data.get("reason", "completed")
                msg["expects_reply"] = payload.get("expects_reply", True)
                yield msg
                break
            elif frame_type == "error":
                msg["error"] = data.get("error", {})
                yield msg
                break

            yield msg

    # ------------------------------------------------------------------
    # 3. 挂断
    # ------------------------------------------------------------------
    def hangup(self) -> None:
        """挂断通话。"""
        if self._ws and self.session_id:
            self._run_coro(self._async_hangup(), timeout=10)

    async def _async_hangup(self) -> None:
        await self._ws.send(json.dumps({
            "v": 1,
            "type": "call.hangup",
            "user_id": self.user_id,
            "session_id": self.session_id,
        }))
        # Wait for hangup ack from inbox
        try:
            data = await asyncio.wait_for(self._inbox.get(), timeout=5)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    # 4. 关闭
    # ------------------------------------------------------------------
    def close(self) -> None:
        """关闭连接和后台线程。"""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            self._reader_task = None
        if self._ws:
            try:
                self._run_coro(self._ws.close(), timeout=5)
            except Exception:
                pass
            self._ws = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


# ======================================================================
# 测试
# ======================================================================

def main() -> None:
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765"
    phone = sys.argv[2] if len(sys.argv) > 2 else "13800138000"

    client = PhoneClient(url)

    try:
        session_id = client.connect(phone)
        print(f"[通话接通] session={session_id[:8]}  phone={phone}\n")

        for text in ["你好", "帮我搜下华为Mate70Pro", "谢谢"]:
            print(f">>> {text}")
            for msg in client.send_message(text):
                if msg["type"] == "stream.chunk":
                    print(msg.get("text", ""), end="", flush=True)
                elif msg["type"] == "run.end":
                    print(f"\n[run.end] reason={msg['reason']} expects_reply={msg['expects_reply']}\n")
                elif msg["type"] == "error":
                    print(f"\n[error] {msg.get('error')}\n")

        client.hangup()
        print("[通话结束]")
    finally:
        client.close()


if __name__ == "__main__":
    main()
