"""The host-private drive ledger (spec 4.0): one JSON file per (board_key, job) in the supervisor's
private directory (~/.local/share/swarm/supervisor/drive), reached only through supervisor.privfs
(descriptor-relative, O_NOFOLLOW, atomic write + fsync of file and directory), so a sandboxed agent
that can write elsewhere under $HOME cannot plant or alter it. A ledger is never trusted from the
board: it records the host's own intents (budget reserved before a launch), owners, integrations
and the terminal state of a drive goal job.

File: "<sha256(board_key NUL job)>.json", {"v": 1, ...fields}.

Concurrency: every write goes through save(), which takes the ledger's lock (a flock on
"<sha>.lock" in the same directory) and refuses (LedgerConflict) when the stored ledger_rev is not
the revision this copy was loaded at. A copy that lost the race is reloaded and its change made
again; nothing is overwritten silently. create() is exclusive (LedgerExistsError over any entry,
good or bad); activate() creates or loads-and-rolls-over.

Incarnation: create() gives each ledger a random id. Receipts carry it, so the receipts of a
deleted and re-created job never validate against the new one.

Loading is strict: unknown or malformed content, out-of-range amounts and unsafe keys are rejected
(LedgerBadError from load; a LedgerBad entry from unfinished), never silently treated as "no
ledger". A link, FIFO or directory at the name is refused the same way.

Windows: there is no O_NOFOLLOW and privfs checks no modes (compat.HAS_MODES is False), so a link
or junction at the name is followed there; the directory's NTFS ACLs are the privacy, as for every
safefs user. The symlink tests are POSIX-only for this reason."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field, fields

from swarm import compat, enrolment
from swarm.supervisor import privfs

log = logging.getLogger("swarm.supervisor.ledger")

SUBDIR = "drive"
FORMAT = 1
MAX_BYTES = 4 * 1024 * 1024
STATES = ("claiming", "claimed", "concluded")
LIFECYCLE = ("activating", "active")     # [RR1]: a ledger is "activating" until its activation is complete
VERDICTS = ("met", "not_met", "unreachable")
ACTIVATE_TRIES = 5
_FILE = re.compile(r"[0-9a-f]{64}\.json")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_MAX_ID = 1024          # launch and action ids
_MAX_TEXT = 4096        # free text fields (reasons, questions, ...)


class LedgerBadError(RuntimeError):
    """A ledger file exists but is unreadable, malformed, unsafe or not for the key asked."""


class LedgerExistsError(RuntimeError):
    """create() found an entry (a good or a bad ledger) already at the job's name."""


class LedgerFinishedError(RuntimeError):
    """activate() on a job whose ledger is terminal (MET, UNREACHABLE or CANCELLED): a finished job is
    not re-opened by activation; a new job is needed."""


class LedgerConflict(RuntimeError):
    """save() found the stored ledger changed since this copy was loaded: reload and try again."""


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
    incarnation: str = ""
    ledger_rev: int = 0
    state: str = "active"            # [RR1] "activating" until the five activation steps are done
    models: dict = field(default_factory=dict)   # [P1] the job's role -> model overrides

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
        """Store this ledger under the lock, if the stored revision is the one this copy was
        loaded at. LedgerConflict otherwise (reload, change again, save). After any OSError the
        state on disk is unknown: reload before saving again."""
        self._commit(creating=False)

    def _commit(self, *, creating: bool) -> None:
        name = _name(self.board_key, self.job)
        with privfs.dir_fd(SUBDIR) as d, privfs.locked(d, _lock_name(name)):
            if creating:
                if privfs.exists(d, name):
                    raise LedgerExistsError(f"a ledger for job {self.job!r} already exists")
                expected = 0
            else:
                stored = _read_entry(d, name, self.board_key, self.job)
                expected = stored.ledger_rev if stored is not None else 0
                if stored is not None and stored.incarnation != self.incarnation:
                    raise LedgerConflict(f"job {self.job!r}: the job was deleted and created again")
            if expected != self.ledger_rev:
                raise LedgerConflict(f"job {self.job!r}: the stored ledger is revision {expected}, "
                                     f"this copy is {self.ledger_rev}")
            body = json.dumps({"v": FORMAT, **asdict(self), "ledger_rev": self.ledger_rev + 1},
                              sort_keys=True, allow_nan=False)
            _parse(body.encode("utf-8"), self.board_key, self.job)   # never write what load refuses
            privfs.write_atomic(d, name, body, durable=True)
            self.ledger_rev += 1


