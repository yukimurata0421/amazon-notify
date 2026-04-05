import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def reset_incident_memory_suppression() -> None:
    from amazon_notify import notifier

    current = getattr(notifier, "_INCIDENT_MEMORY_SUPPRESSED_UNTIL", None)
    if isinstance(current, dict):
        current.clear()
    yield
    current = getattr(notifier, "_INCIDENT_MEMORY_SUPPRESSED_UNTIL", None)
    if isinstance(current, dict):
        current.clear()
