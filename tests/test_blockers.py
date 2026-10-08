"""Generic blocker contract; expiry uses the board's injected clock."""

import datetime as dt
import os
import unittest
from unittest import mock
from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm.board.base import CloseGuard, derive_job_status

NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)


class BlockerContract:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", None, None, None, None, goal="ship")

    def test_multiple_blockers_and_audit_history(self):
        a = self.b.open_blocker("j", "question", "human", "pick a colour", created_by="Alice")
        c = self.b.open_blocker("j", "question", "@EL", "pick a format")
        self.assertEqual(derive_job_status(self.b.job_status("j"), 5, self.b.now()), "waiting")
        self.assertFalse(self.b.close_job("j", "failed", "stall", guard=CloseGuard(False, "ship")))
        self.b.comment_blocker(a.id, "blue", actor="Bob")
        self.assertTrue(self.b.resolve_blocker(a.id, "blue", actor="human"))
        self.assertFalse(self.b.resolve_blocker(a.id, "red", actor="human"))
        self.assertEqual([b.id for b in self.b.blockers("j")], [c.id])
        self.assertEqual(
            [e.event for e in self.b.blocker_events(a.id)], ["opened", "commented", "resolved"]
        )
        self.assertTrue(self.b.reopen_blocker(a.id, "correction", actor="human"))
        self.assertEqual(self.b.blocker(a.id).state, "open")
        self.assertEqual(self.b.blocker_events(a.id)[-1].event, "reopened")

    def test_expiry_default_and_overdue_are_idempotent(self):
        with mock.patch.object(self.b, "now", return_value=NOW):
            a = self.b.open_blocker(
                "j",
                "question",
                "human",
                "choose",
                until=NOW + dt.timedelta(hours=1),
                default_value="blue",
            )
            c = self.b.open_blocker(
                "j", "question", "human", "choose", until=NOW + dt.timedelta(hours=1)
            )
        with mock.patch.object(self.b, "now", return_value=NOW + dt.timedelta(hours=2)):
            self.b.sweep_expiry(0, 0)
            self.b.sweep_expiry(0, 0)
        self.assertEqual(
            (self.b.blocker(a.id).state, self.b.blocker(a.id).resolved_how), ("expired", "blue")
        )
        self.assertEqual(self.b.blocker(c.id).state, "open")
        self.assertEqual([e.event for e in self.b.blocker_events(c.id)], ["opened", "overdue"])
        self.assertEqual(
            len([m for m in self.b.recent_messages(20, "j") if "expired" in m.message]), 1
        )

    def test_decision_expiry_in_paused_and_closed_jobs(self):
        for state in ("paused", "closed"):
            for kind in ("question", "review"):
                job = f"{state}-{kind}"
                with self.subTest(state=state, kind=kind):
                    self.b.open_job(job, None, None, None, None)
                    with mock.patch.object(self.b, "now", return_value=NOW):
                        default = self.b.open_blocker(
                            job,
                            kind,
                            "human",
                            "choose",
                            until=NOW + dt.timedelta(hours=1),
                            default_value="blue",
                        )
                        overdue = self.b.open_blocker(
                            job, kind, "human", "choose", until=NOW + dt.timedelta(hours=1)
                        )
                    if state == "paused":
                        self.b.pause_job(job, "human", "pause")
                    else:
                        self.b.close_job(job, "cancelled", "cancel", forced=True)
                    with mock.patch.object(self.b, "now", return_value=NOW + dt.timedelta(hours=2)):
                        self.b.sweep_expiry(0, 0)
                        self.b.sweep_expiry(0, 0)
                    self.assertEqual(self.b.blocker(default.id).state, "expired")
                    self.assertEqual(self.b.blocker(default.id).resolved_how, "blue")
                    self.assertEqual(
                        [e.event for e in self.b.blocker_events(overdue.id)], ["opened", "overdue"]
                    )
                    self.assertEqual(self.b.blocker(overdue.id).state, "open")
                    self.assertEqual(
                        len([m for m in self.b.recent_messages(20, job) if "expired" in m.message]),
                        1,
                    )
                    self.assertEqual(
                        self.b.job_status(job).status,
                        "paused" if state == "paused" else "cancelled",
                    )

    def test_paused_wait_keeps_legacy_expiry_behavior(self):
        with mock.patch.object(self.b, "now", return_value=NOW):
            self.b.set_waiting("j", "CI", NOW + dt.timedelta(hours=1))
        wait = self.b.blockers("j")[0]
        self.b.pause_job("j", "human", "pause")
        with mock.patch.object(self.b, "now", return_value=NOW + dt.timedelta(hours=2)):
            self.b.sweep_expiry(0, 0)
        self.assertEqual(self.b.blocker(wait.id).state, "open")
        self.assertEqual([e.event for e in self.b.blocker_events(wait.id)], ["opened"])

    def test_wait_is_one_blocker_resume_preserves_question(self):
        a = self.b.open_blocker("j", "question", "human", "choose")
        self.b.set_waiting("j", "CI", NOW + dt.timedelta(hours=1))
        self.b.set_waiting("j", "review", NOW + dt.timedelta(hours=2))
        self.assertEqual(len([b for b in self.b.blockers("j") if b.kind == "wait"]), 1)
        self.b.set_waiting("j", None)
        self.assertEqual([b.id for b in self.b.blockers("j")], [a.id])

    def test_unbounded_blocker_protects_goal_stall_and_orphan(self):
        self.h.backdate_job("j", created_at=86400, activated_at=86400)
        self.b.open_blocker("j", "question", "human", "choose")
        self.assertEqual(self.b.sweep_expiry(1, 1, goal_stall_hours=1), [])
        self.assertEqual(self.b.job_status("j").status, "active")

    def test_protection_depends_on_kind_addressee_and_deadline(self):
        now = self.b.now()
        for i, (kind, addressee, offset, protected) in enumerate(
            [
                ("wait", "external", None, False),
                ("wait", "external", 7, True),
                ("wait", "external", -7, False),
                ("wait", "human", None, False),
                ("question", "human", None, True),
                ("question", "human", -7, True),
                ("review", "@EL", None, True),
                ("review", "@EL", -7, True),
                ("review", "Alice", None, True),
                ("build", "external", None, False),
                ("build", "external", 7, True),
                ("build", "external", -7, False),
            ]
        ):
            with self.subTest(kind=kind, addressee=addressee, offset=offset):
                job = f"case{i}"
                self.b.open_job(job, None, None, None, None, goal="ship")
                until = None if offset is None else now + dt.timedelta(days=offset)
                self.b.open_blocker(job, kind, addressee, "blocked", until=until)
                status = self.b.job_status(job)
                self.assertEqual(derive_job_status(status, 5, now), "waiting")
                self.assertEqual(status.protected_blockers, int(protected))
                self.assertEqual(
                    self.b.close_job(job, "failed", "stall", guard=CloseGuard(False, "ship")),
                    not protected,
                )

    def test_plugin_protection_is_generic_and_survives_plugin_absence(self):
        from pathlib import Path
        from swarm.plugins import Registry, PluginAPI
        registry = Registry(self.h.cfg, Path('/work/config.toml'))
        api = PluginAPI(registry, 'custom', registry.config_path.parent)
        api.register_blocker_kind('approval', protection='always')
        api.register_blocker_kind('timer', protection='deadline')
        self.b.plugin_registry = registry
        for kind, addressee, expected in [('approval', 'external', True), ('timer', 'human', False)]:
            job = 'custom-' + kind
            self.b.open_job(job, None, None, None, None, goal='ship')
            blocker = self.b.open_blocker(job, kind, addressee, 'pending')
            with self.h.board() as reopened:
                # Registry is absent here; the rule lives in durable storage.
                self.assertIsNone(getattr(reopened, 'plugin_registry', None))
                self.assertEqual(reopened.blocker(blocker.id).protection_rule,
                                 'always' if expected else 'deadline')
                self.assertEqual(reopened.job_status(job).protected_blockers, int(expected))
                self.assertEqual(reopened.close_job(job, 'failed', 'stall',
                                 guard=CloseGuard(False, 'ship')), not expected)
            if expected:
                self.b.resolve_blocker(blocker.id, 'approved')
                self.assertTrue(self.b.close_job(job, 'failed', 'stall', guard=CloseGuard(False, 'ship')))

    def test_unregistered_kind_has_no_name_specific_protection(self):
        for kind in ('question', 'approval'):
            job = 'unregistered-' + kind
            self.b.open_job(job, None, None, None, None, goal='ship')
            self.b.open_blocker(job, kind, 'external', 'pending')
            self.assertEqual(self.b.job_status(job).protected_blockers, 0)
            self.assertTrue(self.b.close_job(job, 'failed', 'stall', guard=CloseGuard(False, 'ship')))

    def test_overdue_question_still_protects_goal_stall(self):
        self.h.backdate_job("j", created_at=86400, activated_at=86400)
        with mock.patch.object(self.b, "now", return_value=NOW):
            question = self.b.open_blocker(
                "j", "question", "human", "choose", until=NOW - dt.timedelta(hours=2)
            )
            self.assertEqual(self.b.sweep_expiry(1, 1, goal_stall_hours=1), [])
        self.assertEqual(self.b.blocker(question.id).state, "open")
        self.assertEqual(self.b.job_status("j").status, "active")
        self.assertEqual(
            [e.event for e in self.b.blocker_events(question.id)], ["opened", "overdue"]
        )

    def test_data_persists_across_connections(self):
        a = self.b.open_blocker("j", "question", "human", "choose")
        with self.h.board() as b:
            self.assertEqual(b.blocker(a.id), a)
            self.assertEqual(b.blocker_events(a.id)[0].event, "opened")


