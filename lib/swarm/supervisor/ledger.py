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

import datetime
import hashlib
import json
import logging
import math
import os
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


class LedgerTooLargeError(RuntimeError):
    """save() or create() of a ledger whose serialized form is over MAX_BYTES: nothing is written (a ledger
    load would refuse it, and the job would hold)."""


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
    activation_heads: dict = field(default_factory=dict)   # the target's head per remote URL at activation

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
            data = body.encode("utf-8")
            if len(data) > MAX_BYTES:
                raise LedgerTooLargeError(f"job {self.job!r}: the ledger would be {len(data)} bytes, over "
                                          f"{MAX_BYTES}: nothing written")
            _parse(data, self.board_key, self.job)   # never write what load refuses
            privfs.write_atomic(d, name, body, durable=True)
            self.ledger_rev += 1


# --- validation --------------------------------------------------------------------------------

_BAD_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


def _safe_text(value, limit: int) -> bool:
    """A non-empty string of at most `limit` characters with no control, format, surrogate,
    private-use, unassigned or line/paragraph-separator character (NUL and bidi overrides too)."""
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(unicodedata.category(c) in _BAD_CATEGORIES for c in value))


def serialized_size(led) -> int:
    """The bytes the ledger `led` would take on disk (as _commit writes it)."""
    body = json.dumps({"v": FORMAT, **asdict(led), "ledger_rev": led.ledger_rev + 1}, sort_keys=True, allow_nan=False)
    return len(body.encode("utf-8"))


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
    _need(o["verdict"] in VERDICTS and _safe_text(o["at"], 64), "terminal verdict/at")
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


_DICT_FIELDS = ("budgets", "owners", "integrated", "watermarks", "pinned_requirements", "models",
                "activation_heads")
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
    _text_map(obj["activation_heads"], lambda v: isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40,64}", v) is not None,
              key_limit=2048)
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


def _move_aside(d: int, name: str) -> str:
    """Rename the entry `name` to `<name>.bad-<UTC stamp>-<random>` (a name the scan does not read as a
    ledger; stamps sort by time) and sync the directory. The caller holds the ledger's lock."""
    if os.rename not in os.supports_dir_fd:
        raise LedgerBadError("this platform cannot move a bad ledger aside; a person must do it")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = f"{name[:-len('.json')]}.bad-{stamp}-{uuid.uuid4().hex[:12]}"
    os.rename(name, target, src_dir_fd=d, dst_dir_fd=d)
    try:
        os.fsync(d)
    except OSError:   # directories cannot be synced everywhere; the rename itself is done
        pass
    return target


def archive_bad(board_key: str, job: str) -> str:
    """Move the job's bad ledger entry aside (never deleted, so `create` may start a fresh one): renamed under
    the lock (_move_aside). Returns the new name. ValueError when it is readable; NoLedgerError when there is
    no entry; LedgerBadError where a rename relative to the directory is not available."""
    _check_key(board_key, job)
    name = _name(board_key, job)
    with privfs.dir_fd(SUBDIR) as d, privfs.locked(d, _lock_name(name)):
        try:
            _read_entry(d, name, board_key, job)
        except LedgerBadError:
            pass
        else:
            if not privfs.exists(d, name):
                raise NoLedgerError(f"no ledger entry for job {job!r}")
            raise ValueError(f"the ledger of job {job!r} is readable: nothing to archive")
        return _move_aside(d, name)


class NoLedgerError(ValueError):
    """archive_bad() found no entry at all at the job's name (not a readable one)."""


# --- repair (spec [F2]: a person rebuilds a bad ledger; never silently) ----------------------------------

REPAIR_REASON = "drive repair"     # the halt reason prefix of a repaired ledger: unhalt wants budgets first
SALVAGE_MAX = 64 * 1024 * 1024     # a bad ledger may be over MAX_BYTES (that can be why it is bad)


@dataclass
class Repaired:
    """What repair() did: the fresh ledger, the archive it salvaged from, and what it kept and lost."""
    ledger: Ledger
    archived: str | None      # the bad entry's new name, when this repair moved it (None: moved earlier)
    source: str | None        # the archive the fields were salvaged from (None: there was none)
    kept: list               # field names recovered from the archive
    lost: list               # one human line per thing not recovered
    finished: bool           # the fresh ledger is terminal (the job stays finished)
    archived_generation: int | None


