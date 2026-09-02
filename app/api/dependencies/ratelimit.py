"""요청 빈도 제한 (SEC-031, Harness §11 / §24).

프로세스 메모리 기반의 고정 윈도우 카운터다. **제약을 분명히 해 둔다**: 워커 프로세스가
여러 개면 각 프로세스가 자기 몫만 세므로 실효 한도는 프로세스 수만큼 커진다. 다중
인스턴스 환경에서 정확한 한도가 필요하면 Redis 기반 구현으로 교체해야 하며, 그때도
이 인터페이스는 그대로 둔다.

그럼에도 이 계층을 두는 이유는, 동시 실행 수 제한(`STT_MAX_CONCURRENT_JOBS`)만으로는
막지 못하는 빠른 반복 요청(로그인 무차별 대입 등)을 걸러야 하기 때문이다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from app.core.exceptions import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)

_WINDOW_SECONDS = 60.0

# 카운터가 무한정 쌓이지 않도록 하는 상한. 넘으면 만료된 항목을 정리한다.
_MAX_TRACKED_KEYS = 10000


@dataclass
class _Window:
    started_at: float
    count: int = 0


@dataclass
class FixedWindowRateLimiter:
    """키 단위 고정 윈도우 카운터."""

    _windows: dict[str, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, key: str, *, limit: int) -> None:
        """호출 1회를 기록하고 한도를 넘으면 거절한다.

        Raises:
            RateLimitedError: 현재 윈도우의 허용량을 초과한 경우.
        """
        now = time.monotonic()
        with self._lock:
            if len(self._windows) > _MAX_TRACKED_KEYS:
                self._evict_expired(now)

            window = self._windows.get(key)
            if window is None or now - window.started_at >= _WINDOW_SECONDS:
                self._windows[key] = _Window(started_at=now, count=1)
                return

            window.count += 1
            if window.count > limit:
                retry_after = _WINDOW_SECONDS - (now - window.started_at)
                logger.warning(
                    "rate limit exceeded",
                    extra={"event": "RATE_LIMITED", "limit": limit},
                )
                raise RateLimitedError(
                    internal_detail=f"limit {limit}/min exceeded",
                    context={"retry_after_seconds": round(retry_after, 1)},
                )

    def _evict_expired(self, now: float) -> None:
        expired = [
            key
            for key, window in self._windows.items()
            if now - window.started_at >= _WINDOW_SECONDS
        ]
        for key in expired:
            del self._windows[key]

    def reset(self) -> None:
        """테스트 격리용."""
        with self._lock:
            self._windows.clear()


# 프로세스 단위 인스턴스. 위 docstring 의 제약이 여기서 비롯된다.
api_rate_limiter = FixedWindowRateLimiter()
login_rate_limiter = FixedWindowRateLimiter()
upload_rate_limiter = FixedWindowRateLimiter()