# --- validation --------------------------------------------------------------------------------

_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def _safe_text(value, limit: int) -> bool:
    """A non-empty string of at most `limit` characters with no control, format, surrogate,
    private-use, unassigned or line/paragraph-separator character (NUL and bidi overrides too)."""
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(unicodedata.category(c) in _BAD_CATEGORIES for c in value))


def _check_key(board_key, job) -> None:
    if not _safe_text(board_key, enrolment.MAX_BOARD_KEY):
        raise ValueError("board_key must be 1-%d safe characters" % enrolment.MAX_BOARD_KEY)
    if not _safe_text(job, enrolment.MAX_NAME):
        raise ValueError("job must be 1-%d safe characters" % enrolment.MAX_NAME)


def _name(board_key: str, job: str) -> str:
    return hashlib.sha256(f"{board_key}\0{job}".encode("utf-8", "surrogatepass")).hexdigest() + ".json"


def _lock_name(name: str) -> str:
    return name[:-len(".json")] + ".lock"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _amount(v) -> bool:
    """A finite, non-negative number (a cost, a duration, a budget)."""
    return _is_num(v) and v >= 0


def _nat(v, minimum: int = 0) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= minimum


def _opt_text(v, limit: int = _MAX_TEXT) -> bool:
    return v is None or _safe_text(v, limit)


def _scalar(v) -> bool:
    return v is None or isinstance(v, (bool, str)) or (_is_num(v))


def _need(ok: bool, what: str) -> None:
    if not ok:
        raise ValueError(what)


def _intent(o) -> Intent:
    if not isinstance(o, dict) or set(o) != {f.name for f in fields(Intent)}:
        raise ValueError("intent fields")
    _need(_safe_text(o["launch_id"], _MAX_ID) and _safe_text(o["action_id"], _MAX_ID), "intent ids")
    _need(_nat(o["attempt"], 1), "intent attempt")
    _need(_opt_text(o["seat"], 256), "intent seat")
    _need(_amount(o["reserved_usd"]) and _amount(o["reserved_minutes"]), "intent reservation")
    _need(isinstance(o["locks"], list) and all(_safe_text(x, _MAX_ID) for x in o["locks"]), "intent locks")
    _need(o["state"] in STATES, "intent state")
    if o["state"] == "concluded":
        _need(_safe_text(o["outcome"], 256), "concluded intent needs an outcome")
        _need(isinstance(o["success"], bool), "concluded intent needs success")
        _need(_amount(o["charged_usd"]), "concluded intent needs a charge >= 0")
    else:
        _need(o["outcome"] is None and o["success"] is None and o["charged_usd"] is None,
              "an open intent has no outcome or charge yet")
    return Intent(**o)


def _verdict_keys(o: dict, required: set, optional: set, what: str) -> None:
    keys = set(o)
    _need(required <= keys <= required | optional, f"{what} keys")


def _terminal_intent(o) -> dict:
    _verdict_keys(o, {"verdict", "scope", "generation", "candidate", "req", "by", "at"}, set(), "terminal_intent")
    _need(o["verdict"] in VERDICTS, "terminal_intent verdict")
    _need(_safe_text(o["scope"], 64), "terminal_intent scope")
    _need(_nat(o["generation"], 1), "terminal_intent generation")
    _need(_opt_text(o["candidate"], 256) and _opt_text(o["req"], 256), "terminal_intent candidate/req")
    _need(_safe_text(o["by"], 256) and _safe_text(o["at"], 64), "terminal_intent by/at")
    return o


def _terminal(o) -> dict:
    _verdict_keys(o, {"verdict", "at"}, {"reasons", "scope"}, "terminal")
    _need(o["verdict"] in VERDICTS + ("cancelled",) and _safe_text(o["at"], 64), "terminal verdict/at")
    if "reasons" in o:
        _need(isinstance(o["reasons"], list) and all(_safe_text(x, _MAX_TEXT) for x in o["reasons"]),
              "terminal reasons")
    return o


