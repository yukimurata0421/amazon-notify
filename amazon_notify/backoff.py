from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def next_delay_seconds(attempt: int, *, base_delay: float, max_delay: float) -> float:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if base_delay <= 0:
        raise ValueError("base_delay must be > 0")
    if max_delay <= 0:
        raise ValueError("max_delay must be > 0")

    delay = base_delay * (2 ** (attempt - 1))
    return min(delay, max_delay)


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
