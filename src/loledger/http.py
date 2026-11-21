"""HTTP plumbing: per-host rate limiting and retry with exponential backoff."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

log = logging.getLogger(__name__)


class TransientError(Exception):
    """Raised by response checkers for retryable API-level errors (e.g. rate limit in body)."""


class RateLimiter:
    def __init__(self, rps: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.interval = 1.0 / rps if rps > 0 else 0.0
        self._next = 0.0
        self._clock = clock
        self._sleep = sleep

    def wait(self) -> None:
        now = self._clock()
        if now < self._next:
            self._sleep(self._next - now)
            now = self._next
        self._next = now + self.interval


class HttpClient:
    def __init__(
        self,
        rps: float,
        *,
        timeout: float = 30.0,
        max_attempts: int = 6,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limiter = RateLimiter(rps, sleep=sleep)
        self.max_attempts = max_attempts
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": "loledger/0.1"})
        self._sleep = sleep
        self.calls = 0

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        check: Callable[[Any], None] | None = None,
    ) -> Any:
        delay = 1.0
        for attempt in range(1, self.max_attempts + 1):
            self.limiter.wait()
            self.calls += 1
            try:
                resp = self.client.request(method, url, params=params, json=json_body)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                    raise TransientError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                body = resp.json()
                if check is not None:
                    check(body)
                return body
            except (TransientError, httpx.TransportError) as e:
                if attempt == self.max_attempts:
                    raise
                log.warning("retry %d/%d after %.0fs: %s", attempt, self.max_attempts, delay, e)
                self._sleep(delay)
                delay = min(delay * 2, 60.0)
        raise AssertionError("unreachable")