class MemoryBlockers(BlockerContract, unittest.TestCase):
    harness_factory = MemoryHarness


class FileBlockers(BlockerContract, unittest.TestCase):
    harness_factory = FileHarness


class SqliteBlockers(BlockerContract, unittest.TestCase):
    harness_factory = SqliteHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "throwaway Postgres required")
class PostgresBlockers(BlockerContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def test_keep_blocked_trigger_uses_registered_protection_rule(self):
        from pathlib import Path
        from swarm.plugins import Registry, PluginAPI
        registry = Registry(self.h.cfg, Path('/work/config.toml'))
        api = PluginAPI(registry, 'custom', registry.config_path.parent)
        api.register_blocker_kind('approval', protection='always')
        api.register_blocker_kind('timer', protection='deadline')
        self.b.plugin_registry = registry
        for kind, expected in [('approval', 'active'), ('timer', 'completed')]:
            job = 'trigger-' + kind
            self.b.open_job(job, None, None, None, None)
            self.b.open_blocker(job, kind, 'human', 'pending')
            self.h.conn.execute("UPDATE jobs SET status='completed', closed_by='auto' WHERE job=%s", (job,))
            self.assertEqual(self.b.job_status(job).status, expected)


class BlockerCliTests(unittest.TestCase):
    def test_parser_and_dispatch(self):
        from swarm import cli

        p = cli._parser()
        args = p.parse_args(["blockers", "--job", "j", "--all"])
        self.assertEqual(args.cmd, "blockers")
        self.assertEqual(p.parse_args(["blocker", "resolve", "12", "--how", "blue"]).id, 12)
        self.assertEqual(
            p.parse_args(["blocker", "comment", "12", "choose blue"]).text, ["choose blue"]
        )


class PluginBlockerApiTests(unittest.TestCase):
    def test_invalid_rule_rejected_and_failed_plugin_registration_removed(self):
        from pathlib import Path
        from swarm.plugins import Registry, PluginAPI
        registry = Registry({}, Path('/work/config.toml'))
        api = PluginAPI(registry, 'custom', registry.config_path.parent)
        with self.assertRaises(ValueError):
            api.register_blocker_kind('approval', protection='invalid')
        self.assertNotIn('approval', registry.blocker_kinds)
        def register(plugin_api):
            plugin_api.register_blocker_kind('approval', protection='always')
            raise RuntimeError('failed')
        registry._load_one('broken', 'synthetic', lambda: register)
        self.assertNotIn('approval', registry.blocker_kinds)
        self.assertEqual(registry.blocker_protection('approval'), 'addressed')

    def test_hooks_with_snapshot_replay_and_failure_isolation(self):
        from pathlib import Path
        from swarm import plugins, cli

        h = MemoryHarness("blocker-plugin-api")
        h.reset()
        self.addCleanup(h.close)
        with h.board() as b:
            b.open_job("j", None, None, None, None)
            reg = plugins.Registry(h.cfg, Path("/work/config.toml"))
            api = plugins.PluginAPI(reg, "demo", Path("/work"))
            seen = []
            api.register_blocker_kind(
                "question",
                display=lambda ctx, b: "Q" + str(b.id),
                expiry=lambda ctx, b: seen.append(("expiry", b.id)),
            )
            api.add_blocker_event_hook(lambda ctx, e: seen.append((e.event, e.blocker)))
            api.add_watch_pane(
                lambda ctx, job: [
                    "Questions: " + str(len(ctx.open_board().__enter__().blockers(job)))
                ]
            )
            api.add_orchestrator_lines(lambda ctx, job: ["one question for you"])
            b.plugin_registry = reg
            q = b.open_blocker("j", "question", "human", "choose")
            self.assertEqual(seen, [("opened", q.id)])
            self.assertEqual(reg.blocker_display(b, q), "Q" + str(q.id))
            self.assertEqual(reg.orchestrator_lines("j", b), ["one question for you"])
            with mock.patch.object(cli, "PLUGINS", reg):
                view = {"offset": 0, "wrap": False, "all_agents": True}
                frame = lambda board: cli._watch_frame(board, "j", 1, False, False, view)
                snap = cli._take_snapshot(b, frame, view, "j")
                with mock.patch.object(
                    b, "blockers", side_effect=AssertionError("live query during replay")
                ):
                    self.assertIn("Questions: 1", "\n".join(frame(cli._Replay(snap))))
            reg.blocker_expired(b, q)
            self.assertIn(("expiry", q.id), seen)
            api.add_blocker_event_hook(lambda ctx, e: 1 / 0)
            from contextlib import redirect_stderr
            from io import StringIO

            warnings = StringIO()
            with redirect_stderr(warnings):
                self.assertTrue(b.resolve_blocker(q.id, "blue"))
            self.assertIn("plugin demo: blocker event hook failed: ZeroDivisionError",
                          warnings.getvalue())
            self.assertIn(("resolved", q.id), seen)
            self.assertEqual(b.blocker(q.id).state, "resolved")


class MigrationContract:
    def test_schema_15_and_16_upgrade_keeps_wait_and_all_data(self):
        from swarm.board import setup_board, SCHEMA_VERSION
        from support import SMALL_POOL

        for version in (15, 16):
            with self.subTest(version=version):
                self.h.reset()
                with self.h.board() as b:
                    b.open_job("j", "description", None, None, "human", goal="ship")
                    b.set_job_data("j", "demo.setting", "kept")
                    b.post("j", "swarm", "kept message")
                until = NOW + dt.timedelta(hours=1)
                if self.h.name == "postgres":
                    self.h.conn.execute(
                        "UPDATE jobs SET waiting_on=%s, waiting_since=%s, waiting_until=%s WHERE job=%s",
                        ("CI", NOW, until, "j"),
                    )
                else:
                    self.h.update_job("j", waiting_on="CI", waiting_since=NOW, waiting_until=until)
                if self.h.name in ("memory", "file"):
                    with self.h.store.lock:
                        self.h.store.blockers = []
                        self.h.store.blocker_events = []
                        self.h.store.schema_version = version
                    if self.h.name == "file":
                        (self.h.root / "board" / "schema_version").write_text(str(version) + "\n")
                elif self.h.name == "sqlite":
                    c = self.h._db()
                    c.execute("DROP TABLE blocker_events")
                    c.execute("DROP TABLE blockers")
                    c.execute(f"PRAGMA user_version={version}")
                    if version == 15:
                        c.execute("ALTER TABLE jobs DROP COLUMN plugin_data")
                else:
                    c = self.h.conn
                    c.execute("DROP TABLE blocker_events")
                    c.execute("DROP TABLE blockers CASCADE")
                    c.execute(
                        "UPDATE board_meta SET value=%s WHERE key='schema_version'", (str(version),)
                    )
                    if version == 15:
                        c.execute("ALTER TABLE jobs DROP COLUMN plugin_data")
                if self.h.name == "postgres":
                    import psycopg
                    from swarm.board import postgres

                    install = postgres._install_schema
                    calls = []

                    def retry_install(conn, settings):
                        calls.append(1)
                        if len(calls) == 1:
                            raise psycopg.errors.DeadlockDetected("injected migration contention")
                        return install(conn, settings)

                    with mock.patch.object(
                        postgres, "_install_schema", retry_install
                    ), mock.patch.object(postgres.time, "sleep"):
                        setup_board(self.h.cfg, SMALL_POOL)
                    self.assertEqual(len(calls), 2)
                else:
                    setup_board(self.h.cfg, SMALL_POOL)
                setup_board(self.h.cfg, SMALL_POOL)
                with self.h.board() as b:
                    rows = b.blockers("j")
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(
                        (rows[0].kind, rows[0].reason, rows[0].until, rows[0].created_at),
                        ("wait", "CI", until, NOW),
                    )
                    self.assertEqual(len(b.blocker_events(rows[0].id)), 1)
                    self.assertEqual(b.recent_messages(10, "j")[0].message, "kept message")
                    self.assertEqual(b.job_status("j").description, "description")
                    if version == 16:
                        self.assertEqual(b.job_data("j")["demo.setting"], "kept")
                self.assertEqual(SCHEMA_VERSION, 20)

    def test_main_schema_17_auto_upgrades_to_19_with_blockers(self):
        """Main's schema 17 has plugin_data and status views, but no blocker storage."""
        from pathlib import Path
        from swarm.board import backend_class, ensure_initialized, SCHEMA_VERSION
        from swarm.board import autoinit
        from support import home_env
        import tempfile

        self.h.reset()
        fixtures = Path(__file__).parent / "fixtures"
        if self.h.name == "memory":
            # Main FileBoard uses main MemoryBoard's setup and serializes its state.
            from swarm.board.file import loads
            state = loads((fixtures / "schema17_state.json").read_text())
            with self.h.store.lock:
                for key in ("pool", "jobs", "agents", "routes", "restarts", "next_restart_id",
                            "pauses", "next_pause_id", "message_max_chars", "next_id"):
                    setattr(self.h.store, key, state[key])
                self.h.store.messages = [loads(line) for line in
                    (fixtures / "schema17_messages.jsonl").read_text().splitlines()]
                self.h.store.blockers = []
                self.h.store.blocker_events = []
                self.h.store.schema_version = 17
        elif self.h.name == "file":
            board_dir = self.h.root / "board"
            for name in ("state.json", "messages.jsonl"):
                source = "schema17_state.json" if name == "state.json" else "schema17_messages.jsonl"
                (board_dir / name).write_text((fixtures / source).read_text())
            (board_dir / "schema_version").write_text("17\n")
        elif self.h.name == "sqlite":
            self.h.close()
            self.h.conn = None
            self.h.path.parent.mkdir(parents=True, exist_ok=True)
            self.h._db().executescript((fixtures / "schema17_sqlite.sql").read_text())
            self.assertEqual(self.h._db().execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='blockers'"
            ).fetchone()[0], 0)
        else:
            c = self.h.conn
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            c.execute((fixtures / "schema17_postgres.sql").read_text())
            c.execute("SET search_path TO public")
            self.assertIsNone(c.execute("SELECT to_regclass('public.blockers')").fetchone()[0])
        self.assertEqual(backend_class(self.h.cfg).schema_version(self.h.cfg), 17)
        with tempfile.TemporaryDirectory(prefix="swarm-schema18-", dir=os.environ.get("TMPDIR")) as tmp:
            with mock.patch.dict(os.environ, {**home_env(Path(tmp)), "SWARM_AUTO_INIT": "1"}):
                stamp = autoinit.stamp_path(self.h.cfg)
                if stamp is not None:
                    stamp.unlink(missing_ok=True)
                self.assertEqual(ensure_initialized(self.h.cfg).action, "initialized")
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(backend_class(self.h.cfg).schema_version(self.h.cfg), 20)
        with self.h.board() as b:
            self.assertEqual(b.blockers("j"), [])
            self.assertEqual(b.job_data("j"), {"demo.setting": "kept"})
            self.assertEqual(b.recent_messages(10, "j")[0].message, "message from schema 17")
            blocker = b.open_blocker("j", "review", "@EL", "new blocker")
            self.assertEqual([row.id for row in b.blockers("j")], [blocker.id])
            self.assertEqual([event.event for event in b.blocker_events(blocker.id)], ["opened"])

    def test_main_schema_18_auto_upgrades_to_19_preserving_blockers(self):
        """Main c206c89 fixtures include an open question and its audit history."""
        from pathlib import Path
        from swarm.board import backend_class, ensure_initialized, SCHEMA_VERSION
        from swarm.board import autoinit
        from support import home_env
        import tempfile

        self.h.reset()
        fixtures = Path(__file__).parent / "fixtures"
        if self.h.name == "memory":
            # Main FileBoard uses main MemoryBoard's setup and serializes its state.
            from swarm.board.file import loads
            state = loads((fixtures / "schema18_state.json").read_text())
            with self.h.store.lock:
                for key in ("pool", "jobs", "agents", "routes", "restarts", "next_restart_id",
                            "pauses", "next_pause_id", "message_max_chars", "next_id",
                            "blockers", "blocker_events", "next_blocker_id", "next_blocker_event_id"):
                    setattr(self.h.store, key, state[key])
                self.h.store.messages = [loads(line) for line in
                    (fixtures / "schema18_messages.jsonl").read_text().splitlines()]
                self.h.store.schema_version = 18
        elif self.h.name == "file":
            board_dir = self.h.root / "board"
            for name in ("state.json", "messages.jsonl"):
                source = "schema18_state.json" if name == "state.json" else "schema18_messages.jsonl"
                (board_dir / name).write_text((fixtures / source).read_text())
            (board_dir / "schema_version").write_text("18\n")
        elif self.h.name == "sqlite":
            self.h.close()
            self.h.conn = None
            self.h.path.parent.mkdir(parents=True, exist_ok=True)
            self.h._db().executescript((fixtures / "schema18_sqlite.sql").read_text())
            self.assertEqual(self.h._db().execute("SELECT count(*) FROM blockers").fetchone()[0], 1)
            # the one schema 20 column, so the current code can read this snapshot before the upgrade
            self.h._db().execute("ALTER TABLE agents ADD COLUMN title TEXT")
        else:
            c = self.h.conn
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            c.execute((fixtures / "schema18_postgres.sql").read_text())
            c.execute("SET search_path TO public")
            self.assertEqual(c.execute("SELECT count(*) FROM blockers").fetchone()[0], 1)
        self.assertEqual(backend_class(self.h.cfg).schema_version(self.h.cfg), 18)
        # SQLite refuses opening an older schema; read its pre-upgrade snapshot
        # using the fixture's version, without running setup or changing storage.
        from contextlib import nullcontext
        old_version = (mock.patch("swarm.board.sqlite.SCHEMA_VERSION", 18)
                       if self.h.name == "sqlite" else nullcontext())
        with old_version, self.h.board() as b:
            before = (b.blockers("j"), b.blocker_events(1), b.job_status("j"),
                      b.recent_messages(10, "j"))
        with tempfile.TemporaryDirectory(prefix="swarm-schema19-", dir=os.environ.get("TMPDIR")) as tmp:
            with mock.patch.dict(os.environ, {**home_env(Path(tmp)), "SWARM_AUTO_INIT": "1"}):
                stamp = autoinit.stamp_path(self.h.cfg)
                if stamp is not None:
                    stamp.unlink(missing_ok=True)
                self.assertEqual(ensure_initialized(self.h.cfg).action, "initialized")
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(backend_class(self.h.cfg).schema_version(self.h.cfg), 20)
        from swarm.board import setup_board
        from support import SMALL_POOL
        for _ in range(2):  # automatic migration, then idempotent setup
            with self.h.board() as b:
                self.assertEqual((b.blockers("j"), b.blocker_events(1), b.job_status("j"),
                                  b.recent_messages(10, "j")), before)
                self.assertEqual(b.job_data("j"), {"demo.setting": "kept"})
                self.assertEqual([j.job for j in b.session_shown_jobs("S18")], ["j"])
                self.assertEqual(derive_job_status(b.job_status("j"), 5, b.now()), "waiting")
                self.assertEqual((b.job_status("j").open_blockers,
                                  b.job_status("j").protected_blockers), (1, 1))
            setup_board(self.h.cfg, SMALL_POOL)
        with self.h.board() as b:
            blocker = b.open_blocker("j", "review", "@EL", "new blocker")
            self.assertGreater(blocker.id, 1)
            self.assertEqual([event.event for event in b.blocker_events(blocker.id)], ["opened"])

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()