def _halted(o) -> dict:
    _verdict_keys(o, {"by", "at"}, {"reason"}, "halted")
    _need(_safe_text(o["by"], 256) and _safe_text(o["at"], 64), "halted by/at")
    _need(_opt_text(o.get("reason", None)), "halted reason")
    return o


def _text_map(m, value_check, key_limit: int = 256) -> dict:
    _need(isinstance(m, dict), "map")
    for k, v in m.items():
        _need(_safe_text(k, key_limit) and value_check(v), f"map entry {k!r:.40}")
    return m


_DICT_FIELDS = ("budgets", "owners", "integrated", "watermarks", "pinned_requirements", "models")
_OPT_DICT = ("halted", "terminal", "terminal_intent")
_OPT_STR = ("ruling_due_at", "closing", "last_pass", "requester_session")
_NATS = ("ruling_attempts", "rounds_without_progress", "errors")


def _check_fields(obj: dict, board_key: str | None, job: str | None) -> None:
    """Raise ValueError for any field whose value a ledger may not hold."""
    if obj.get("v") != FORMAT:
        raise ValueError("not a version 1 ledger")
    names = {f.name for f in fields(Ledger)}
    if set(obj) != names | {"v"}:
        raise ValueError(f"fields differ: {sorted(set(obj) ^ (names | {'v'}))[:5]}")
    _check_key(obj["board_key"], obj["job"])
    if (board_key is not None and obj["board_key"] != board_key) or (job is not None and obj["job"] != job):
        raise ValueError("ledger is for another key")
    _need(_nat(obj["generation"], 1) and _nat(obj["generation_requested"], 1), "generations")
    _need(all(_nat(obj[k]) for k in _NATS), "counters")
    _need(_nat(obj["ledger_rev"]), "ledger_rev")
    _need(_amount(obj["halted_total_seconds"]), "halted_total_seconds")
    _need(_HEX32.fullmatch(obj["incarnation"]) is not None, "incarnation")
    _need(isinstance(obj["activated_at"], str) and len(obj["activated_at"]) <= 64, "activated_at")
    _need(all(_opt_text(obj[k], 64) for k in _OPT_STR), "optional strings")
    _need(isinstance(obj["mirror_fetch_pending"], list)
          and all(_safe_text(x, _MAX_ID) for x in obj["mirror_fetch_pending"]), "mirror_fetch_pending")
    _need(isinstance(obj["intents"], list), "intents")
    _text_map(obj["budgets"], _amount)
    _text_map(obj["owners"], lambda v: _safe_text(v, 256))
    _text_map(obj["watermarks"], lambda v: _nat(v))
    _text_map(obj["pinned_requirements"], lambda v: _safe_text(v, _MAX_ID))
    _text_map(obj["models"], lambda v: _safe_text(v, 64))
    _need(obj["state"] in LIFECYCLE, "state")
    _text_map(obj["integrated"], lambda v: isinstance(v, dict) and all(
        _safe_text(k, 256) and _scalar(x) for k, x in v.items()))
    for k in _OPT_DICT:
        _need(obj[k] is None or isinstance(obj[k], dict), k)
    if obj["halted"] is not None:
        _halted(obj["halted"])
    if obj["terminal"] is not None:
        _terminal(obj["terminal"])
    if obj["terminal_intent"] is not None:
        _terminal_intent(obj["terminal_intent"])


def _parse(data: bytes, board_key: str | None, job: str | None) -> Ledger:
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    _check_fields(obj, board_key, job)
    obj = {k: v for k, v in obj.items() if k != "v"}
    obj["intents"] = [_intent(i) for i in obj["intents"]]
    return Ledger(**obj)


def _hint(data: bytes | None) -> str | None:
    try:
        j = json.loads(data or b"")
        return j["job"] if isinstance(j, dict) and isinstance(j.get("job"), str) else None
    except (ValueError, KeyError, TypeError, RecursionError):
        return None


