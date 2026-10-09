"""Shared test support: config, name pools, and per-backend harnesses.

A harness knows how to give a test an empty board with a chosen name pool, open boards on it,
and move stored timestamps into the past ("backdate"), which is the only way to exercise
retention and derived status without waiting. The Board interface itself has no such method on
purpose; harnesses reach into their backend's storage directly.
"""
from __future__ import annotations

import copy
import datetime as dt
from contextlib import contextmanager
import os
import sys
from pathlib import Path

# No automatic board setup (board/autoinit.py) unless a test turns it on: it writes stamps under
# ~/.local/state/swarm and the CLI registers hooks in the Claude settings. Env (test_hooks_cli)
# turns it on inside its private $HOME; test_autoinit tests it.
os.environ.setdefault("SWARM_AUTO_INIT", "0")
# What the calling host sets in its shell (the tests may run inside Claude Code or Codex): host
# detection and session binding must only see what a test sets itself.
HOST_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION", "CLAUDECODE", "CLAUDE_PLUGIN_ROOT",
            "CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_HOME", "PLUGIN_ROOT", "SWARM_HOST",
            "SWARM_RESUME_TOKEN")
for _var in HOST_ENV:
    os.environ.pop(_var, None)
# Never retire an install from a test: bootstrap() runs `swarm migrate` (settings.json, the old
# skill dir) unless SWARM_NO_MIGRATE=1. test_migrate turns it back on inside its private $HOME.
os.environ.setdefault("SWARM_NO_MIGRATE", "1")
# Never install or query systemd units from a test (bootstrap's supervisor-timer step).
os.environ.setdefault("SWARM_NO_SYSTEMD", "1")
# Codex setup (bootstrap --host codex) edits $CODEX_HOME/config.toml, default ~/.codex: never the
# real one from a test, even when the suite runs inside a Codex session. Tests that need a Codex
# home set CODEX_HOME to a temp dir; the rest fall back to ~/.codex under their temp $HOME.
os.environ.pop("CODEX_HOME", None)

if sys.platform == "win32":
    # The tests build their files as LF text (JSONL fixtures, scripts, configs) and compare bytes
    # and strings; Path.write_text would turn every "\n" into "\r\n" there.
    _write_text = Path.write_text

    def _lf_write_text(self, data, encoding=None, errors=None, newline="\n"):
        return _write_text(self, data, encoding=encoding, errors=errors, newline=newline)
    Path.write_text = _lf_write_text

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
LIB = ROOT / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from swarm import cli as swarm  # noqa: E402
from swarm.board import open_board, setup_board  # noqa: E402

# Live data is never touched. The old shared spool is emptied by migrate: every test
# gets a path in the suite's own temp dir instead of the real one (REAL_OLD_SPOOL is the string,
# for tests that compare against it, never a path to open).
import atexit as _atexit  # noqa: E402
import shutil as _shutil  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from swarm import bootstrap as _bootstrap  # noqa: E402

# The temp dir, resolved: on macOS it lives under /var, a symlink to /private/var, and the swarm's
# safe path handling rightly refuses symlinked components. Tests (and their child processes) get
# the real path.
_tempfile.tempdir = os.path.realpath(_tempfile.gettempdir())
os.environ["TMPDIR"] = _tempfile.tempdir

# Child processes that import this module (the file-board tests kill some with SIGKILL, so no atexit
# runs there) reuse the parent's sandbox, handed down in the environment; only the process that
# made it removes it.
if os.environ.get("SWARM_TEST_SANDBOX") and os.path.isdir(os.environ["SWARM_TEST_SANDBOX"]):
    SANDBOX = os.environ["SWARM_TEST_SANDBOX"]
else:
    SANDBOX = os.path.realpath(_tempfile.mkdtemp(prefix="swarm-suite-"))
    os.environ["SWARM_TEST_SANDBOX"] = SANDBOX
    _atexit.register(_shutil.rmtree, SANDBOX, True)
REAL_OLD_SPOOL = _bootstrap.OLD_SPOOL
_bootstrap.OLD_SPOOL = os.path.join(SANDBOX, "old-shared-spool")
_VENV = None


