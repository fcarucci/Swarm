"""Per-seat questions in drive jobs (spec 3.10, Task 12): the plugin's drive defaults and the goal
job --default rule, the core seat/generation binding, the generic sweep's skip of drive questions
([RR3]), and the supervisor passes' per-seat gate (stuck, candidates, orphans)."""
from __future__ import annotations

import datetime as dt
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from support import SANDBOX, ROOT, add_backend_variants, drive_backends, home_env  # noqa: F401  (puts lib/ on sys.path)

from swarm import plugins
from swarm.board import STUCK_PREFIX
from swarm.board.autoinit import store_key
from swarm.board.blockers import open_question_seats
from swarm.supervisor import command, ledger, orphans, stuck
from swarm.supervisor import settings as sup_settings

PLUGIN = ROOT / "skills" / "ask-answer" / "swarm_plugin.py"
NOW = dt.datetime(2026, 10, 5, 12, tzinfo=dt.timezone.utc)
ACTIVATED = "2026-10-10T00:00:00+00:00"
HOUR = dt.timedelta(hours=1)


class QuestionEnv(unittest.TestCase):
    """Two jobs on a real memory board: J is a drive job (board claim, no ledger), K is a goal job
    that is not driven. The host's private dir is a temporary HOME, so ledgers never touch the real one."""
    harness_factory = staticmethod(drive_backends()["memory"])    # variants below swap it per backend

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=SANDBOX, prefix="q-home-"))
        env = mock.patch.dict(os.environ, home_env(self.home))
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("drive_questions_plugin", PLUGIN)
        self.p = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.p)
        self.h = self.harness_factory(self.id().rsplit(".", 1)[0])
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", None, None, None, None, goal="ship")
        self.b.open_job("K", None, None, None, None, goal="ship")
        self.assertTrue(self.b.set_job_data("J", "drive.enabled", "1"))
        self.b.allocate_name("eng-a", "J", role="engineer")
        self.b.allocate_name("eng-b", "J", role="engineer")
        self.b.allocate_name("k-asker", "K", role="engineer")
        self.a = self.b.active_agent_name("eng-a")
        self.bn = self.b.active_agent_name("eng-b")
        self.kasker = self.b.active_agent_name("k-asker")
        self.ctx = self.make_ctx()

    def make_ctx(self, cfg=None):
        reg = plugins.Registry(self.h.cfg if cfg is None else cfg, Path("/work/config.toml"),
                               core_commands=("doctor", "blocker"))
        self.p.register(plugins.PluginAPI(reg, "ask-answer", reg.config_path.parent))
        self.b.plugin_registry = reg
        return reg.context("ask-answer", self.b)

    def ask(self, job="J", actor=None, to="human", text="Which store?", **kw):
        return self.p.open_question(self.ctx, self.b, job, actor or self.a, to, text, **kw)

    def remaining(self, q) -> float:
        return (q.until - self.b.now()).total_seconds()

    def create_ledger(self, job="J", generation_budget=None):
        return ledger.create(store_key(self.h.cfg), job, budgets={}, activated_at=ACTIVATED)


