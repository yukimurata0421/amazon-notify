from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

_T = TypeVar("_T")


def next_delay_seconds(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter_ratio: float = 0.0,
) -> float:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if base_delay <= 0:
        raise ValueError("base_delay must be > 0")
    if max_delay <= 0:
        raise ValueError("max_delay must be > 0")
    if jitter_ratio < 0:
        raise ValueError("jitter_ratio must be >= 0")

    delay = base_delay * (2 ** (attempt - 1))
    capped_delay = min(delay, max_delay)
    if jitter_ratio == 0 or capped_delay >= max_delay:
        # すでに上限に達している場合は jitter を加えても再び max_delay に吸収されるため、
        # 実効値と意図が変わらない。ここでは分岐を明示して読みやすさを優先する。
        return capped_delay

    jitter = random.uniform(0.0, capped_delay * jitter_ratio)
    return min(capped_delay + jitter, max_delay)


def retry_with_backoff(
    fn: Callable[[], _T],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    should_retry: Callable[[Exception], bool],
    on_retry: Callable[[int, int, float, Exception], None] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> _T:
    """Execute *fn* with exponential back-off retry.

    Raises the last exception when all attempts are exhausted or when
    *should_retry* returns ``False``.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    _sleep = sleep_fn if sleep_fn is not None else time.sleep
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not should_retry(exc) or attempt == max_attempts:
                break
            delay = next_delay_seconds(
                attempt,
                base_delay=base_delay,
                max_delay=max_delay,
            )
            if on_retry is not None:
                on_retry(attempt, max_attempts, delay, exc)
            _sleep(delay)

    if last_exc is None:
        raise RuntimeError("retry_with_backoff exhausted without exception")
    raise last_exc


def parse_retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None

    text = header_value.strip()
    if not text:
        return None

    if text.isdigit():
        return float(text)

    try:
        target = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None

    now = datetime.now(UTC)
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - now).total_seconds())