def abs_(path: str) -> str:
    """A POSIX-looking absolute test path made absolute on this platform too: on Windows a path
    needs a drive ("/work/x" becomes "C:\\work\\x"); on POSIX it is returned unchanged."""
    return ("C:" + path.replace("/", "\\")) if sys.platform == "win32" else path


def posix_only(reason: str):
    """Skip a test on Windows, with the reason (only for features that are POSIX-only by nature)."""
    import unittest
    return unittest.skipIf(sys.platform == "win32", reason)


def home_env(home) -> dict:
    """The environment variables that make a directory the user's home: HOME (POSIX) and
    USERPROFILE (what Windows' expanduser/Path.home read)."""
    return {"HOME": str(home), "USERPROFILE": str(home)}


def tq(value) -> str:
    """A TOML basic string for a path or text (backslashes and quotes escaped: Windows paths)."""
    import json
    return json.dumps(str(value))


def temp_venv() -> Path:
    """A throwaway venv for the tests that need one (doctor's venv check, the launcher): its own
    dir with its own requirements stamp, sharing the interpreter that runs the suite (and its
    packages) by symlink, so no test writes into a real venv (which may be another worktree's).
    It is built from the running interpreter, not from a checkout's `.venv`: CI installs the
    requirements straight into the runner's Python and has no `.venv`."""
    global _VENV
    if _VENV is None:
        import subprocess
        v = Path(SANDBOX) / "venv"
        if sys.platform == "win32":
            # Scripts/python.exe is a stand-in (a copy: no symlink privilege needed); doctor and the
            # launcher tests only look for it, none of them runs it
            (v / "Scripts").mkdir(parents=True)
            _shutil.copy(os.path.realpath(sys.executable), v / "Scripts" / "python.exe")
        else:
            (v / "bin").mkdir(parents=True)
            os.symlink(os.path.realpath(sys.executable), v / "bin" / "python")
        if sys.prefix != sys.base_prefix:       # running in a venv: mirror it
            src = Path(sys.prefix)
            (v / "pyvenv.cfg").write_text((src / "pyvenv.cfg").read_text())
            for d in ("lib", "lib64", "include"):
                if (src / d).exists() and sys.platform != "win32":
                    os.symlink(src / d, v / d)
        else:                                    # a plain interpreter with the packages installed in it
            (v / "pyvenv.cfg").write_text(
                f"home = {os.path.dirname(os.path.realpath(sys.executable))}\n"
                "include-system-site-packages = true\n")
        if sys.platform == "win32":
            from swarm.winlaunch import requirements_stamp
            req = requirements_stamp(ROOT)
        else:
            req = subprocess.run(["sh", "-c", f"cksum < '{ROOT / 'requirements.txt'}' | cut -d' ' -f1"],
                                 capture_output=True, text=True).stdout.strip()
        (v / ".swarm-requirements").write_text(req + "\n")
        _VENV = v
    return _VENV

def fake_image(seed: int, kind: str = "png", n: int = 3000) -> bytes:
    """Image-like bytes: the format's magic, then n pseudo-random bytes (incompressible)."""
    import random
    magic = {"png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8\xff\xe0", "gif": b"GIF89a",
             "webp": b"RIFF\x00\x00\x00\x00WEBPVP8 "}[kind]
    return magic + random.Random(seed).randbytes(n)


SMALL_POOL = {"simpsons": ["Homer Simpson", "Marge Simpson", "Bart Simpson"],
              "english": ["Alice", "Bob"]}


def lzma_bomb(raw_bytes: int) -> bytes:
    """A small, valid lzma stream that decompresses to `raw_bytes` zero bytes (built per MiB)."""
    import lzma
    c, chunk = lzma.LZMACompressor(preset=1), b"\0" * (1 << 20)
    out, left = [], raw_bytes
    while left > 0:
        out.append(c.compress(chunk[:min(left, len(chunk))]))
        left -= len(chunk)
    return b"".join(out) + c.flush()


def base_config(**board) -> dict:
    """Default config (no user file), memory backend unless overridden."""
    cfg = swarm.load_config(Path("/nonexistent/swarm-test-config.toml"))
    cfg["board"]["backend"] = "memory"
    cfg["board"].update(board)
    return cfg