class PluginDriveQuestions(QuestionEnv):
    def test_goal_job_question_without_default_refused(self):
        with self.assertRaisesRegex(ValueError, "default"):
            self.ask(default=None)
        self.assertEqual(self.b.blockers("J"), [], "a refused question opens nothing")
        q = self.ask(default="postgres")
        self.assertEqual(q.default_value, "postgres")

    def test_plain_goal_job_question_may_omit_default(self):
        # A goal job that is not driven keeps the old rules (spec 9: never converted silently).
        q = self.ask(job="K", actor=self.kasker)
        self.assertIsNone(q.default_value)
        self.assertIsNone(q.until)
        self.assertIsNone(q.seat)

    def test_drive_question_expires_after_question_expiry_by_default(self):
        q = self.ask(default="postgres")
        self.assertAlmostEqual(self.remaining(q), 4 * 3600, delta=60)

    def test_drive_question_expiry_follows_configured_question_expiry(self):
        cfg = dict(self.h.cfg, drive={"question_expiry": "30m"})
        self.ctx = self.make_ctx(cfg)
        q = self.ask(default="postgres")
        self.assertAlmostEqual(self.remaining(q), 30 * 60, delta=60)

    def test_explicit_expires_wins_over_question_expiry(self):
        q = self.ask(default="postgres", expires="2h")
        self.assertAlmostEqual(self.remaining(q), 2 * 3600, delta=60)

    def test_drive_question_binds_the_asking_seat_and_blocks_it_by_default(self):
        q = self.ask(default="postgres")
        blocker = self.b.blocker(q.id)
        self.assertEqual(blocker.seat, self.a)
        self.assertIsNone(blocker.generation, "no ledger on this host: no generation")
        self.assertEqual(self.p.question_data(self.ctx, self.b, q)["blocks"], self.a)

    def test_explicit_blocks_is_kept_and_the_seat_is_still_the_asker(self):
        q = self.ask(default="postgres", blocks="rendering")
        self.assertEqual(self.p.question_data(self.ctx, self.b, q)["blocks"], "rendering")
        self.assertEqual(self.b.blocker(q.id).seat, self.a)

    def test_drive_question_records_the_ledger_generation(self):
        self.create_ledger("J")
        q = self.ask(default="postgres")
        self.assertEqual(self.b.blocker(q.id).generation, 1)

    def test_non_drive_question_is_not_bound_to_a_seat(self):
        q = self.ask(job="K", actor=self.kasker)
        blocker = self.b.blocker(q.id)
        self.assertIsNone(blocker.seat)
        self.assertIsNone(blocker.generation)
        self.assertIsNone(self.p.question_data(self.ctx, self.b, q).get("blocks"))

    def test_generic_sweep_skips_drive_questions(self):
        q = self.ask(default="postgres", expires="30m")
        self.b.expire_blockers(self.b.now() + 2 * HOUR)
        self.assertEqual(self.b.blocker(q.id).state, "open", "only the driver expires a drive question")
        self.assertEqual(self.b.blocker(q.id).resolved_how, None)

    def test_generic_sweep_still_expires_a_non_drive_question(self):
        q = self.ask(job="K", actor=self.kasker, expires="30m", default="postgres")
        self.b.expire_blockers(self.b.now() + 2 * HOUR)
        self.assertEqual(self.b.blocker(q.id).state, "expired")

    def test_open_question_seats_is_the_per_seat_gate(self):
        q1 = self.ask(default="postgres", actor=self.a)
        self.ask(default="redis", actor=self.bn)
        self.assertEqual(open_question_seats(self.b, "J", None), {self.a, self.bn})
        self.p.answer_question(self.ctx, self.b, q1.id, "human", text="postgres")
        self.assertEqual(open_question_seats(self.b, "J", None), {self.bn})
        self.assertEqual(open_question_seats(self.b, "J", 2), set(), "another generation holds nothing")
        self.assertEqual(open_question_seats(self.b, "K", None), set(), "job-scoped")


class FakeBoard:
    """The board reads the supervisor passes make, with the rows a test sets."""

    def __init__(self, js, rows=(), blockers=(), data=None, restarts=()):
        self.js = js
        self.rows = list(rows)
        self._blockers = list(blockers)
        self.data = data or {}
        self._restarts = list(restarts)
        self.messages = []

    def now(self):
        return NOW

    def jobs(self, all):
        return [self.js]

    def job_status(self, job):
        return self.js

    def agents(self, job):
        return self.rows

    def restarts(self, job=None):
        return self._restarts

    def blockers(self, job=None):
        return self._blockers

    def job_data(self, job):
        return self.data

    def recent_messages(self, n, job=None):
        return self.messages[-n:]

    def verdict_details(self, job, artifact=None):
        return None


def drive_js(**kw):
    base = dict(job="J", status="active", verdict=None, supervise=True, waiting_on="Q1 -> human: which store?",
                last_activity_at=NOW - 3 * HOUR, activated_at=NOW - 3 * HOUR, created_at=NOW - 3 * HOUR,
                created_by="owner", started=0, running=0, idle=0, goal="ship", task=None, description=None)
    return NS(**{**base, **kw})


def question(seat, generation=None):
    return NS(kind="question", seat=seat, generation=generation)


def agent(name, key, *, ended=None, left=None):
    return NS(name=name, agent_key=key, ended_at=ended, left_reason=left, host="h", os_user="u",
              joined_at=NOW - 5 * HOUR, last_contact_at=ended, last_post_at=None, status="dead",
              current_tool=None)


