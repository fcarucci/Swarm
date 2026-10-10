"""Crash barriers for chaos tests: SIGKILL this process at a named point when asked."""
from __future__ import annotations

import os
import signal

# Windows has no SIGKILL; os.kill there terminates the process with the given value as exit code.
_KILL = getattr(signal, "SIGKILL", None) or signal.SIGTERM


def hit(name: str) -> None:
    if os.environ.get("SWARM_CRASH_AT") == name:
        os.kill(os.getpid(), _KILL)
