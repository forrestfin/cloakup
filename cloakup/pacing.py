"""Human-like timing: think pauses, reading dwell and an action-rate ceiling.

CloakBrowser's humanize layer already makes *each* action look human (Bezier
mouse paths, per-key timing, wheel-burst scrolling). What it can't know is the
rhythm *between* actions — an agent that fires a perfectly human click every
300 ms, or reads a 3,000-word page in 0.2 s, is still a bot. That is this
module's job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
from collections import deque

from .config import PacingConfig


def lognormal(median: float, sigma: float, lo: float, hi: float) -> float:
    """Right-skewed delay: mostly near the median, occasionally much longer —
    the shape human reaction and dwell times actually have."""
    return max(lo, min(hi, median * math.exp(random.gauss(0.0, sigma))))


def jitter(seconds: float, sigma: float = 0.3) -> float:
    """以配置时长为中位数取右偏随机值，避免调度按固定周期准点触发页面访问。"""
    return lognormal(seconds, sigma, seconds * 0.6, seconds * 1.8)


def stable_fraction(*key) -> float:
    """按键生成 [0,1) 内的稳定随机数：同一键每次相同，不同键互不相同。

    用于需要反复重算却不能每次重抽的场景（如某条消息的追聊时刻、某天某岗位的联系上限）。
    """
    digest = hashlib.sha256(json.dumps(key, ensure_ascii=False, default=str).encode()).digest()
    return int.from_bytes(digest[:8], 'big') / 2 ** 64


class Pacer:
    def __init__(self, cfg: PacingConfig) -> None:
        self.cfg = cfg
        self._last_action = 0.0
        self._recent: deque[float] = deque()

    async def before_action(self, kind: str) -> float:
        """Sleep so the gap since the previous action looks like human thinking.
        Returns the seconds actually slept."""
        c = self.cfg
        target = lognormal(c.think_median, 0.55, c.think_min, c.think_max)
        if kind in ("type", "select", "key"):
            target *= 0.6  # follow-up input in a form is quicker than deciding where to click
        now = time.monotonic()
        elapsed = now - self._last_action
        wait = max(0.0, target - elapsed, c.min_interval - elapsed)

        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= c.max_actions_per_minute:
            wait = max(wait, 60 - (now - self._recent[0]) + random.uniform(0.5, 3.0))

        if wait > 0:
            await asyncio.sleep(wait)
        return wait

    def mark_action(self) -> None:
        t = time.monotonic()
        self._last_action = t
        self._recent.append(t)

    def reading_time(self, visible_chars: int) -> float:
        c = self.cfg
        median = min(c.read_max, max(c.read_min, visible_chars / c.read_chars_per_sec))
        return lognormal(median, 0.35, c.read_min, c.read_max * 1.3)
