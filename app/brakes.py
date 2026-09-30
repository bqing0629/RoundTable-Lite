"""两道刹车之一：token bucket（需求 9）。

- 每发送者容量 10 个令牌，每 3 秒补 1 个，纯内存、不持久化；
- **按最终作者身份计**：Hub 代写 CLI 回复时，以该 agent 的 participant_id 过桶；
- 主席"我"同样受限（防手抖刷屏），但主席的 stop/reset 等控制操作永不受限。
"""
from __future__ import annotations

import time

CAPACITY = 10
REFILL_SECONDS = 3.0


class _Bucket:
    __slots__ = ("tokens", "ts")

    def __init__(self) -> None:
        self.tokens = float(CAPACITY)
        self.ts = time.monotonic()


class Brakes:
    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, sender_id: str) -> tuple[bool, int]:
        """取一个令牌。返回 (ok, retry_after_seconds)。"""
        now = time.monotonic()
        b = self._buckets.get(sender_id)
        if b is None:
            b = self._buckets[sender_id] = _Bucket()
        else:
            refill = int((now - b.ts) / REFILL_SECONDS)
            if refill > 0:
                b.tokens = min(float(CAPACITY), b.tokens + refill)
                b.ts = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True, 0
        wait = REFILL_SECONDS - (now - b.ts)
        return False, max(1, int(wait) + 1)


brakes = Brakes()
