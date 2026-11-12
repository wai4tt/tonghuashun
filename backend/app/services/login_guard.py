"""登录失败限流：抑制口令爆破，同时不误伤正常用户。

口径：

* 只在"同一账号 + 同一来源"连续失败时计数，成功登录立刻清零；
* 阈值与锁定时长可通过环境变量调整，默认 5 次失败 / 10 分钟窗口 / 60 秒锁定；
* 只存在进程内存里，多实例部署时应换成 Redis 等共享存储（本模块不假装是分布式限流）。

被锁定时抛出的 429 语义与"服务过载"不同，因此单独给出 ``Retry-After``。
"""

from __future__ import annotations

import math
import os
import threading
from time import monotonic
from typing import Any, Callable


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class LoginThrottle:
    """按 (username, client) 维度统计失败次数并给出锁定剩余秒数。"""

    def __init__(
        self,
        *,
        max_failures: int | None = None,
        window_seconds: float | None = None,
        lockout_seconds: float | None = None,
        max_entries: int = 4096,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        failures = max_failures if max_failures is not None else int(
            _env_float("WENCE_LOGIN_MAX_FAILURES", 5))
        self.max_failures = max(1, int(failures))
        self.window_seconds = window_seconds if window_seconds is not None else _env_float(
            "WENCE_LOGIN_WINDOW_SECONDS", 600.0)
        self.lockout_seconds = lockout_seconds if lockout_seconds is not None else _env_float(
            "WENCE_LOGIN_LOCKOUT_SECONDS", 60.0)
        self.max_entries = max(64, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self.locked_count = 0

    @staticmethod
    def key(username: str, client: str | None) -> str:
        return f"{(username or '').strip().casefold()}|{(client or 'unknown').strip()}"

    def _purge(self, now: float) -> None:
        expired = []
        for key, entry in self._entries.items():
            failures = [stamp for stamp in entry["failures"] if now - stamp <= self.window_seconds]
            entry["failures"] = failures
            if not failures and entry["locked_until"] <= now:
                expired.append(key)
        for key in expired:
            self._entries.pop(key, None)
        if len(self._entries) > self.max_entries:
            # 只保留最近活跃的条目，防止被随机用户名撑爆内存。
            ordered = sorted(self._entries.items(), key=lambda item: item[1]["last_seen"], reverse=True)
            for key, _ in ordered[self.max_entries:]:
                self._entries.pop(key, None)

    def retry_after(self, key: str) -> int:
        """允许登录返回 0，否则返回建议的 Retry-After 秒数。"""

        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return 0
            remaining = entry["locked_until"] - now
            if remaining > 0:
                return max(1, math.ceil(remaining))
            return 0

    def failure(self, key: str) -> int:
        """记录一次失败，返回锁定剩余秒数（0 表示尚未锁定）。"""

        now = self._clock()
        with self._lock:
            self._purge(now)
            entry = self._entries.get(key)
            if entry is None:
                entry = {"failures": [], "locked_until": 0.0, "last_seen": now, "lock_count": 0}
                self._entries[key] = entry
            entry["failures"].append(now)
            entry["last_seen"] = now
            if len(entry["failures"]) >= self.max_failures:
                entry["locked_until"] = now + self.lockout_seconds
                entry["lock_count"] += 1
                entry["failures"] = []
                self.locked_count += 1
                return max(1, math.ceil(self.lockout_seconds))
            return 0

    def success(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._purge(now)
            tracked = len(self._entries)
            locked = sum(1 for entry in self._entries.values() if entry["locked_until"] > now)
        return {
            "tracked_keys": tracked,
            "locked_keys": locked,
            "lock_events": self.locked_count,
            "max_failures": self.max_failures,
            "window_seconds": self.window_seconds,
            "lockout_seconds": self.lockout_seconds,
            "scope": "single_process_memory",
        }

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self.locked_count = 0


login_throttle = LoginThrottle()