# The backend the end-to-end tests (hooks, CLI: test_hooks_cli.Env and its subclasses) run on.
E2E_BACKEND = os.environ.get("SWARM_TEST_BACKEND", "memory")


class MemoryHarness:
    """Harness interface (every harness): name; cfg; toml (the backend's config lines, for a
    test config file: `[board] backend` is written by the caller); for_env(tmp, name), a fresh
    harness for one end-to-end test; reset(pool); board(**overrides); backdate_agent/message/
    job/route (seconds ago); update_agent/update_job (raw column values, datetimes aware UTC);
    set_available(flag): False makes opening a board raise BoardUnavailable from a
    ConnectionError; close()."""
    name = "memory"

    def __init__(self, store: str = "contract"):
        from swarm.board import memory
        self.memory = memory
        self.cfg = base_config()
        self.cfg["memory"] = {"store": store}
        self.store_name = store
        self.toml = f'[memory]\nstore = {tq(store)}\n'

    @classmethod
    def for_env(cls, tmp: Path, name: str) -> "MemoryHarness":
        return cls(store=name)

    def reset(self, pool=SMALL_POOL) -> None:
        self.store = self.memory.reset_store(self.store_name)
        setup_board(self.cfg, pool)

    def board(self, **board_overrides):
        cfg = copy.deepcopy(self.cfg)
        cfg["board"].update(board_overrides)
        return open_board(cfg)

    def _ago(self, seconds: float) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)

    # ---- planting stored bodies as a forged or corrupt store could hold them (bytes as is)
    def plant_transcript_body(self, job: str, agent_key: str, blob: bytes) -> None:
        from swarm.board.memory import transcript_key
        with self.store.lock:
            self.store.put_transcript_body(transcript_key(job, agent_key), blob)

    def plant_memory_excerpt(self, document_id: str, blob: bytes) -> None:
        from swarm.board.memory import MemoryBoard
        with self.store.lock:
            self.store.put_transcript_body(MemoryBoard._mref_key(document_id), blob)

    def backdate_agent(self, agent_key: str, **fields_seconds_ago) -> None:
        with self.store.lock:
            row = self.store.agents[agent_key]
            for field, secs in fields_seconds_ago.items():
                row[field] = self._ago(secs)
            self._recount()

    def _recount(self) -> None:
        """agents.message_count follows posts as they are made; backdating rewrites history behind it."""
        for a in self.store.agents.values():
            a["message_count"] = sum(1 for m in self.store.messages if m["job"] == a["job"]
                                     and m["agent_name"] == a["name"] and m["created_at"] >= a["joined_at"])

    def backdate_transcript(self, job: str, agent_key: str, seconds_ago: float) -> None:
        from swarm.board.memory import transcript_key
        with self.store.lock:
            self.store.transcripts[transcript_key(job, agent_key)]["captured_at"] = self._ago(seconds_ago)

    def backdate_message(self, msg_id: int, seconds_ago: float) -> None:
        with self.store.lock:
            next(m for m in self.store.messages if m["id"] == msg_id)["created_at"] = self._ago(seconds_ago)
            self._recount()

    def backdate_job(self, job: str, **fields_seconds_ago) -> None:
        with self.store.lock:
            for field, secs in fields_seconds_ago.items():
                self.store.jobs[job][field] = self._ago(secs)
                if field == "waiting_until":
                    for b in self.store.blockers:
                        if b["job"] == job and b["kind"] == "wait" and b["state"] == "open":
                            b["until"] = self._ago(secs)

    def backdate_route(self, agent_key: str, seconds_ago: float) -> None:
        with self.store.lock:
            self.store.routes[agent_key]["created_at"] = self._ago(seconds_ago)

    def backdate_event(self, event_id: int, seconds_ago: float) -> None:
        with self.store.lock:
            next(e for e in self.store.events if e["id"] == event_id)["created_at"] = self._ago(seconds_ago)
            self.store.touch()

    def backdate_bg(self, bg_id: int, **fields_seconds_ago) -> None:
        with self.store.lock:
            row = next(r for r in self.store.bg_commands if r["id"] == bg_id)
            for field, secs in fields_seconds_ago.items():
                row[field] = self._ago(secs)
            self.store.touch()

    def update_agent(self, agent_key: str, **values) -> None:
        with self.store.lock:
            self.store.agents[agent_key].update(values)

    def update_job(self, job: str, **values) -> None:
        with self.store.lock:
            self.store.jobs[job].update(values)

    def set_available(self, flag: bool) -> None:
        self.store.available = flag

    def close(self) -> None:
        pass