# --- reading -----------------------------------------------------------------------------------

_UNSAFE_FILE = "not a private regular file of this user, or too large"


def _read_entry(d: int, name: str, board_key: str | None, job: str | None) -> Ledger | None:
    """The ledger stored at `name`: None when there is no entry, LedgerBadError when there is one
    that is unsafe (a link, FIFO, directory, oversize) or does not parse."""
    try:
        data = privfs.read(d, name, MAX_BYTES)
    except OSError as exc:
        raise LedgerBadError(f"{name}: {exc}") from exc
    if data is None:
        if privfs.exists(d, name):
            raise LedgerBadError(f"{name}: {_UNSAFE_FILE}")
        return None
    try:
        return _parse(data, board_key, job)
    except (ValueError, TypeError, RecursionError, ArithmeticError) as exc:
        log.warning("bad ledger %s: %s", name, exc)
        raise LedgerBadError(f"{name}: {exc}") from exc


# --- API ---------------------------------------------------------------------------------------

def load(board_key: str, job: str) -> Ledger | None:
    """The ledger, None when there is none, LedgerBadError when there is an entry that is bad."""
    _check_key(board_key, job)
    name = _name(board_key, job)
    # Under the lock: a save replaces the file by rename, and a reader that opened the old one
    # just before it would see a link count of 0 and call the entry unsafe.
    with privfs.dir_fd(SUBDIR) as d, privfs.locked(d, _lock_name(name)):
        return _read_entry(d, name, board_key, job)


def create(board_key: str, job: str, *, budgets: dict, activated_at: str, state: str = "active",
           models: dict | None = None, pinned: dict | None = None) -> Ledger:
    """A new ledger with a fresh incarnation, written at once. LedgerExistsError if any entry is
    already at the job's name (a good one or a bad one: nothing is overwritten). Activation creates
    it as "activating" and completes it to "active" once its steps are done ([RR1])."""
    _check_key(board_key, job)
    if state not in LIFECYCLE:
        raise ValueError(f"ledger state must be one of {LIFECYCLE}")
    led = Ledger(board_key=board_key, job=job, budgets=dict(budgets), activated_at=activated_at,
                 incarnation=uuid.uuid4().hex, state=state, models=dict(models or {}),
                 pinned_requirements=dict(pinned or {}))
    led._commit(creating=True)
    return led


def activate(board_key: str, job: str, *, budgets: dict, activated_at: str) -> Ledger:
    """Create the job's ledger, or re-activate it: load it, bump generation_requested (a rollover),
    update the budgets and the activation time, keep intents, charges and the incarnation. A bad
    entry is refused, never replaced."""
    _check_key(board_key, job)
    for _ in range(ACTIVATE_TRIES):
        led = load(board_key, job)
        if led is None:
            try:
                return create(board_key, job, budgets=budgets, activated_at=activated_at)
            except LedgerExistsError:
                continue   # created by someone else since the load: roll it over instead
        if led.terminal is not None:
            raise LedgerFinishedError(f"job {job!r} is finished ({led.terminal.get('verdict')}); "
                                      f"activate a new job instead")
        led.generation_requested += 1
        led.budgets.update(budgets)
        led.activated_at = activated_at
        try:
            led.save()
            return led
        except LedgerConflict:
            continue
    raise LedgerConflict(f"job {job!r}: the ledger kept changing; try again")


def unfinished(board_key: str) -> list:
    """Every ledger of this board whose terminal is None, plus a LedgerBad for each entry that
    does not parse or is unsafe (its board is unknowable then, so it is reported for every board)."""
    out: list = []
    with privfs.dir_fd(SUBDIR) as d:
        for name in sorted(compat.listdir(d)):
            if not _FILE.fullmatch(name):
                continue
            with privfs.locked(d, _lock_name(name)):   # as load(): not while a save replaces it
                try:
                    data = privfs.read(d, name, MAX_BYTES)
                except OSError as exc:
                    out.append(LedgerBad(None, name, str(exc)))
                    continue
                if data is None:
                    if privfs.exists(d, name):
                        out.append(LedgerBad(None, name, _UNSAFE_FILE))
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
