"""Injectable clock: one place for wall time and monotonic time, fakeable in tests."""
from __future__ import annotations

import contextlib
import time
from datetime import datetime, timezone

_fake = None


def now() -> datetime:
    """Aware UTC wall-clock time."""
    if _fake is not None:
        return _fake.now()
    return datetime.now(timezone.utc)


def monotonic() -> float:
    if _fake is not None:
        return _fake.monotonic()
    return time.monotonic()


@contextlib.contextmanager
def use(fake):
    """Route now()/monotonic() to `fake` (an object with those two methods); nesting restores."""
    global _fake
    prev, _fake = _fake, fake
    try:
        yield fake
    finally:
        _fake = prev
