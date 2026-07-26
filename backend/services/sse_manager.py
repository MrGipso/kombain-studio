from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any


class SSEManager:
    """Manages SSE event streams for long-running pipeline operations."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}

    def subscribe(self, project_id: str) -> tuple[str, asyncio.Queue]:
        sub_id = f"{project_id}_{uuid.uuid4().hex}"
        q: asyncio.Queue = asyncio.Queue()
        self._queues[sub_id] = q
        q.put_nowait({"event": "connected", "data": {"sub_id": sub_id}})
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        self._queues.pop(sub_id, None)

    def emit_to(self, sub_id: str, event: str, data: dict[str, Any]) -> bool:
        """Emit an event only to the stream that started a particular run."""
        q = self._queues.get(sub_id)
        if q is None:
            return False
        q.put_nowait({"event": event, "data": data})
        return True

    async def generator(self, sub_id: str):
        q = self._queues.get(sub_id)
        if not q:
            return
        try:
            while True:
                msg = await q.get()
                if msg.get("type") == "disconnect":
                    break
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                # Terminal events close the stream so the connection does not
                # hang open forever (which previously caused ConnectionResetError
                # spam when the client navigated away mid-stream).
                if msg.get("event") in ("done", "error"):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(sub_id)