class FileHarness(MemoryHarness):
    """The file backend in a temp directory of its own. Its FileStore has the memory store's
    rows, so MemoryHarness's backdate_* work unchanged: `with self.store.lock:` is a
    transaction that loads the rows from disk and writes the edits back."""
    name = "file"

    def __init__(self, root: str | None = None):
        import tempfile
        from swarm.board import file
        self.file = file
        self.root = Path(root or tempfile.mkdtemp(prefix="swarm-file-", dir=os.environ.get("TMPDIR")))
        self.cfg = base_config(backend="file")
        self.cfg["file"] = {"path": str(self.root / "board")}
        self.toml = '[file]\npath = %s\n' % tq(self.root / "board")
        self.store = file.FileStore(file.board_dir(self.cfg))

    @classmethod
    def for_env(cls, tmp: Path, name: str) -> "FileHarness":
        root = tmp / "file-board"
        root.mkdir()
        return cls(str(root))

    def set_available(self, flag: bool) -> None:
        self.file.set_available(self.file.board_dir(self.cfg), flag)

    def reset(self, pool=SMALL_POOL) -> None:
        """Empty the board in place (boards already open see the empty board), then set it up."""
        from swarm.board import NAME_SOURCES
        with self.store.lock:
            s = self.store
            s.pool, s.jobs, s.agents, s.routes = {n: [] for n in NAME_SOURCES}, {}, {}, {}
            s.messages, s.next_id = [], 1
            s.transcripts = {}
            s.memory_refs = {}
            s.restarts, s.next_restart_id = [], 1
            s.pauses, s.next_pause_id = [], 1
            s.blockers, s.next_blocker_id = [], 1
            s.blocker_events, s.next_blocker_event_id = [], 1
            s.events, s.next_event_id = [], 1
            s.bg_commands, s.next_bg_id = [], 1
            s.message_max_chars = None   # setup seeds it from the config again
        setup_board(self.cfg, pool)

    def close(self) -> None:
        import shutil
        self.set_available(True)
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)


