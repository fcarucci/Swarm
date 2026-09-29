"""The board-outage window, kept per machine and OS user in
board-outage.json in the supervisor's private directory (settings.private_dir: no Codex sandbox
can write it): {"started": iso, "recovered": iso or null}. The
supervisor notes every failed and every successful board open; stuck detection gives agents
whose last contact falls inside the window dead_minutes of grace after the board is back."""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path



@dataclass(frozen=True)
class Outage:
    started: _dt.datetime
    recovered: _dt.datetime | None

    def affects(self, last_contact: _dt.datetime, grace: _dt.timedelta) -> bool:
        return self.started - grace <= last_contact and (self.recovered is None or last_contact <= self.recovered)


def path() -> Path:
    from swarm.supervisor.settings import private_dir
    return private_dir() / "board-outage.json"


def _now(now):
    return now or _dt.datetime.now(_dt.timezone.utc)


def _read() -> Outage | None:
    from swarm.supervisor.settings import read_private
    try:
        d = json.loads(read_private(path().name) or b"null")
        rec = d.get("recovered")
        return Outage(_dt.datetime.fromisoformat(d["started"]),
                      _dt.datetime.fromisoformat(rec) if rec else None)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write(o: Outage) -> None:
    from swarm.supervisor.settings import write_private
    write_private(path().name, json.dumps({"started": o.started.isoformat(),
                                           "recovered": o.recovered.isoformat() if o.recovered else None}))


def note_unreachable(now: _dt.datetime | None = None) -> Outage:
    cur = _read()
    if cur is not None and cur.recovered is None:
        return cur
    o = Outage(_now(now), None)
    _write(o)
    return o


def note_reachable(now: _dt.datetime | None = None) -> Outage | None:
    cur = _read()
    if cur is None or cur.recovered is not None:
        return cur
    o = Outage(cur.started, _now(now))
    _write(o)
    return o


def current(now: _dt.datetime | None = None, keep_hours: float = 24) -> Outage | None:
    cur = _read()
    if cur is None:
        return None
    if cur.recovered is not None and _now(now) - cur.recovered > _dt.timedelta(hours=keep_hours):
        return None
    return cur
