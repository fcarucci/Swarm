"""The host-private drive ledger (spec 4.0): one JSON file per (board_key, job) in the supervisor's
private directory (~/.local/share/swarm/supervisor/drive), reached only through supervisor.privfs
(descriptor-relative, O_NOFOLLOW, atomic write + fsync of file and directory), so a sandboxed agent
that can write elsewhere under $HOME cannot plant or alter it. A ledger is never trusted from the
board: it records the host's own intents (budget reserved before a launch), owners, integrations
and the terminal state of a drive goal job.

File: "<sha256(board_key NUL job)>.json", {"v": 1, ...fields}. Loading is strict: unknown or
malformed content is rejected (LedgerBadError from load; a LedgerBad entry from unfinished), never
silently treated as "no ledger"."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import asdict, dataclass, field, fields

from swarm import compat
from swarm.supervisor import privfs

log = logging.getLogger("swarm.supervisor.ledger")

SUBDIR = "drive"
FORMAT = 1
MAX_BYTES = 4 * 1024 * 1024
STATES = ("claiming", "claimed", "concluded")
_FILE = re.compile(r"[0-9a-f]{64}\.json")


class LedgerBadError(RuntimeError):
    """A ledger file exists but is unreadable, malformed or not for the key asked."""


@dataclass(frozen=True)
class LedgerBad:
    job_hint: str | None
    path: str
    why: str


@dataclass
class Intent:
    launch_id: str
    action_id: str
    attempt: int
    seat: str | None
    reserved_usd: float
    reserved_minutes: float
    locks: list
    state: str
    outcome: str | None = None
    success: bool | None = None
    charged_usd: float | None = None


@dataclass
class Ledger:
    board_key: str
    job: str
    generation: int = 1
    generation_requested: int = 1
    activated_at: str = ""
    budgets: dict = field(default_factory=dict)
    intents: list = field(default_factory=list)
    owners: dict = field(default_factory=dict)
    integrated: dict = field(default_factory=dict)
    ruling_due_at: str | None = None
    ruling_attempts: int = 0
    rounds_without_progress: int = 0
    errors: int = 0
    closing: str | None = None
    halted: dict | None = None
    terminal: dict | None = None
    mirror_fetch_pending: list = field(default_factory=list)
    last_pass: str | None = None
    watermarks: dict = field(default_factory=dict)
    terminal_intent: dict | None = None
    halted_total_seconds: float = 0.0
    requester_session: str | None = None
    pinned_requirements: dict = field(default_factory=dict)

    def charged_usd(self) -> float:
        """Concluded intents' charged cost plus the reservation of every open one."""
        total = 0.0
        for i in self.intents:
            if i.state == "concluded":
                total += i.charged_usd or 0.0
            else:
                total += i.reserved_usd
        return total

    def save(self) -> None:
        body = json.dumps({"v": FORMAT, **asdict(self)}, sort_keys=True)
        with privfs.dir_fd(SUBDIR) as d:
            privfs.write_atomic(d, _name(self.board_key, self.job), body)


# --- validation --------------------------------------------------------------------------------

def _name(board_key: str, job: str) -> str:
    return hashlib.sha256(f"{board_key}\0{job}".encode("utf-8", "surrogatepass")).hexdigest() + ".json"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _opt_str(v) -> bool:
    return v is None or isinstance(v, str)


def _intent(o) -> Intent:
    if not isinstance(o, dict) or set(o) != {f.name for f in fields(Intent)}:
        raise ValueError("intent fields")
    ok = (isinstance(o["launch_id"], str) and isinstance(o["action_id"], str) and _int(o["attempt"])
          and _opt_str(o["seat"]) and _num(o["reserved_usd"]) and _num(o["reserved_minutes"])
          and isinstance(o["locks"], list) and all(isinstance(x, str) for x in o["locks"])
          and o["state"] in STATES and _opt_str(o["outcome"])
          and (o["success"] is None or isinstance(o["success"], bool))
          and (o["charged_usd"] is None or _num(o["charged_usd"])))
    if not ok:
        raise ValueError("intent values")
    return Intent(**o)


_DICT_FIELDS = ("budgets", "owners", "integrated", "watermarks", "pinned_requirements")
_OPT_DICT = ("halted", "terminal", "terminal_intent")
_OPT_STR = ("ruling_due_at", "closing", "last_pass", "requester_session")
_INTS = ("generation", "generation_requested", "ruling_attempts", "rounds_without_progress", "errors")