class PostgresHarness:
    """Runs the contract against a THROWAWAY Postgres database: reset() truncates every table.

    Refuses to run when the test config points at the same host+database as the user's normal
    swarm config, so it cannot wipe a production board by accident."""
    name = "postgres"

    def __init__(self, config_path: str):
        self.cfg = swarm.load_config(Path(config_path).expanduser())
        self.cfg["board"]["backend"] = "postgres"
        db = self.cfg["database"]
        live = swarm.load_config(Path(os.environ.get("SWARM_CONFIG", "~/.config/swarm/config.toml")).expanduser())
        if Path(config_path).expanduser().resolve() == Path(os.environ.get(
                "SWARM_CONFIG", "~/.config/swarm/config.toml")).expanduser().resolve() or \
                (live["database"]["host"], live["database"]["dbname"]) == (db["host"], db["dbname"]):
            raise RuntimeError("SWARM_TEST_CONFIG points at the production board database; use a throwaway one")
        # Thresholds are baked into the views at setup: the contract assumes the defaults.
        for k, v in (("idle_minutes", 5), ("dead_minutes", 30), ("tool_timeout_minutes", 60),
                     ("retention_days", 7), ("agent_stale_hours", 12)):
            self.cfg["board"][k] = v
        setup_board(self.cfg, SMALL_POOL)
        import psycopg
        self.conn = psycopg.connect(host=db["host"], port=db["port"], user=db["user"],
                                    password=self._password(db), dbname=db["dbname"],
                                    connect_timeout=db["connect_timeout"], sslmode=db["sslmode"],
                                    autocommit=True)

    @staticmethod
    def _password(db: dict) -> str | None:
        if os.environ.get("PGPASSWORD"):
            return os.environ["PGPASSWORD"]
        p = Path(db.get("password_env_file") or "/nonexistent").expanduser()
        if p.exists():
            for line in p.read_text().splitlines():
                if line.strip().startswith("PGPASSWORD="):
                    return line.split("=", 1)[1].strip().strip("'\"")
        return None

    def reset(self, pool=SMALL_POOL) -> None:
        self.conn.execute("TRUNCATE messages, agents, jobs, name_pool, agent_routes, transcripts, transcript_image_refs, "
                          "memory_ref_images, memory_refs, transcript_images, restarts, job_pauses, blocker_events, blockers, events, "
                          "bg_commands")
        with self.conn.cursor() as cur:
            cur.executemany("INSERT INTO name_pool (name, source) VALUES (%s, %s)",
                            [(n, s) for s, names in pool.items() for n in names])
        # a test may have changed the cap: back to the config's (what a new board starts with)
        from swarm.board import postgres
        cap = int(self.cfg["board"]["message_max_chars"])
        self.conn.execute(postgres._cap_statement(cap))
        self.conn.execute("INSERT INTO board_meta (key, value) VALUES ('message_max_chars', %s) "
                          "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(cap),))

    def board(self, **board_overrides):
        cfg = copy.deepcopy(self.cfg)
        cfg["board"].update(board_overrides)
        return open_board(cfg)

    def board_via(self, host: str, port: int, **database_overrides):
        """A board on the same database reached at host:port (a test proxy), with
        `[database]` overrides such as query_timeout_seconds."""
        cfg = copy.deepcopy(self.cfg)
        cfg["database"].update(host=host, port=port, **database_overrides)
        return open_board(cfg)

    def _backdate(self, table: str, key_col: str, key, fields: dict) -> None:
        from psycopg import sql
        for field, secs in fields.items():
            self.conn.execute(sql.SQL("UPDATE {} SET {} = now() - make_interval(secs => %s) WHERE {} = %s")
                              .format(sql.Identifier(table), sql.Identifier(field), sql.Identifier(key_col)),
                              (secs, key))

    def backdate_bg(self, bg_id: int, **fields_seconds_ago) -> None:
        self._backdate("bg_commands", "id", bg_id, fields_seconds_ago)

    def plant_transcript_body(self, job: str, agent_key: str, blob: bytes) -> None:
        self.conn.execute("UPDATE transcripts SET body = %s WHERE job = %s AND agent_key = %s", (blob, job, agent_key))

    def plant_memory_excerpt(self, document_id: str, blob: bytes) -> None:
        self.conn.execute("UPDATE memory_refs SET excerpt = %s WHERE document_id = %s", (blob, document_id))

    def backdate_agent(self, agent_key: str, **fields_seconds_ago) -> None:
        self._backdate("agents", "agent_key", agent_key, fields_seconds_ago)

    def update_agent(self, agent_key: str, **values) -> None:
        from psycopg import sql
        for field, value in values.items():
            self.conn.execute(sql.SQL("UPDATE agents SET {} = %s WHERE agent_key = %s")
                              .format(sql.Identifier(field)), (value, agent_key))

    def backdate_message(self, msg_id: int, seconds_ago: float) -> None:
        self._backdate("messages", "id", msg_id, {"created_at": seconds_ago})
        self.conn.execute("UPDATE agents a SET message_count = (SELECT count(*) FROM messages m WHERE m.job = a.job "
                          "AND m.agent_name = a.name AND m.created_at >= a.joined_at)")   # history rewritten

    def backdate_job(self, job: str, **fields_seconds_ago) -> None:
        self._backdate("jobs", "job", job, fields_seconds_ago)
        if "waiting_until" in fields_seconds_ago:
            self.conn.execute("UPDATE blockers SET until=now()-make_interval(secs => %s) "
                              "WHERE job=%s AND kind='wait' AND state='open'",
                              (fields_seconds_ago["waiting_until"],job))

    def shown_status(self, job: str) -> str:
        """The job_status view's own shown_status column (base.derive_job_status in SQL)."""
        return self.conn.execute("SELECT shown_status FROM job_status WHERE job = %s", (job,)).fetchone()[0]

    def backdate_route(self, agent_key: str, seconds_ago: float) -> None:
        self._backdate("agent_routes", "agent_key", agent_key, {"created_at": seconds_ago})

    def backdate_event(self, event_id: int, seconds_ago: float) -> None:
        self._backdate("events", "id", event_id, {"created_at": seconds_ago})

    def close(self) -> None:
        self.conn.close()


class SqliteHarness:
    """A throwaway SQLite board file (in `tmp`, or a fresh temp dir). reset() empties every
    table and keeps the file (and its schema); backdating is plain SQL on a side connection."""
    name = "sqlite"

    def __init__(self, tmp: Path | None = None):
        import tempfile
        from swarm.board import sqlite as sqlite_board
        self.sqlite_board = sqlite_board
        self._own_dir = tmp is None
        self.dir = Path(tempfile.mkdtemp(prefix="swarm-sqlite-", dir=os.environ.get("TMPDIR"))) \
            if tmp is None else Path(tmp)
        self.path = self.dir / "board.sqlite3"
        self.cfg = base_config(backend="sqlite")
        self.cfg["sqlite"] = {"path": str(self.path), "busy_timeout_ms": 60000}
        self.toml = f'[sqlite]\npath = {tq(self.path)}\n'
        self._unavailable = None
        self.conn = None

    @classmethod
    def for_env(cls, tmp: Path, name: str) -> "SqliteHarness":
        return cls(tmp / "db")   # its own directory: a test can make it read-only

    def _db(self):
        import sqlite3
        if self.conn is None:
            self.conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        return self.conn

    def reset(self, pool=SMALL_POOL) -> None:
        self.set_available(True)
        if self.path.exists():
            if self.sqlite_board.SqliteBoard.schema_version(self.cfg) != self.sqlite_board.SCHEMA_VERSION:
                setup_board(self.cfg, pool)   # a store from before the newest tables: add them first
            self._db().executescript("BEGIN IMMEDIATE; DELETE FROM messages; DELETE FROM agents; "
                                     "DELETE FROM events; DELETE FROM bg_commands; DELETE FROM blocker_events; DELETE FROM blockers; DELETE FROM jobs; DELETE FROM name_pool; "
                                     "DELETE FROM agent_routes; DELETE FROM transcripts; DELETE FROM restarts; DELETE FROM job_pauses; "
                                     "DELETE FROM memory_ref_images; DELETE FROM memory_refs; "
                                     "DELETE FROM transcript_image_refs; DELETE FROM transcript_images; "
                                     "DELETE FROM board_meta; COMMIT;")
        setup_board(self.cfg, pool)

    def board(self, **board_overrides):
        cfg = copy.deepcopy(self.cfg)
        cfg["board"].update(board_overrides)
        return open_board(cfg)

    def _set(self, table: str, key_col: str, key, values: dict) -> None:
        conv = self.sqlite_board._ts
        for field, value in values.items():
            if isinstance(value, dt.datetime):
                value = conv(value)
            elif isinstance(value, bool):
                value = int(value)
            self._db().execute(f"UPDATE {table} SET {field} = ? WHERE {key_col} = ?", (value, key))

    def _ago(self, seconds: float) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)

    def plant_transcript_body(self, job: str, agent_key: str, blob: bytes) -> None:
        import sqlite3
        self._db().execute("UPDATE transcripts SET body = ? WHERE job = ? AND agent_key = ?",
                           (sqlite3.Binary(blob), job, agent_key))

    def plant_memory_excerpt(self, document_id: str, blob: bytes) -> None:
        import sqlite3
        self._db().execute("UPDATE memory_refs SET excerpt = ? WHERE document_id = ?",
                           (sqlite3.Binary(blob), document_id))

    def backdate_transcript(self, job: str, agent_key: str, seconds_ago: float) -> None:
        self._db().execute("UPDATE transcripts SET captured_at = ? WHERE job = ? AND agent_key = ?",
                           (self.sqlite_board._ts(self._ago(seconds_ago)), job, agent_key))

    def backdate_agent(self, agent_key: str, **fields_seconds_ago) -> None:
        self._set("agents", "agent_key", agent_key, {f: self._ago(s) for f, s in fields_seconds_ago.items()})

    def backdate_bg(self, bg_id: int, **fields_seconds_ago) -> None:
        self._set("bg_commands", "id", bg_id, {f: self._ago(s) for f, s in fields_seconds_ago.items()})

    def backdate_message(self, msg_id: int, seconds_ago: float) -> None:
        self._set("messages", "id", msg_id, {"created_at": self._ago(seconds_ago)})
        self._db().execute(self.sqlite_board.COUNT_BACKFILL)   # history rewritten behind the counter

    def backdate_job(self, job: str, **fields_seconds_ago) -> None:
        self._set("jobs", "job", job, {f: self._ago(s) for f, s in fields_seconds_ago.items()})
        if "waiting_until" in fields_seconds_ago:
            self._db().execute("UPDATE blockers SET until=? WHERE job=? AND kind='wait' AND state='open'",
                               (self._ago(fields_seconds_ago["waiting_until"]),job))

    def backdate_route(self, agent_key: str, seconds_ago: float) -> None:
        self._set("agent_routes", "agent_key", agent_key, {"created_at": self._ago(seconds_ago)})

    def backdate_event(self, event_id: int, seconds_ago: float) -> None:
        self._set("events", "id", event_id, {"created_at": self._ago(seconds_ago)})

    def update_agent(self, agent_key: str, **values) -> None:
        self._set("agents", "agent_key", agent_key, values)

    def update_job(self, job: str, **values) -> None:
        self._set("jobs", "job", job, values)

    def set_available(self, flag: bool) -> None:
        """Unavailable: opening a board fails as an unreachable store does (BoardUnavailable
        from a ConnectionError, like the memory harness). The real "can't write the file" path
        is tested separately (test_sqlite.py)."""
        from unittest import mock
        if flag and self._unavailable is not None:
            self._unavailable.stop()
            self._unavailable = None
        elif not flag and self._unavailable is None:
            def refuse(*_a, **_k):
                msg = "sqlite board marked unavailable by the test"
                raise self.sqlite_board.BoardUnavailable(msg) from ConnectionError(msg)
            self._unavailable = mock.patch.object(self.sqlite_board, "_connect", refuse)
            self._unavailable.start()

    def close(self) -> None:
        self.set_available(True)
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self._own_dir:
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)


