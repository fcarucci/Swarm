"""`swarm snapshot` and its follow stream (lib/swarm/snapshot.py).

* property: the state a consumer rebuilds from the stream (the first snapshot, then only the
  deltas) equals a fresh full snapshot after every step of a random sequence of board events, on
  every backend (Postgres only when $SWARM_TEST_CONFIG names a throwaway database);
* the follow loop: LISTEN before the first fetch, keepalives, a full resync every `check_s`,
  a resync after a reconnect (nothing that happened during the outage is lost), and the degraded
  line while a standby (or a refusing pooler) forces polling, then recovery;
* the snapshot is a pure read: no write is issued, the spool and the store are untouched.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import (FileHarness, MemoryHarness, PostgresHarness, SqliteHarness, SMALL_POOL,  # noqa: F401
                     ManualClock, tq)

from swarm import cli as swarm  # noqa: E402
from swarm import snapshot as snap  # noqa: E402
from swarm.board import BoardUnavailable, ReadOnlyBoard, open_board  # noqa: E402


def comparable(s: dict) -> dict:
    """The part of a snapshot a consumer must reproduce (not gen/now/reason/degraded)."""
    return {k: s[k] for k in ("jobs", "agents", "hidden", "blockers", "messages")}


def wire(obj) -> dict:
    """What a consumer sees: the JSON round trip."""
    return json.loads(snap.dumps(obj))


class Scenario:
    """Random board events on one board; each returns after the store changed."""

    def __init__(self, h, board, rng, jobs=("A", "B", "C")):
        self.h, self.b, self.rng, self.jobs = h, board, rng, jobs
        self.keys: dict[str, str] = {}      # agent key -> job
        self.names: dict[str, str] = {}
        self.blockers: list[int] = []
        self.n = 0

    def step(self) -> str:
        r = self.rng
        ops = [self.open_job, self.join, self.post, self.post, self.post, self.tool, self.finish_tool,
               self.leave, self.blocker, self.resolve, self.close_job, self.age, self.wait]
        op = r.choice(ops)
        op()
        return op.__name__

    def open_job(self):
        job = self.rng.choice(self.jobs)
        self.b.open_job(job, f"desc {self.n}", None, self.rng.choice(["S1", "S2", None]), "tester")
        self.n += 1

    def join(self):
        live = [j.job for j in self.b.jobs()]
        if not live:
            return self.open_job()
        job = self.rng.choice(live)
        key = f"k{self.n}"
        self.n += 1
        try:
            self.names[key] = self.b.allocate_name(key, job)
            self.keys[key] = job
        except Exception:
            pass

    def _agent(self):
        return self.rng.choice(sorted(self.keys)) if self.keys else None

    def post(self):
        key = self._agent()
        if key is None:
            return self.join()
        try:
            self.b.post(self.keys[key], self.names[key], f"msg {self.n}", agent_key=key)
        except Exception:
            pass
        self.n += 1

    def tool(self):
        key = self._agent()
        if key:
            self.b.tool_started(key, "Bash")

    def finish_tool(self):
        key = self._agent()
        if key:
            self.b.tool_finished(key)

    def leave(self):
        key = self._agent()
        if key:
            self.b.leave(agent_key=key)

    def blocker(self):
        live = [j.job for j in self.b.jobs()]
        if live:
            try:
                self.blockers.append(self.b.open_blocker(self.rng.choice(live), "wait", "ci", "why").id)
            except Exception:
                pass

    def resolve(self):
        if self.blockers:
            try:
                self.b.resolve_blocker(self.blockers.pop(), "done", "tester")
            except Exception:
                pass

    def close_job(self):
        live = [j.job for j in self.b.jobs()]
        if live:
            try:
                self.b.close_job(self.rng.choice(live), "completed", "ok")
            except Exception:
                pass

    def age(self):
        key = self._agent()
        if key:
            self.h.backdate_agent(key, last_seen=self.rng.choice([60, 400, 4000]))

    def wait(self):
        live = [j.job for j in self.b.jobs()]
        if live:
            try:
                self.b.set_waiting(self.rng.choice(live), "something")
            except Exception:
                pass


class Property:
    """incremental == full, for the scope variants, after every random step."""
    harness_factory = None
    SEEDS = range(6)
    STEPS = 40

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def run_scope(self, scope: snap.Scope, seed: int):
        self.h.reset()
        b = self.h.board()
        self.addCleanup(b.close)
        reader = self.h.board()
        self.addCleanup(reader.close)
        sc = Scenario(self.h, b, random.Random(seed))
        state = snap.ViewState(scope)
        client = wire(state.reset(snap.read_view(reader, scope, 0)))
        for i in range(self.STEPS):
            op = sc.step()
            fetched = snap.read_view(reader, scope, state.cursor)
            delta = state.diff(fetched, lambda: snap.read_view(reader, scope, 0))
            if delta is not None:
                client = snap.apply_delta(client, wire(delta))
            full = wire(snap.ViewState(scope).reset(snap.read_view(reader, scope, 0)))
            self.assertEqual(comparable(client), comparable(full), f"seed {seed} step {i} after {op}")
            self.assertEqual(comparable(wire(state.snapshot())), comparable(full), f"state seed {seed} step {i}")
            # the backstop agrees with the state the stream built (on Postgres: the SQL digest
            # equals the Python one over the same rows)
            self.assertEqual(snap.remote_fingerprint(reader, scope)[0], snap.fingerprint_of_state(state),
                             f"fingerprint seed {seed} step {i} after {op}")

    def test_all_active_jobs(self):
        for seed in self.SEEDS:
            self.run_scope(snap.Scope(messages=5), seed)

    def test_named_jobs(self):
        for seed in self.SEEDS:
            self.run_scope(snap.Scope(jobs=("A", "C"), messages=3), seed)

    def test_session(self):
        for seed in self.SEEDS:
            self.run_scope(snap.Scope(sessions=("S1",), messages=4), seed)

    def test_job_and_two_sessions(self):
        for seed in self.SEEDS:
            self.run_scope(snap.Scope(jobs=("B",), sessions=("S1", "S2"), messages=2), seed)


class MemoryProperty(Property, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness("snapshot-prop"))


class FileProperty(Property, unittest.TestCase):
    harness_factory = staticmethod(lambda: FileHarness())


class SqliteProperty(Property, unittest.TestCase):
    harness_factory = staticmethod(lambda: SqliteHarness())


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway database")
class PostgresProperty(Property, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))
    SEEDS = range(4)


# ---------------------------------------------------------------------------- reducer + caps

class ReducerTests(unittest.TestCase):
    def test_delta_without_a_matching_base_gap_is_refused(self):
        base = {"type": "snapshot", "gen": 3, "jobs": [], "agents": [], "hidden": {}, "blockers": [],
                "messages": [], "cursor": 0, "scope": {"messages": 5}}
        with self.assertRaises(snap.GapError):
            snap.apply_delta(base, {"type": "delta", "gen": 5, "jobs": {}, "agents": {}, "blockers": {},
                                    "hidden": {}, "messages": [], "cursor": 0})

    def test_caps_are_reported_and_bound_the_output(self):
        h = MemoryHarness("snapshot-caps")
        h.reset()
        b = h.board()
        for i in range(snap.MAX_JOBS + 5):
            b.open_job(f"job{i:03d}", None, None, None, "t")
        view = snap.read_view(b, snap.Scope(messages=5), 0)
        full = snap.ViewState(snap.Scope(messages=5)).reset(view)
        self.assertEqual(len(full["jobs"]), snap.MAX_JOBS)
        self.assertIn("jobs", full["truncated"])

    def test_messages_clamped(self):
        self.assertEqual(snap.Scope(messages=10_000).messages, snap.MAX_MESSAGES)
        self.assertEqual(snap.Scope(messages=-3).messages, 0)


# ---------------------------------------------------------------------------- the follow loop

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


_real_read_view = snap.read_view   # unaffected by a test patching snap.read_view


class Proxy:
    """A board whose change source is scripted: `mode`, the wake-ups and an outage."""

    def __init__(self, real, clock, script):
        self._real, self.clock, self.script = real, clock, script
        self.subscribed = 0
        self.log = script.log
        self.degraded = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def change_mode(self):
        return self.script.mode

    def subscribe(self, messages_only=False):
        self.subscribed += 1
        self.log.append(("subscribe", self.script.mode))

    def scope_fingerprint(self, jobs, sessions, recent, with_messages=True):
        """What Postgres computes in one statement, from a read that is not counted."""
        scope = snap.Scope(jobs=jobs, sessions=sessions, recent_minutes=recent, messages=5 if with_messages else 0)
        f = _real_read_view(self._real, scope, 0)
        self.script.fingerprints += 1
        return snap.fingerprint_of_rows(f.jobs, f.agents, max((m["id"] for m in f.messages), default=0))

    def wait_for_change(self, timeout):
        self.clock.sleep(timeout)
        return self.script.next_wake(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


class Script:
    """wakes: a list consumed by wait_for_change; each item is a callable(board) -> bool, run in
    order (it may change the store, flip the mode or raise BoardUnavailable)."""

    def __init__(self, store_board, wakes, mode="push"):
        self.store, self.wakes, self.mode, self.log = store_board, list(wakes), mode, []
        self.fingerprints = 0

    def next_wake(self, proxy):
        if not self.wakes:
            raise StopIteration
        return self.wakes.pop(0)(proxy)


class FollowTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness("snapshot-follow")
        self.h.reset()
        self.store = self.h.board()
        self.addCleanup(self.store.close)
        self.store.open_job("J", "d", None, "S", "t")
        self.name = self.store.allocate_name("k1", "J")
        self.clock = FakeClock()
        self.lines: list[dict] = []

    def post(self, text="hi"):
        self.store.post("J", self.name, text, agent_key="k1")

    def run_follow(self, script, boards=1, **kw):
        made = []

        def factory():
            if len(made) >= boards:
                raise BoardUnavailable("down")
            p = Proxy(self.h.board(), self.clock, script)
            made.append(p)
            return p

        def stopper():
            return not script.wakes and self.clock.t > 0 and getattr(script, "done", False)

        try:
            snap.follow(factory, kw.pop("scope", snap.Scope(messages=5)), lambda d: self.lines.append(wire(d)),
                        clock=self.clock, sleep=self.clock.sleep, **kw)
        except StopIteration:
            pass
        return made

    def types(self):
        return [(l["type"], l["degraded"] if l["type"] == "degraded" else l.get("reason")) for l in self.lines]

    def test_listen_precedes_the_first_fetch_and_changes_stream_as_deltas(self):
        def w1(p):
            self.post("one")
            return True

        def w2(p):
            return False
        script = Script(self.store, [w1, w2])
        made = self.run_follow(script)
        self.assertEqual(script.log[0][0], "subscribe")   # LISTEN before the initial fetch
        self.assertEqual(made[0].subscribed, 1)
        first = self.lines[0]
        self.assertEqual((first["type"], first["reason"], first["gen"]), ("snapshot", "initial", 1))
        delta = self.lines[1]
        self.assertEqual((delta["type"], delta["gen"]), ("delta", 2))
        self.assertEqual([m["message"] for m in delta["messages"]], ["one"])
        # an unchanged wake emits nothing but (with time passing) keepalives
        self.assertTrue(all(l["type"] in ("snapshot", "delta", "ping") for l in self.lines))

    def test_keepalive_and_check_cadence(self):
        script = Script(self.store, [lambda p: False] * 30)
        self.run_follow(script, check_s=60, keepalive_s=15)
        pings = [l for l in self.lines if l["type"] == "ping"]
        elapsed = self.clock.t - 1000.0
        self.assertGreaterEqual(len(pings), int(elapsed // 15) - 1)
        self.assertGreaterEqual(script.fingerprints, 1)
        self.assertLessEqual(script.fingerprints, int(elapsed // 60) + 1)
        self.assertEqual([l["gen"] for l in self.lines], sorted(l["gen"] for l in self.lines))
        self.assertEqual([l["type"] for l in self.lines if l["type"] == "snapshot"], ["snapshot"])   # no resync

    def test_no_full_fetch_while_the_fingerprint_matches(self):
        fulls = []
        real = snap.read_view

        def counting(board, scope, after):
            if after == 0:
                fulls.append(1)
            return real(board, scope, after)

        self.post("seed")       # a cursor above 0: an incremental read is not a full one

        def change(p):
            self.post("x")
            return True
        script = Script(self.store, [change] + [lambda p: False] * 12)
        with mock.patch.object(snap, "read_view", counting):
            self.run_follow(script, check_s=30, keepalive_s=15)
        self.assertGreaterEqual(script.fingerprints, 2)
        self.assertEqual(len(fulls), 1)      # the initial one; the checks matched

    def test_a_mismatch_triggers_exactly_one_full_fetch(self):
        fulls = []
        real = snap.read_view

        def counting(board, scope, after):
            if after == 0:
                fulls.append(1)
            return real(board, scope, after)

        self.post("seed")

        def silent_change(p):        # a change no notification told us about (a lost NOTIFY)
            self.post("lost notification")
            return False
        script = Script(self.store, [lambda p: False, silent_change] + [lambda p: False] * 8)
        with mock.patch.object(snap, "read_view", counting):
            self.run_follow(script, check_s=30, keepalive_s=1000)
        repairs = [l for l in self.lines if l["type"] == "snapshot" and l["reason"] == "mismatch"]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(len(fulls), 2)      # initial + the one repair
        self.assertIn("lost notification", [m["message"] for m in repairs[0]["messages"]])

    def test_corrupted_local_state_is_repaired_within_one_interval(self):
        holder = {}

        def corrupt(p):
            st = holder["f"].state          # a simulated bug: the stream's state lost an agent and a job fact
            st.agents.clear()
            st.jobs["J"] = {**st.jobs["J"], "messages": 999}
            return False
        script = Script(self.store, [lambda p: False, corrupt] + [lambda p: False] * 8)
        made = []

        def factory():
            p = Proxy(self.h.board(), self.clock, script)
            made.append(p)
            return p
        f = snap.Follower(factory, snap.Scope(messages=5), lambda d: self.lines.append(wire(d)), 30.0, 1000.0, 2.0,
                          self.clock, self.clock.sleep, lambda: False)
        holder["f"] = f
        start = None
        try:
            f.run()
        except StopIteration:
            pass
        repair = [l for l in self.lines if l.get("reason") == "mismatch"]
        self.assertEqual(len(repair), 1)
        truth = wire(snap.ViewState(snap.Scope(messages=5)).reset(_real_read_view(self.store, snap.Scope(messages=5), 0)))
        self.assertEqual(comparable(repair[0]), comparable(truth))
        self.assertEqual(comparable(wire(f.state.snapshot())), comparable(truth))

    def test_push_mode_does_not_fetch_without_a_wake(self):
        fetches = []
        real = snap.read_view

        def counting(board, scope, after):
            fetches.append(after)
            return real(board, scope, after)
        script = Script(self.store, [lambda p: False] * 5)
        with mock.patch.object(snap, "read_view", counting):
            self.run_follow(script, check_s=10_000, keepalive_s=15)
        self.assertEqual(len(fetches), 1)      # the initial one only

    def test_reconnect_resyncs_what_happened_during_the_outage(self):
        def outage(p):
            self.post("during outage")
            raise BoardUnavailable("connection lost")
        script = Script(self.store, [lambda p: False, outage, lambda p: False])
        made = self.run_follow(script, boards=2)
        self.assertEqual(len(made), 2)
        kinds = self.types()
        self.assertIn(("degraded", True), kinds)
        reconnect = [l for l in self.lines if l["type"] == "snapshot" and l["reason"] == "reconnect"]
        self.assertEqual(len(reconnect), 1)
        self.assertIn("during outage", [m["message"] for m in reconnect[0]["messages"]])
        self.assertIn(("degraded", False), kinds)
        self.assertEqual(made[1].subscribed, 1)

    def test_failover_standby_polls_then_recovers(self):
        def standby_change(p):
            self.post("while degraded")
            return False

        def recover(p):
            p.script.mode = "push"
            return True       # board.wait_for_change returns True when the connection changed
        script = Script(self.store, [lambda p: False, standby_change, standby_change, recover, lambda p: False],
                        mode="degraded")
        self.run_follow(script, poll_s=2.0)
        kinds = self.types()
        self.assertEqual(kinds[0], ("degraded", True))      # said before the first snapshot
        self.assertEqual(kinds[1], ("snapshot", "initial"))
        texts = [m["message"] for l in self.lines if l["type"] == "delta" for m in l["messages"]]
        self.assertEqual(texts, ["while degraded", "while degraded"])      # polling found them
        i = kinds.index(("degraded", False))
        self.assertEqual(kinds[i + 1], ("snapshot", "resync"))      # re-LISTENed: a full read, not a check
        self.assertEqual(kinds.count(("degraded", True)), 1)         # said once, not per poll

    def test_poll_backend_emits_on_poll_changes_only(self):
        script = Script(self.store, [lambda p: False, lambda p: (self.post("p1"), False)[1], lambda p: False],
                        mode="poll")
        self.run_follow(script, poll_s=1.0)
        self.assertNotIn(True, [l.get("degraded") for l in self.lines])   # polling is normal here
        self.assertEqual([m["message"] for l in self.lines if l["type"] == "delta" for m in l["messages"]], ["p1"])


# ---------------------------------------------------------------------------- pure read

class WriteSpy:
    """A Postgres connection stand-in recording every statement and refusing writes."""


class PureReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-snap-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.spool = self.tmp / "spool"
        self.spool.mkdir()
        (self.spool / "pending-post.json").write_text('{"job": "J", "message": "queued"}')
        self.h = SqliteHarness(self.tmp / "db")
        self.h.dir.mkdir(parents=True, exist_ok=True)
        self.config = self.tmp / "config.toml"
        self.config.write_text(f'[board]\nbackend = "sqlite"\nspool_dir = {tq(self.spool)}\n'
                               f'[hook]\nmarker_dir = {tq(self.tmp / "markers")}\n'
                               f'[plugins]\ndisabled = ["engineering-team", "ask-answer", "ci"]\n' + self.h.toml)
        self.h.reset(SMALL_POOL)
        with self.h.board() as b:
            b.open_job("J", "d", None, "S", "t")
            b.post("J", b.allocate_name("k", "J"), "hello", agent_key="k")
        home = self.tmp / "home"
        home.mkdir()
        p = mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home), "SWARM_AUTO_INIT": "1"})
        p.start()
        self.addCleanup(p.stop)

    def tree(self):
        out = {}
        for root in (self.tmp / "db", self.spool, self.tmp / "markers", self.tmp / "home"):
            for p in sorted(root.rglob("*")) if root.exists() else ():
                if p.is_file() and not p.name.endswith(("-shm", "-wal")):   # SQLite's own WAL index files
                    st = p.stat()
                    out[str(p)] = (hashlib.sha256(p.read_bytes()).hexdigest(), st.st_mtime_ns)
        return out

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = swarm.main(["--config", str(self.config), *argv])
        return rc, out.getvalue(), err.getvalue()

    def test_snapshot_command_touches_nothing(self):
        before = self.tree()
        rc, out, err = self.cli("snapshot", "--json", "--messages", "5")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tree(), before)       # spool not flushed, store not written, no stamps
        data = json.loads(out)
        self.assertEqual(data["type"], "snapshot")
        self.assertEqual([j["job"] for j in data["jobs"]], ["J"])
        self.assertEqual([m["message"] for m in data["messages"]], ["hello"])

    def test_snapshot_board_refuses_every_write(self):
        board = snap.open_snapshot_board(swarm.load_config(self.config))
        self.addCleanup(board.close)
        with self.assertRaises(ReadOnlyBoard):
            board.post("J", "x", "nope", agent_key="k")
        with self.assertRaises(ReadOnlyBoard):
            board.open_job("Z", None, None, None, "t")

    def test_session_and_job_flags_and_bounds(self):
        rc, out, err = self.cli("snapshot", "--json", "--job", "J", "--session", "nope", "--messages", "100000")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data["scope"]["messages"], snap.MAX_MESSAGES)
        self.assertEqual(data["scope"]["jobs"], ["J"])

    def test_unreachable_board_is_one_line_and_exit_1(self):
        cfg = self.tmp / "bad.toml"
        cfg.write_text('[board]\nbackend = "sqlite"\n[sqlite]\npath = ' + tq(self.tmp / "missing" / "x.db") + "\n")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = swarm.main(["--config", str(cfg), "snapshot", "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(out.getvalue(), "")


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "set SWARM_TEST_CONFIG to a throwaway database")
class PostgresPureReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        with self.h.board() as b:
            b.open_job("J", "d", None, "S", "t")
            self.name = b.allocate_name("k", "J")
            b.post("J", self.name, "hello", agent_key="k")

    def test_no_write_statement_is_issued_and_the_server_would_refuse_one(self):
        board = snap.open_snapshot_board(self.h.cfg)
        self.addCleanup(board.close)
        seen = []
        real = board._conn.execute

        def spy(query, *a, **k):
            seen.append(str(getattr(query, "as_string", lambda c: query)(board._conn) if not isinstance(query, str) else query))
            return real(query, *a, **k)
        board._conn.execute = spy
        scope = snap.Scope(messages=5)
        full = snap.ViewState(scope).reset(snap.read_view(board, scope, 0))
        self.assertEqual(len(full["messages"]), 1)
        board.subscribe()
        board.wait_for_change(0.01)
        writes = [q for q in seen if q.lstrip().split(None, 1)[0].upper() in
                  ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP", "SELECT_INTO")]
        self.assertEqual(writes, [])
        board._conn.execute = real
        import psycopg
        with self.assertRaises(psycopg.errors.ReadOnlySqlTransaction):
            real("UPDATE jobs SET description = 'x'")

    def test_postgres_follow_standby_refuses_listen_then_recovers(self):
        """The failover simulation: LISTEN is refused (a standby, or a pooler) -> polling and the
        degraded line; once LISTEN works again the stream says so and resyncs."""
        import psycopg
        from swarm.board import postgres as pgmod
        refuse = {"on": True}
        boards = []

        def factory():
            b = snap.open_snapshot_board(self.h.cfg)
            real = b._conn.execute

            def execute(query, *a, **k):
                if isinstance(query, str) and query.startswith("LISTEN") and refuse["on"]:
                    raise psycopg.errors.ReadOnlySqlTransaction("cannot execute LISTEN during recovery")
                return real(query, *a, **k)
            b._conn.execute = execute
            boards.append(b)
            return b

        lines = []
        writer = self.h.board()
        self.addCleanup(writer.close)

        def emit(d):
            lines.append(json.loads(snap.dumps(d)))
            if d["type"] == "snapshot" and d["reason"] == "initial":
                writer.post("J", self.name, "polled", agent_key="k")     # found by polling, no NOTIFY seen
            if d["type"] == "delta":
                refuse["on"] = False        # LISTEN works from now on

        def stop():
            return any(l["type"] == "snapshot" and l["reason"] == "resync" for l in lines)
        with mock.patch.object(pgmod, "LIVE_RETRY_SECONDS", 0.0):
            snap.follow(factory, snap.Scope(messages=5), emit, poll_s=0.05, keepalive_s=1000, check_s=1000,
                        stop=stop)
        kinds = [(l["type"], l["degraded"] if l["type"] == "degraded" else l.get("reason")) for l in lines]
        self.assertEqual(kinds[0], ("degraded", True))
        self.assertIn(("degraded", False), kinds)
        self.assertEqual([m["message"] for l in lines if l["type"] == "delta" for m in l["messages"]], ["polled"])
        self.assertEqual(boards[0].change_mode, "push")


if __name__ == "__main__":
    unittest.main()
