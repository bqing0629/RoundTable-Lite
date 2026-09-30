"""WebSocket 广播总线（需求 5.2）。每个会议一个房间，控制类事件全房间广播。"""
from __future__ import annotations

import asyncio

from fastapi import WebSocket


class Bus:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def join(self, meeting_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._rooms.setdefault(meeting_id, set()).add(ws)

    async def leave(self, meeting_id: str, ws: WebSocket) -> None:
        async with self._lock:
            s = self._rooms.get(meeting_id)
            if s is not None:
                s.discard(ws)
                if not s:
                    self._rooms.pop(meeting_id, None)

    def room_size(self, meeting_id: str) -> int:
        return len(self._rooms.get(meeting_id, ()))

    async def broadcast(self, meeting_id: str, event: str, payload: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(meeting_id, ())):
            try:
                await ws.send_json({"event": event, "data": payload})
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.leave(meeting_id, ws)

    async def broadcast_all(self, event: str, payload: dict) -> None:
        for mid in list(self._rooms):
            await self.broadcast(mid, event, payload)


bus = Bus()
