from __future__ import annotations

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


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

    now = datetime.now(timezone.utc)
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - now).total_seconds())