class MemoryMigration(MigrationContract, unittest.TestCase):
    harness_factory = MemoryHarness


class FileMigration(MigrationContract, unittest.TestCase):
    harness_factory = FileHarness


class SqliteMigration(MigrationContract, unittest.TestCase):
    harness_factory = SqliteHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "throwaway Postgres required")
class PostgresMigration(MigrationContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


from test_routing import RoutingEnv


class BlockerCommandFlow(RoutingEnv):
    def test_resolve_comment_status_and_resume(self):
        self.activate("J")
        with self.board() as b:
            q = b.open_blocker("J", "question", "human", "choose blue")
        self.assertEqual(self.cli("wait", "--job", "J", "--on", "CI")[0], 0)
        self.assertEqual(self.cli("resume", "--job", "J")[0], 0)
        rc, out, err = self.cli("blockers", "--job", "J", "--open")
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("question -> human: choose blue", out)
        rc, out, err = self.cli("status", "--job", "J", "--no-color")
        self.assertEqual(rc, 0)
        self.assertIn("blockers", out)
        self.assertIn("question -> human", out)
        self.assertEqual(self.cli("blocker", "comment", str(q.id), "blue please")[0], 0)
        self.assertEqual(self.cli("blocker", "resolve", str(q.id), "--how", "blue")[0], 0)
        with self.board() as b:
            self.assertEqual(
                [e.event for e in b.blocker_events(q.id)], ["opened", "commented", "resolved"]
            )
            self.assertEqual(b.blocker(q.id).resolved_by, "human")
        self.assertIn("[resolved]", self.cli("blockers", "--job", "J", "--all")[1])
        self.assertEqual(self.cli("blocker", "resolve", "999999")[0], 1)


class OrchestratorHookApi(unittest.TestCase):
    def test_only_bound_session_jobs_receive_plugin_lines(self):
        from swarm import hooks, plugins
        from pathlib import Path

        h = MemoryHarness("orchestrator-blocker-hook")
        h.reset()
        self.addCleanup(h.close)
        reg = plugins.Registry(h.cfg, Path("/work/config.toml"))
        plugins.PluginAPI(reg, "demo", Path("/work")).add_orchestrator_lines(
            lambda ctx, job: [f"{job}: one pending question"]
        )
        from types import SimpleNamespace
        lease = SimpleNamespace(allowed=True)
        with mock.patch.dict(hooks._CURRENT, {"lease": lease}), mock.patch.object(
            hooks,
            "_markers",
            return_value=[
                {"job": "mine", "session_id": "s"},
                {"job": "other", "session_id": "other"},
            ],
        ), mock.patch.object(plugins.Registry, "load", return_value=reg), mock.patch.object(
            hooks, "_out"
        ) as out:
            hooks._orchestrator_plugin_lines("turn", h.cfg, "s")
            out.assert_called_once_with("PreToolUse", "mine: one pending question")
            self.assertFalse(lease.allowed)
            out.reset_mock()
            hooks._orchestrator_plugin_lines("done", h.cfg, "s")
            out.assert_not_called()


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"), "throwaway Postgres required")
class PostgresBlockerView(unittest.TestCase):
    def test_view_lists_each_open_blocker_and_old_clients_cannot_auto_close(self):
        h = PostgresHarness(os.environ["SWARM_TEST_CONFIG"])
        h.reset()
        self.addCleanup(h.close)
        with h.board() as b:
            b.open_job("j", None, None, None, None)
            one = b.open_blocker(
                "j", "question", "human", "choose", until=b.now() - dt.timedelta(days=7)
            )
            two = b.open_blocker("j", "question", "@EL", "review")
            shown, rows = h.conn.execute(
                "SELECT shown_status, blockers FROM job_status WHERE job=%s", ("j",)
            ).fetchone()
            self.assertEqual(shown, "waiting")
            self.assertEqual(
                h.conn.execute(
                    "SELECT protected_blockers FROM job_status WHERE job='j'"
                ).fetchone()[0],
                2,
            )
            self.assertEqual([r["id"] for r in rows], [one.id, two.id])
            h.conn.execute(
                "UPDATE jobs SET status='failed', closed_by='auto', outcome='auto-closed: no progress for 1 h' WHERE job='j'"
            )
            self.assertEqual(b.job_status("j").status, "active")
