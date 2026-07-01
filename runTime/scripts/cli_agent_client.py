#!/usr/bin/env python3
"""
简单命令行客户端：连接 Gateway WebSocket，多轮对话、流式输出，便于验证 agent 与 skills。

用法（先启动 Runtime + Gateway）:
  python scripts/cli_agent_client.py
  python scripts/cli_agent_client.py --url ws://127.0.0.1:8765
  python scripts/cli_agent_client.py -v   # 调试：打印 run.end 详情与未知帧

命令:
  /help          显示帮助
  /quit /q       退出
  /cancel        取消当前 run（需本轮已出现 run.started）
  /session ID    切换会话（后续消息使用该 session_id）
  /new           使用新的随机 session_id
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

import websockets

# 允许在未 pip install -e . 时从仓库根运行：把 src 加入 path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from openclaw_gateway_runtime.infra import load_dotenv  # noqa: E402
from openclaw_gateway_runtime.protocol import frame  # noqa: E402


def _print_tool(payload: Any) -> None:
    p = payload or {}
    name = p.get("name")
    phase = p.get("phase")
    extra = {k: v for k, v in p.items() if k not in ("name", "phase")}
    line = f"[tool] {name}"
    if phase:
        line += f" phase={phase}"
    if extra:
        line += f" {json.dumps(extra, ensure_ascii=False, default=str)[:500]}"
    print(f"\n{line}", flush=True)


async def _input_line(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def _recv_until_run_end(
    ws: Any, *, last_run_id: dict[str, str | None], verbose: bool = False
) -> None:
    """Drain frames until run.end; print stream chunks and tool lines."""
    while True:
        raw = await ws.recv()
        obj = json.loads(raw)
        t = obj.get("type")
        if t == "run.started":
            last_run_id["id"] = str(obj.get("run_id") or "")
        elif t == "stream.chunk":
            payload = obj.get("payload") or {}
            txt = str(payload.get("text") or "")
            display_only = payload.get("display_only", False)
            if display_only:
                # Gray color for display-only content (not sent to TTS)
                print(f"\033[90m{txt}\033[0m", end="", flush=True)
            else:
                print(txt, end="", flush=True)
        elif t == "event.tool":
            _print_tool(obj.get("payload"))
        elif t == "run.end":
            reason = obj.get("reason")
            err = obj.get("error")
            payload = obj.get("payload") or {}
            expects_reply = payload.get("expects_reply", True)
            if verbose:
                print(f"\n--- run.end reason={reason!r} expects_reply={expects_reply} ---", flush=True)
            elif reason == "error" or err:
                print(f"\n[run.end] reason={reason!r}", flush=True)
            else:
                print(flush=True)
            if not expects_reply:
                print("[expects_reply=false] 本轮无需用户回复", flush=True)
            if err:
                print(f"error: {err}", flush=True)
            return
        elif t == "error":
            print(f"\n[frame error] {obj.get('error')}", flush=True)
        elif t in ("pong",):
            pass
        else:
            if verbose:
                print(f"\n[{t}] {json.dumps(obj, ensure_ascii=False, default=str)[:200]}", flush=True)


async def main_async(*, url: str, session_id: str, verbose: bool) -> int:
    load_dotenv()
    last_run_id: dict[str, str | None] = {"id": None}

    print(f"连接 {url!r}  session_id={session_id!r}", flush=True)
    print("输入内容后回车发送；/help 查看命令。\n", flush=True)

    async with websockets.connect(url) as ws:
        while True:
            line = (await _input_line("你> ")).rstrip("\n")
            if not line:
                continue
            if line in {"/q", "/quit"}:
                print("再见。", flush=True)
                return 0
            if line == "/help":
                print(__doc__ or "", flush=True)
                continue
            if line == "/new":
                session_id = str(uuid.uuid4())
                print(f"已切换 session_id={session_id}", flush=True)
                continue
            if line.startswith("/session "):
                session_id = line.split(maxsplit=1)[1].strip() or session_id
                print(f"已切换 session_id={session_id}", flush=True)
                continue
            if line == "/cancel":
                rid = last_run_id.get("id")
                if not rid:
                    print("当前没有可用的 run_id，请先发起一轮对话。", flush=True)
                    continue
                await ws.send(
                    json.dumps(
                        frame(
                            type="run.cancel",
                            session_id=session_id,
                            run_id=rid,
                        )
                    )
                )
                if verbose:
                    print("(等待 run.end …)", flush=True)
                await _recv_until_run_end(ws, last_run_id=last_run_id, verbose=verbose)
                continue

            await ws.send(
                json.dumps(
                    frame(
                        type="message.user",
                        session_id=session_id,
                        payload={"text": line},
                    )
                )
            )
            await _recv_until_run_end(ws, last_run_id=last_run_id, verbose=verbose)

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Gateway WebSocket 交互式测试客户端")
    ap.add_argument(
        "--url",
        default=os.environ.get("GATEWAY_WS_URL", "ws://127.0.0.1:8765"),
        help="Gateway WebSocket 地址（也可用环境变量 GATEWAY_WS_URL）",
    )
    ap.add_argument(
        "--session-id",
        default=os.environ.get("CLI_SESSION_ID", "cli"),
        help="会话 ID，多轮对话保持不变（也可用 CLI_SESSION_ID）",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印 run.end 横幅、未知帧类型等调试信息",
    )
    args = ap.parse_args()
    try:
        return asyncio.run(
            main_async(url=args.url, session_id=args.session_id, verbose=args.verbose)
        )
    except KeyboardInterrupt:
        print("\n已中断。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