def _parse(data: bytes, board_key: str | None, job: str | None) -> Ledger:
    obj = json.loads(data)
    if not isinstance(obj, dict) or obj.get("v") != FORMAT:
        raise ValueError("not a version 1 ledger")
    names = {f.name for f in fields(Ledger)}
    if set(obj) != names | {"v"}:
        raise ValueError(f"fields differ: {sorted(set(obj) ^ (names | {'v'}))[:5]}")
    if (not isinstance(obj["board_key"], str) or not obj["board_key"]
            or not isinstance(obj["job"], str) or not obj["job"]):
        raise ValueError("board_key/job")
    if (board_key is not None and obj["board_key"] != board_key) or (job is not None and obj["job"] != job):
        raise ValueError("ledger is for another key")
    if (any(not _int(obj[k]) for k in _INTS) or any(not isinstance(obj[k], dict) for k in _DICT_FIELDS)
            or any(obj[k] is not None and not isinstance(obj[k], dict) for k in _OPT_DICT)
            or any(not _opt_str(obj[k]) for k in _OPT_STR) or not isinstance(obj["activated_at"], str)
            or not _num(obj["halted_total_seconds"])
            or not isinstance(obj["mirror_fetch_pending"], list)
            or not all(isinstance(x, str) for x in obj["mirror_fetch_pending"])
            or not isinstance(obj["intents"], list)):
        raise ValueError("field types")
    if (not all(isinstance(k, str) and _int(v) for k, v in obj["watermarks"].items())
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in obj["owners"].items())
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in obj["pinned_requirements"].items())):
        raise ValueError("map types")
    obj = {k: v for k, v in obj.items() if k != "v"}
    obj["intents"] = [_intent(i) for i in obj["intents"]]
    return Ledger(**obj)


def _hint(data: bytes | None) -> str | None:
    try:
        j = json.loads(data or b"")
        return j["job"] if isinstance(j, dict) and isinstance(j.get("job"), str) else None
    except (ValueError, KeyError, TypeError, RecursionError):
        return None


# --- API ---------------------------------------------------------------------------------------

def load(board_key: str, job: str) -> Ledger | None:
    """The ledger, None when there is none, LedgerBadError when the file exists but is bad."""
    name = _name(board_key, job)
    with privfs.dir_fd(SUBDIR) as d:
        try:
            data = privfs.read(d, name, MAX_BYTES)
            if data is None and privfs.exists(d, name):
                raise LedgerBadError(f"{name}: not a private regular file of this user, or too large")
        except OSError as exc:
            raise LedgerBadError(f"{name}: {exc}") from exc
    if data is None:
        return None
    try:
        return _parse(data, board_key, job)
    except (ValueError, TypeError, RecursionError, ArithmeticError) as exc:
        log.warning("bad ledger %s: %s", name, exc)
        raise LedgerBadError(f"{name}: {exc}") from exc


def create(board_key: str, job: str, *, budgets: dict, activated_at: str) -> Ledger:
    """A new ledger, written at once (so an unsafe directory fails here). An existing one is
    replaced only by its owner calling create again at activation."""
    led = Ledger(board_key=board_key, job=job, budgets=dict(budgets), activated_at=activated_at)
    led.save()
    return led


def unfinished(board_key: str) -> list:
    """Every ledger of this board whose terminal is None, plus a LedgerBad for each file that
    does not parse (its board is unknowable then, so it is reported for every board)."""
    out: list = []
    with privfs.dir_fd(SUBDIR) as d:
        for name in sorted(compat.listdir(d)):
            if not _FILE.fullmatch(name):
                continue
            try:
                data = privfs.read(d, name, MAX_BYTES)
            except OSError as exc:
                out.append(LedgerBad(None, name, str(exc)))
                continue
            if data is None:
                if privfs.exists(d, name):   # a planted link, FIFO, directory or oversize file
                    out.append(LedgerBad(None, name, "not a private regular file of this user, or too large"))
                continue
            try:
                led = _parse(data, None, None)
            except (ValueError, TypeError, RecursionError, ArithmeticError) as exc:
                log.warning("bad ledger %s: %s", name, exc)
                out.append(LedgerBad(_hint(data), name, str(exc)))
                continue
            if _name(led.board_key, led.job) != name:
                out.append(LedgerBad(led.job, name, "file name does not match its key"))
            elif led.board_key == board_key and led.terminal is None:
                out.append(led)
    return out