def archives(d: int, name: str) -> list:
    """The archived (moved-aside) entries of the ledger `name`, oldest first."""
    prefix = name[:-len(".json")] + ".bad-"
    return sorted(n for n in compat.listdir(d) if n.startswith(prefix))


def _salvage(raw: bytes | None) -> dict | None:
    """The archived ledger as a plain JSON object, read leniently (no field checks), or None."""
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def _checked(old: dict, key: str, check) -> tuple:
    """(True, value) when old[key] passes `check` (a predicate, or a validator that raises ValueError)."""
    if key not in old:
        return False, None
    v = old[key]
    try:
        ok = check(v)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False, None
    return (ok is not False), v


def _spend_lines(old: dict) -> list:
    """What the archived intents say about spend and open launches (lenient: what can be read)."""
    intents = old.get("intents")
    if not isinstance(intents, list):
        return ["spend and launches: the archived intents do not read; the spend so far is unknown"]
    spent, open_ = 0.0, []
    for i in intents:
        if not isinstance(i, dict):
            continue
        charge = i.get("charged_usd") if i.get("state") == "concluded" else i.get("reserved_usd")
        if _amount(charge):
            spent += charge
        if i.get("state") in ("claiming", "claimed"):
            open_.append(f"{str(i.get('launch_id'))[:80]} (seat {str(i.get('seat'))[:64]})")
    lines = [f"spend: ${spent:.2f} charged or reserved over {len(intents)} launches; the fresh ledger counts "
             "from $0, so set the budgets net of it"]
    if open_:
        lines.append("open launches (their sessions may still run, their locks are forgotten; stop them): "
                     + ", ".join(open_))
    return lines


