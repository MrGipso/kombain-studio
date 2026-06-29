from __future__ import annotations

import asyncio
import json
import time
from typing import Any


class SSEManager:
    """Manages SSE event streams for long-running pipeline operations."""

    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
        self._subs: dict[str, list[str]] = {}

    def subscribe(self, project_id: str) -> tuple[str, asyncio.Queue]:
        sub_id = f"{project_id}_{int(time.time() * 1000)}"
        q: asyncio.Queue = asyncio.Queue()
        self._queues[sub_id] = q
        self._subs.setdefault(project_id, []).append(sub_id)
        q.put_nowait({"event": "connected", "data": {"sub_id": sub_id}})
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        self._queues.pop(sub_id, None)
        for pid in list(self._subs.keys()):
            if sub_id in self._subs[pid]:
                self._subs[pid].remove(sub_id)
                if not self._subs[pid]:
                    del self._subs[pid]
                break

    def emit(self, project_id: str, event: str, data: dict[str, Any]):
        payload = {"event": event, "data": data}
        for sub_id in self._subs.get(project_id, []):
            q = self._queues.get(sub_id)
            if q:
                q.put_nowait(payload)

    async def emit_async(self, project_id: str, event: str, data: dict[str, Any]):
        payload = {"event": event, "data": data}
        for sub_id in self._subs.get(project_id, []):
            q = self._queues.get(sub_id)
            if q:
                await q.put(payload)

    async def generator(self, sub_id: str):
        q = self._queues.get(sub_id)
        if not q:
            return
        try:
            msg = await q.get()
            while msg.get("type") != "disconnect":
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                msg = await q.get()
        except asyncio.CancelledError:
            pass
        finally:
            self.unsubscribe(sub_id)
