from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import websockets


def _frame(*, type: str, session_id: str | None = None, run_id: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    f: dict[str, Any] = {"v": 1, "type": type}
    if session_id is not None:
        f["session_id"] = session_id
    if run_id is not None:
        f["run_id"] = run_id
    if payload is not None:
        f["payload"] = payload
    return f


async def run_demo(*, url: str, session_id: str, cancel_after_ms: int | None) -> int:
    tool_exec_seen = False
    tool_read_seen = False
    text_buf: list[str] = []

    run_id: str | None = None
    started_at = time.time()
    cancelled = False

    prompt = (
        "请使用 available_skills 里的 example skill。\n"
        "要求：先 read_file 读取 skills/example/SKILL.md，然后 exec 运行脚本，最后把脚本 stdout 原样放进最终回复。\n"
        "不要编造脚本输出。"
    )

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps(_frame(type="message.user", session_id=session_id, payload={"text": prompt})))

        while True:
            if cancel_after_ms is not None and run_id and not cancelled:
                if (time.time() - started_at) * 1000 >= cancel_after_ms:
                    await ws.send(json.dumps(_frame(type="run.cancel", session_id=session_id, run_id=run_id)))
                    cancelled = True

            raw = await ws.recv()
            obj = json.loads(raw)
            t = obj.get("type")

            if t == "run.started":
                run_id = obj.get("run_id")
                continue

            if t == "event.tool":
                name = str((obj.get("payload") or {}).get("name") or "")
                if name == "exec":
                    tool_exec_seen = True
                if name == "read_file":
                    tool_read_seen = True
                continue

            if t == "stream.chunk":
                txt = str((obj.get("payload") or {}).get("text") or "")
                if txt:
                    text_buf.append(txt)
                continue

            if t == "run.end":
                reason = obj.get("reason")
                final_text = "".join(text_buf).strip()
                print(f"reason={reason}")
                print("---- text ----")
                print(final_text)
                print("--------------")
                print(f"tool_read_file_seen={tool_read_seen}")
                print(f"tool_exec_seen={tool_exec_seen}")

                if cancel_after_ms is not None:
                    return 0 if reason == "cancelled" else 2

                if reason != "completed":
                    return 2
                if not tool_exec_seen:
                    return 3
                if "hello" not in final_text.lower():
                    return 4
                return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765", help="Gateway WS URL")
    ap.add_argument("--session-id", default="e4", help="Session id")
    ap.add_argument("--cancel-after-ms", type=int, default=None, help="If set, cancel run after N ms and expect reason=cancelled")
    args = ap.parse_args()

    try:
        return asyncio.run(run_demo(url=args.url, session_id=args.session_id, cancel_after_ms=args.cancel_after_ms))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