def repair(board_key: str, job: str, *, at: str, by: str, generation: int | None = None,
           claimed: bool = False, board_finished: str | None = None) -> Repaired:
    """Replace a bad ledger by a fresh one, under the ledger's lock from the check to the write (an activation
    cannot slip in between). The bad entry is moved aside (never deleted). A retry after a crash between the
    move and the write finds no entry and the newest archive, and finishes the repair from it.

    Salvaged from the archive when they validate: generation (never from the board), generation_requested,
    incarnation, activated_at, requester_session, pinned_requirements, watermarks, halted_total_seconds and
    the round counters, terminal and terminal_intent. Never salvaged: budgets (a person sets them again, net
    of the spend so far), intents, owners, integrated groups, pending mirror fetches. `generation` (a person's
    --generation) is needed when the archive gives none, and must agree with it when it does.

    The fresh ledger is halted (reason REPAIR_REASON) unless the job had finished: then it stays terminal
    (archived terminal, else a terminal built from the archived terminal_intent, the archived terminal's
    verdict or `board_finished`, the board job's closed verdict). ValueError when there is nothing to repair
    (a readable ledger; no entry, no archive and not `claimed`) or the generation is unknown or disagrees."""
    _check_key(board_key, job)
    name = _name(board_key, job)
    with privfs.dir_fd(SUBDIR) as d, privfs.locked(d, _lock_name(name)):
        try:
            stored = _read_entry(d, name, board_key, job)
        except LedgerBadError:
            stored, bad = None, True
        else:
            bad = False
            if stored is not None:
                raise ValueError(f"{job}'s drive ledger reads fine: nothing to repair")
        archived = None
        if bad:
            try:
                raw = privfs.read(d, name, SALVAGE_MAX)
            except OSError:
                raw = None
            archived = source = _move_aside(d, name)
        else:
            found = archives(d, name)
            source = found[-1] if found else None
            if source is None and not claimed:
                raise ValueError(f"{job} has no drive ledger and no archived one, and the board does not "
                                 "claim it as a drive job: nothing to repair")
            try:
                raw = privfs.read(d, source, SALVAGE_MAX) if source else None
            except OSError:
                raw = None
        old = _salvage(raw) or {}
        kept, lost = [], []
        if raw is not None and not old:
            lost.append("everything: the archived ledger is not a JSON object")
        elif source is None:
            lost.append("everything: there is no archived ledger to recover from")

        ok, gen = _checked(old, "generation", lambda v: _nat(v, 1))
        if not ok:
            if generation is None:
                raise ValueError("the archived ledger gives no generation: a person names it with "
                                 "--generation N (the board's copy is not trusted)")
            gen = generation
            lost.append(f"generation: taken from --generation {generation}")
        elif generation is not None and generation != gen:
            raise ValueError(f"the archived ledger says generation {gen}; --generation {generation} disagrees")
        else:
            kept.append("generation")
        fresh = Ledger(board_key=board_key, job=job, generation=gen, generation_requested=gen,
                       activated_at=at, incarnation=uuid.uuid4().hex)

        def take(key, check, what=None, attr=None):
            ok, v = _checked(old, key, check)
            if ok:
                setattr(fresh, attr or key, v)
                kept.append(key)
            elif what:
                lost.append(what)
            return ok

        take("generation_requested", lambda v: _nat(v, gen))
        take("incarnation", lambda v: isinstance(v, str) and _HEX32.fullmatch(v) is not None,
             "incarnation: a new one (receipts of the old ledger no longer validate)")
        take("activated_at", lambda v: _safe_text(v, 64),
             "activated_at: the wall clock (max_wall_hours) restarts now")
        take("requester_session", lambda v: v is None or _safe_text(v, 64),
             "requester_session: the requester session is unknown to the hooks")
        if not take("pinned_requirements", lambda v: _text_map(v, lambda x: _safe_text(x, _MAX_ID))):
            lost.append("pinned requirements: none recovered, so the pin set is open (a GOAL-READY may pin "
                        "any set): check the goal's requirements before you unhalt")
        take("watermarks", lambda v: _text_map(v, lambda x: _nat(x)), "watermarks: wake marks restart at 0")
        take("halted_total_seconds", _amount, "halted_total_seconds: the halted time is forgotten")
        take("rounds_without_progress", _nat, "rounds_without_progress: restarts at 0")
        take("ruling_attempts", _nat, "ruling_attempts: restarts at 0")
        take("terminal_intent", lambda v: v is None or _terminal_intent(v))
        has_terminal = take("terminal", lambda v: v is None or _terminal(v))

        budgets = old.get("budgets")
        lost.append("budgets: not carried over" + (f" (they were {json.dumps(budgets, sort_keys=True)[:400]})"
                                                   if isinstance(budgets, dict) and budgets else "")
                    + "; set them before unhalt")
        lost.extend(_spend_lines(old) if old else [])
        groups = old.get("integrated")
        if isinstance(groups, dict) and groups:
            lost.append("integrated groups (rebuild from the integration receipts): "
                        + ", ".join(str(g)[:80] for g in list(groups)[:50]))
        owners = old.get("owners")
        if isinstance(owners, dict) and owners:
            lost.append("action owners: " + ", ".join(str(a)[:80] for a in list(owners)[:50]))

        finished = fresh.terminal is not None
        if not finished and (old.get("terminal") is not None or fresh.terminal_intent is not None
                             or board_finished is not None):
            pending = fresh.terminal_intent or {}
            prior = old.get("terminal") if isinstance(old.get("terminal"), dict) else {}
            verdict = next((v for v in (pending.get("verdict"), prior.get("verdict"), board_finished)
                            if v in VERDICTS), "unreachable")
            fresh.terminal = {"verdict": verdict, "at": at, "scope": "repair",
                              "reasons": [f"{REPAIR_REASON}: the job had finished (archived terminal "
                                          f"{'unreadable' if not has_terminal else 'pending'}; board "
                                          f"{board_finished or 'open'}); it stays finished"]}
            finished = True
        if not finished:
            fresh.halted = {"by": by, "at": at, "reason": f"{REPAIR_REASON}: set the budgets, then unhalt"}
        body = json.dumps({"v": FORMAT, **asdict(fresh), "ledger_rev": 1}, sort_keys=True, allow_nan=False)
        _parse(body.encode("utf-8"), board_key, job)   # never write what load refuses
        if privfs.exists(d, name):
            raise LedgerExistsError(f"a ledger for job {job!r} appeared during the repair")
        privfs.write_atomic(d, name, body, durable=True)
        fresh.ledger_rev = 1
        return Repaired(fresh, archived, source, kept, lost, finished, gen if ok else None)


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