class GateDriveQuestions(QuestionEnv):
    def test_human_question_heuristic_ignored_for_drive(self):
        self.b.post("J", self.a, "ok?", "human")
        self.assertFalse(orphans.human_question(self.b, self.b.job_status("J"), self.h.cfg))
        self.b.post("K", self.kasker, "ok?", "human")
        self.assertTrue(orphans.human_question(self.b, self.b.job_status("K"), self.h.cfg))

    def test_stuck_reason_job_wait_ignored_and_per_seat_gate_applied(self):
        bcfg, sup = {"dead_minutes": 30}, {"silent_minutes": 20}
        js = drive_js()
        a = NS(name="eng-a", ended_at=None, status="idle", current_tool=None,
               last_contact_at=NOW - 40 * dt.timedelta(minutes=1), last_post_at=None)
        self.assertIsNone(stuck.stuck_reason(a, js, NOW, bcfg, sup), "a job-level wait gates a plain job")
        self.assertEqual(stuck.stuck_reason(a, js, NOW, bcfg, sup, gate=frozenset()), "dead")
        self.assertIsNone(stuck.stuck_reason(a, js, NOW, bcfg, sup, gate=frozenset({"eng-a"})))
        self.assertEqual(stuck.stuck_reason(a, js, NOW, bcfg, sup, gate=frozenset({"eng-b"})), "dead")

    def test_quiet_seat_is_silent_not_waiting_for_drive(self):
        bcfg, sup = {"dead_minutes": 30}, {"silent_minutes": 20}
        a = NS(name="eng-a", ended_at=None, status="idle", current_tool=None,
               last_contact_at=NOW - 25 * dt.timedelta(minutes=1), last_post_at=None)
        js = drive_js()
        self.assertIsNone(stuck.stuck_reason(a, js, NOW, bcfg, sup))
        self.assertEqual(stuck.stuck_reason(a, js, NOW, bcfg, sup, gate=frozenset()), "silent")

    def test_candidates_hold_only_the_blocked_seat(self):
        js = drive_js()
        rows = [agent("eng-a", "ka", ended=NOW - HOUR, left=STUCK_PREFIX + "dead"),
                agent("eng-b", "kb", ended=NOW - HOUR, left=STUCK_PREFIX + "dead")]
        sup = sup_settings.settings({})
        with mock.patch.object(command, "_host_restarts", return_value=[]), \
                mock.patch.object(command, "enrolment_of", return_value=object()), \
                mock.patch.object(command, "_decide_for",
                                  side_effect=lambda board, cfg, sup, js, a, *r, **k: NS(
                                      decision=NS(go=True), agent=a, js=js)):
            drive = FakeBoard(js, rows, [question("eng-a")], {"drive.enabled": "1"})
            got = command.candidates(drive, self.h.cfg, sup, now=NOW)
            self.assertEqual([c.agent.name for c in got], ["eng-b"])
            plain = FakeBoard(js, rows, [question("eng-a")], {})
            self.assertEqual(command.candidates(plain, self.h.cfg, sup, now=NOW), [],
                             "a plain job waits on its job-level wait, as before")

    def test_invalid_holds_only_the_blocked_seat(self):
        js = drive_js()
        rows = [agent("eng-a", "ka", ended=NOW - HOUR, left=STUCK_PREFIX + "dead"),
                agent("eng-b", "kb", ended=NOW - HOUR, left=STUCK_PREFIX + "dead")]
        b = FakeBoard(js, rows, [question("eng-a")], {"drive.enabled": "1"})
        why, _, _ = command._invalid(b, "J", "ka")
        self.assertIn("question", why)
        why, _, a = command._invalid(b, "J", "kb")
        self.assertIsNone(why)
        self.assertEqual(a.name, "eng-b")

    def write_bad_ledger(self, job):
        import hashlib
        name = hashlib.sha256(f"{store_key(self.h.cfg)}\0{job}".encode("utf-8")).hexdigest() + ".json"
        drive_dir = sup_settings.private_dir() / "drive"
        os.makedirs(drive_dir, mode=0o700, exist_ok=True)
        (drive_dir / name).write_text("{ not a ledger")

    def test_bad_ledger_gates_the_job_as_a_drive_job(self):
        # A planted or unreadable ledger fails closed: the job is a drive job, so the plain job-level
        # heuristic never applies to it, and the coordinator is HELD (never released) by the bad ledger.
        self.b.post("K", self.kasker, "ok?", "human")
        self.write_bad_ledger("K")
        self.assertFalse(orphans.human_question(self.b, self.b.job_status("K"), self.h.cfg))
        gate = orphans.drive_gate(self.b, self.h.cfg, "K")
        self.assertIsNotNone(gate, "a bad ledger must classify the job as a drive job")
        self.assertIn("lead", gate)

    def test_bad_ledger_holds_the_asking_seat_and_the_coordinator(self):
        # Real questions carry the ledger's generation (1+). An unreadable ledger must still hold them.
        self.create_ledger("J")
        q = self.ask(default="postgres")
        self.assertEqual(self.b.blocker(q.id).generation, 1)
        self.write_bad_ledger("J")
        gate = orphans.drive_gate(self.b, self.h.cfg, "J")
        self.assertIn(self.a, gate)
        self.assertIn("lead", gate)
        self.assertNotIn(self.bn, gate, "a seat with no open question is not held")
        rows = [agent("eng-a", "ka", ended=NOW - HOUR, left=STUCK_PREFIX + "dead"),
                agent("eng-b", "kb", ended=NOW - HOUR, left=STUCK_PREFIX + "dead")]
        held = FakeBoard(drive_js(), rows, [question("eng-a", generation=1)], {"drive.enabled": "1"})
        with mock.patch.object(command, "_host_restarts", return_value=[]), \
                mock.patch.object(command, "enrolment_of", return_value=object()), \
                mock.patch.object(command, "_decide_for",
                                  side_effect=lambda board, cfg, sup, js, a, *r, **k: NS(
                                      decision=NS(go=True), agent=a, js=js)):
            got = command.candidates(held, self.h.cfg, sup_settings.settings({}), now=NOW)
        self.assertEqual([c.agent.name for c in got], ["eng-b"])

    def test_drive_view_never_creates_the_ledger_directory(self):
        self.assertFalse(os.path.lexists(sup_settings.private_dir()))
        self.assertEqual(orphans.drive_view(self.b, self.h.cfg, "J"), (True, None))
        self.assertEqual(orphans.drive_view(self.b, self.h.cfg, "K"), (False, None))
        self.assertFalse(orphans.human_question(self.b, self.b.job_status("K"), self.h.cfg))
        self.assertFalse(os.path.lexists(sup_settings.private_dir()),
                         "a read must not create the private drive directory")

    def test_wait_blocker_with_a_seat_is_not_a_question(self):
        self.b.open_blocker("J", "wait", "external", "waiting on the lab", seat=self.a)
        self.assertEqual(open_question_seats(self.b, "J", None), set())
        self.assertEqual(orphans.drive_gate(self.b, self.h.cfg, "J"), frozenset())

    def test_drive_lead_question_holds_the_orphan_coordinator(self):
        sup = sup_settings.settings({})
        rows = []
        with mock.patch("swarm.cli.OrchestratorWatch.active", return_value=False):
            js = drive_js(waiting_on=None)
            successor = FakeBoard(js, rows, [question("lead~1")], {"drive.enabled": "1"})
            self.assertFalse(orphans.eligible(successor, self.h.cfg, js, sup, NOW))
            held = FakeBoard(js, rows, [question("lead")], {"drive.enabled": "1"})
            self.assertFalse(orphans.eligible(held, self.h.cfg, js, sup, NOW))
            free = FakeBoard(js, rows, [question("eng-a")], {"drive.enabled": "1"})
            self.assertTrue(orphans.eligible(free, self.h.cfg, js, sup, NOW))
            plain = FakeBoard(js, rows, [question("lead")], {})
            self.assertTrue(orphans.eligible(plain, self.h.cfg, js, sup, NOW),
                            "a plain job's gate is the job-level wait, not a seat's question")


# one subclass per backend of support.drive_backends() beyond memory (Postgres only with SWARM_TEST_CONFIG)
add_backend_variants(globals(), PluginDriveQuestions, GateDriveQuestions)


if __name__ == "__main__":
    unittest.main()
