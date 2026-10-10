"""Crash barriers for chaos tests: SIGKILL this process at a named point when asked."""
from __future__ import annotations

import os
import signal


def hit(name: str) -> None:
    if os.environ.get("SWARM_CRASH_AT") == name:
        os.kill(os.getpid(), signal.SIGKILL)