# backend name -> harness class, for the end-to-end tests (SWARM_TEST_BACKEND)
HARNESSES = {"memory": MemoryHarness, "file": FileHarness, "sqlite": SqliteHarness}


def e2e_harness(tmp: Path, name: str):
    """A fresh harness of E2E_BACKEND for one end-to-end test (temp dir `tmp`, unique `name`)."""
    return HARNESSES[E2E_BACKEND].for_env(tmp, name)


class ManualClock:
    """An injected monotonic clock; advance only at the boundary being tested."""
    def __init__(self, now=1000.0, step=0.0):
        self.now, self.step, self.calls = now, step, 0

    def __call__(self):
        self.calls += 1
        now = self.now
        self.now += self.step
        return now

    def advance(self, seconds):
        self.now += seconds


def assert_finishes(test, fn, timeout=30):
    """A generous deadlock guard, with worker failures re-raised in the test thread."""
    import threading
    result = {}

    def run():
        try:
            result["value"] = fn()
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    test.assertFalse(worker.is_alive(), "operation did not finish (possible deadlock)")
    if "error" in result:
        raise result["error"]
    return result.get("value")


@contextmanager
def on_lock_contention(callback):
    """Run a callback at the first real failed nonblocking lock attempt."""
    from unittest import mock
    from swarm import compat
    real, fired = compat.flock, False

    def flock(*args, **kwargs):
        nonlocal fired
        try:
            return real(*args, **kwargs)
        except BlockingIOError:
            if not fired:
                fired = True
                callback()
            raise

    with mock.patch.object(compat, "flock", side_effect=flock):
        yield


def wait_until(predicate, timeout=30, interval=0.02):
    """Poll a real condition with a generous deadlock guard, never a fixed sleep count."""
    import time
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(interval)


def join_processes(processes, timeout=60):
    """Join workers under one cleanup deadline; never leak them after a failed guard."""
    import time
    deadline = time.monotonic() + timeout
    for process in processes:
        process.join(max(0, deadline - time.monotonic()))
    stranded = [process for process in processes if process.is_alive()]
    for process in stranded:
        process.terminate()
    for process in stranded:
        process.join(30)
        if process.is_alive():
            process.kill()
            process.join(30)
    if stranded:
        raise AssertionError(f"workers did not finish: {[process.pid for process in stranded]}")
