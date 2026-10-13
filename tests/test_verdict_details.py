"""A judge's full verdict report (`swarm verdict --details`): stored with the verdict on every
backend (schema 23, jobs.verdict_details), bound to the verdict's artifact, capped at
VERDICT_DETAILS_MAX bytes, read by `swarm verdict show`, shown by `status`, and taught to judges
by their instructions and the judge gate's refusal (judges cannot write a verdict.md).

The board contract and the schema migration run on the memory, file and sqlite backends
(Postgres with SWARM_TEST_CONFIG); the CLI and hook tests on SWARM_TEST_BACKEND."""
from __future__ import annotations

import datetime as dt
import json
import os
import types
import unittest
from pathlib import Path

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness, SMALL_POOL  # noqa: F401
from test_goals import GoalEnv  # noqa: E402  (sets sys.path)

from swarm import cli as swarm, hooks, spool  # noqa: E402
from swarm.board import SCHEMA_VERSION, VERDICT_DETAILS_MAX, BoardError, setup_board  # noqa: E402

REPORT = "# Verdict\n\n1. The drill was never run.\n2. `db-2` lost writes -> data loss.\n"


class DetailsContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("j", None, None, None, "me", goal="G")
        self.judge = self.b.allocate_name("jk", "j")
        self.b.claim_judge("jk", "j")

    def test_not_met_details_are_stored_and_read_back(self):
        self.assertIsNone(self.b.verdict_details("j"))
        self.assertTrue(self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "br@abc", REPORT))
        d = self.b.verdict_details("j")
        self.assertEqual((d.details, d.verdict, d.by, d.artifact), (REPORT, "not_met", self.judge, "br@abc"))
        self.assertIsNotNone(d.at)
        self.assertEqual(self.b.job_status("j").verdict_reason, "why")

    def test_met_details_are_stored_too(self):
        self.assertTrue(self.b.record_verdict("j", self.judge, "met", "ok", None, "a1", REPORT))
        d = self.b.verdict_details("j")
        self.assertEqual((d.verdict, d.details), ("met", REPORT))

    def test_details_are_bound_to_the_artifact_of_the_verdict(self):
        self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "br@abc", REPORT)
        self.assertEqual(self.b.verdict_details("j", "br@abc").details, REPORT)
        self.assertIsNone(self.b.verdict_details("j", "br@other"))
        self.assertEqual(self.b.verdict_details("j").artifact, "br@abc")

    def test_a_verdict_without_an_artifact_is_bound_to_none(self):
        self.b.record_verdict("j", self.judge, "met", "ok", None, None, REPORT)
        self.assertIsNone(self.b.verdict_details("j").artifact)
        self.assertIsNone(self.b.verdict_details("j", "anything"))

    def test_a_later_verdict_replaces_or_clears_the_details(self):
        self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "a", "first")
        self.b.record_verdict("j", self.judge, "not_met", "again", "fix", "a", "second")
        self.assertEqual(self.b.verdict_details("j").details, "second")
        self.b.record_verdict("j", self.judge, "met", "fixed", None, "a")
        self.assertIsNone(self.b.verdict_details("j"))

    def test_blank_details_are_no_details(self):
        self.b.record_verdict("j", self.judge, "met", "ok", None, "a", "  \n\t\n")
        self.assertIsNone(self.b.verdict_details("j"))

    def test_the_size_cap_is_in_bytes_and_refuses_with_a_clear_error(self):
        edge = "x" * VERDICT_DETAILS_MAX
        self.assertTrue(self.b.record_verdict("j", self.judge, "met", "ok", None, "a", edge))
        self.assertEqual(len(self.b.verdict_details("j").details), VERDICT_DETAILS_MAX)
        for too_big in ("x" * (VERDICT_DETAILS_MAX + 1), "é" * (VERDICT_DETAILS_MAX // 2 + 1)):
            with self.assertRaisesRegex(BoardError, "verdict details are .* the limit is 65536"):
                self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "a", too_big)
        self.assertEqual(len(self.b.verdict_details("j").details), VERDICT_DETAILS_MAX)   # nothing replaced

    def test_only_the_active_judge_stores_details(self):
        worker = self.b.allocate_name("wk", "j")
        self.assertFalse(self.b.record_verdict("j", worker, "met", "trust me", None, "a", REPORT))
        self.assertIsNone(self.b.verdict_details("j"))

    def test_details_are_cleared_when_the_job_is_reopened_and_for_unknown_jobs(self):
        self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "a", REPORT)
        self.b.open_job("j", None, None, None, "me")
        self.assertIsNone(self.b.verdict_details("j"))
        self.assertIsNone(self.b.verdict_details("nope"))

    def test_details_are_cleared_when_the_goal_changes(self):
        self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "a", REPORT)
        self.b.set_job_goal("j", "another goal")
        self.assertIsNone(self.b.verdict_details("j"))

    def test_a_report_left_by_an_older_verdict_is_not_shown(self):
        """An older client updates the verdict columns only and never clears the report."""
        self.b.record_verdict("j", self.judge, "not_met", "why", "fix", "a", REPORT)
        self.assertIsNotNone(self.b.verdict_details("j"))
        self.old_client_verdict("j", "met", "newer verdict, no details")
        self.assertEqual(self.b.job_status("j").verdict, "met")
        self.assertIsNone(self.b.verdict_details("j"))
        self.assertTrue(self.b.record_verdict("j", self.judge, "met", "again", None, "a", "fresh"))
        self.assertEqual(self.b.verdict_details("j").details, "fresh")
        from swarm.board.base import WRITE_METHODS
        self.assertIn("record_verdict", WRITE_METHODS)


class MemoryDetails(DetailsContract, unittest.TestCase):
    harness_factory = MemoryHarness

    def old_client_verdict(self, job, verdict, reason):
        s = self.b._s()
        with s.lock:
            s.jobs[job].update(verdict=verdict, verdict_reason=reason, verdict_next=None,
                               verdict_at=self.b.now() + dt.timedelta(seconds=1))
            s.touch()


class SqliteDetails(DetailsContract, unittest.TestCase):
    harness_factory = SqliteHarness

    def old_client_verdict(self, job, verdict, reason):
        later = (self.b.now() + dt.timedelta(seconds=1)).isoformat()
        c = self.h._db()
        c.execute("UPDATE jobs SET verdict = ?, verdict_reason = ?, verdict_next = NULL, verdict_at = ? "
                  "WHERE job = ?", (verdict, reason, later, job))
        c.commit()


class FileDetails(MemoryDetails):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresDetails(DetailsContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def old_client_verdict(self, job, verdict, reason):
        self.h.conn.execute("UPDATE jobs SET verdict = %s, verdict_reason = %s, verdict_next = NULL, "
                            "verdict_at = now() + interval '1 second' WHERE job = %s", (verdict, reason, job))


# ---- schema 23 ---------------------------------------------------------------------------------

PREVIOUS = 22   # the schema main had before this change


class Schema23Migration:
    """A board before schema 23 (no verdict_details anywhere) upgrades in place, keeps its jobs and
    verdicts, and then stores details."""
    harness_factory = None

    def setUp(self):
        self.h = self.harness_factory()
        self.addCleanup(self.h.close)
        self.h.reset()

    def test_a_previous_schema_board_upgrades_and_keeps_its_verdicts(self):
        with self.h.board() as b:
            cls = type(b)
            b.open_job("j", None, None, None, "me", goal="G")
            judge = b.allocate_name("jk", "j")
            b.claim_judge("jk", "j")
            b.record_verdict("j", judge, "not_met", "why", "fix", "a")
        self.downgrade()
        self.assertEqual(cls.schema_version(self.h.cfg), PREVIOUS)
        for run in range(2):   # the migration, then an idempotent re-run
            setup_board(self.h.cfg, SMALL_POOL)
            self.assertEqual(cls.schema_version(self.h.cfg), SCHEMA_VERSION)
            with self.h.board() as b:
                s = b.job_status("j")
                if run == 0:
                    self.assertEqual((s.verdict, s.verdict_reason, s.verdict_next), ("not_met", "why", "fix"))
                    self.assertIsNone(b.verdict_details("j"))   # a verdict from before has none
                self.assertTrue(b.record_verdict("j", judge, "met", "ok", None, "a", REPORT))
                self.assertEqual(b.verdict_details("j").details, REPORT)


class MemorySchema23(Schema23Migration, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness("schema23"))

    def downgrade(self):
        with self.h.store.lock:
            self.h.store.schema_version = PREVIOUS
            for row in self.h.store.jobs.values():
                row.pop("verdict_details", None)
                row.pop("verdict_details_at", None)


class FileSchema23(MemorySchema23):
    harness_factory = FileHarness

    def downgrade(self):
        super().downgrade()
        (Path(self.h.cfg["file"]["path"]) / "schema_version").write_text(f"{PREVIOUS}\n")


class SqliteSchema23(Schema23Migration, unittest.TestCase):
    harness_factory = SqliteHarness

    def downgrade(self):
        c = self.h._db()
        c.execute("ALTER TABLE jobs DROP COLUMN verdict_details")
        c.execute("ALTER TABLE jobs DROP COLUMN verdict_details_at")
        c.execute(f"PRAGMA user_version = {PREVIOUS}")


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresSchema23(Schema23Migration, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def downgrade(self):
        c = self.h.conn
        c.execute("ALTER TABLE jobs DROP COLUMN verdict_details")
        c.execute("ALTER TABLE jobs DROP COLUMN verdict_details_at")
        c.execute(f"UPDATE board_meta SET value = '{PREVIOUS}' WHERE key = 'schema_version'")


# ---- CLI ---------------------------------------------------------------------------------------

class VerdictDetailsCliTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()
        self.spawn_judge()
        self.judge = self.member("judge-1").name

    def verdict(self, *args, stdin=""):
        return self.cli("verdict", "--job", "J", "--as", self.judge, *args, stdin=stdin)

    def test_details_from_stdin_are_stored_shown_and_announced_in_status(self):
        rc, out, err = self.verdict("--artifact", "br@abc", "not_met", "--reason", "incomplete", "--next", "do x",
                                    "--details", "-", stdin=REPORT)
        self.assertEqual(rc, 0, err)
        rc, out, err = self.cli("verdict", "--job", "J", "show")   # no --as: anyone reads it
        self.assertEqual(rc, 0, err)
        self.assertIn("verdict not_met by " + self.judge, out)
        self.assertIn("artifact br@abc", out)
        self.assertIn("1. The drill was never run.", out)
        _, status, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("details    4 lines, `swarm verdict show --job J`", status)
        with self.board() as b:
            posted = [m.message for m in b.recent_messages(20, job="J")]
        self.assertTrue(any(m.startswith("DETAILS (4 lines): swarm verdict show --job J") for m in posted), posted)
        self.assertFalse(any("drill was never run" in m for m in posted))   # the report is not broadcast

    def test_details_from_a_file_and_for_met(self):
        path = self.tmp / "report.md"
        path.write_text(REPORT)
        rc, _, err = self.verdict("--artifact", "a1", "--details", str(path), "met", "all checked")
        self.assertEqual(rc, 0, err)
        _, out, _ = self.cli("verdict", "--job", "J", "show", "--artifact", "a1")
        self.assertIn("verdict met by", out)
        self.assertIn("db-2", out)

    def test_show_is_bound_to_the_artifact(self):
        self.verdict("--artifact", "a1", "--details", "-", "met", "ok", stdin=REPORT)
        rc, out, err = self.cli("verdict", "--job", "J", "show", "--artifact", "other")
        self.assertEqual(rc, 1)
        self.assertIn("no verdict details bound to other", err)

    def test_show_without_details_says_how_to_add_them(self):
        self.verdict("met", "ok")
        rc, _, err = self.cli("verdict", "--job", "J", "show")
        self.assertEqual(rc, 1)
        self.assertIn("--details", err)
        _, status, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertNotIn("details    ", status)

    def test_unreachable_needs_its_reason_and_report_and_judges_the_goal(self):
        rc, _, err = self.verdict("unreachable", "--reason", "no credentials")
        self.assertEqual(rc, 1)
        self.assertIn("unreachable verdict must say why", err)
        with self.board() as b:
            self.assertIsNone(b.job_status("J").verdict)
        rc, _, err = self.verdict("unreachable", "--reason", "no credentials", "--details", "-", stdin=REPORT)
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            js = b.job_status("J")
        self.assertEqual((js.verdict, js.verdict_scope, js.closed_by), ("unreachable", "goal", None))
        rc, out, _ = self.cli("verdict", "--job", "J", "show")
        self.assertEqual(rc, 0)
        self.assertIn("1. The drill was never run.", out)

    def test_a_drive_jobs_not_met_must_give_its_open_count_and_progress_is_kept(self):
        with self.board() as b:
            self.assertTrue(b.set_job_data("J", "drive.enabled", "1"))
        rc, _, err = self.verdict("--artifact", "a1", "not_met", "--reason", "r", "--next", "n")
        self.assertEqual(rc, 1)
        self.assertIn("open_count", err)
        rc, _, err = self.verdict("--artifact", "a1", "--open", "2", "--progress", "yes", "--candidate", "c" * 40,
                                  "not_met", "--reason", "r", "--next", "n")
        self.assertEqual(rc, 0, err)
        from swarm.review import artifact_verdicts
        with self.board() as b:
            rec = artifact_verdicts(b, "J")["a1"]
        self.assertEqual((rec["open"], rec["progress"], rec["candidate"], rec["scope"]),
                         (2, True, "c" * 40, "artifact"))

    def test_an_unreachable_verdict_is_refused_not_queued_when_the_board_is_down(self):
        self.h.set_available(False)
        self.addCleanup(self.h.set_available, True)
        rc, _, err = self.verdict("unreachable", "--reason", "no credentials", "--details", "-", stdin=REPORT)
        self.assertEqual(rc, 1)
        self.assertIn("not queued", err)
        self.assertEqual(list(self.spool_dir.glob("*.vrd")), [])

    def test_a_queued_drive_not_met_without_open_count_is_refused_on_delivery(self):
        with self.board() as b:
            self.assertTrue(b.set_job_data("J", "drive.enabled", "1"))
        spool.spool_verdict(self.cfg, "J", self.judge, "not_met", "why", "fix", "a1")
        with self.board() as b:
            self.assertEqual(spool.flush_spool(b, self.cfg), 0)
            self.assertIsNone(b.job_status("J").verdict)
            posted = [m.message for m in b.recent_messages(20, job="J")]
        self.assertTrue(any(m.startswith("verdict refused: ") and "open_count" in m for m in posted), posted)
        self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), 1)

    def test_a_queued_unreachable_carries_its_details_and_scope(self):
        spool.spool_verdict(self.cfg, "J", self.judge, "unreachable", "cannot", None, None, REPORT, scope="goal",
                            generation=1)
        with self.board() as b:
            self.assertEqual(spool.flush_spool(b, self.cfg), 1)
            js = b.job_status("J")
            self.assertEqual((js.verdict, js.verdict_scope, js.verdict_generation), ("unreachable", "goal", 1))
            self.assertEqual(b.verdict_details("J").details, REPORT)

    def test_a_queued_unreachable_without_details_goes_to_bad(self):
        spool.spool_verdict(self.cfg, "J", self.judge, "unreachable", "cannot", scope="goal")
        with self.board() as b:
            self.assertEqual(spool.flush_spool(b, self.cfg), 0)
            self.assertIsNone(b.job_status("J").verdict)
            posted = [m.message for m in b.recent_messages(20, job="J")]
        self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), 1)
        self.assertFalse(any("verdict refused" in m for m in posted), posted)   # rejected on load, not on the board

    def test_a_queued_verdict_with_a_malformed_typed_field_goes_to_bad(self):
        for field, value in (("open_count", -1), ("generation", 0), ("progress", "yes"), ("scope", "whole-job")):
            with self.subTest(field=field):
                spool.spool_verdict(self.cfg, "J", self.judge, "met", "ok", **{field: value})
        with self.board() as b:
            self.assertEqual(spool.flush_spool(b, self.cfg), 0)
            self.assertIsNone(b.job_status("J").verdict)
        self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), 4)

    def test_typed_verdict_fields_survive_the_offline_spool(self):
        with self.board() as b:
            self.assertTrue(b.set_job_data("J", "drive.enabled", "1"))
        self.h.set_available(False)
        self.addCleanup(self.h.set_available, True)
        rc, _, err = self.verdict("--artifact", "a1", "--scope", "goal", "--open", "3", "--progress", "yes",
                                  "--candidate", "c" * 40, "--req", "sha256:r",
                                  "not_met", "--reason", "r", "--next", "n")
        self.assertEqual(rc, 0, err)
        self.h.set_available(True)
        with self.board() as b:
            self.assertEqual(spool.flush_spool(b, self.cfg), 1)
            js = b.job_status("J")
            self.assertEqual((js.verdict, js.verdict_scope, js.verdict_candidate, js.verdict_req),
                             ("not_met", "goal", "c" * 40, "sha256:r"))
            from swarm.review import artifact_verdicts
            rec = artifact_verdicts(b, "J")["a1"]
        self.assertEqual((rec["open"], rec["progress"], rec["scope"]), (3, True, "goal"))

    def test_a_spooled_goal_verdict_keeps_candidate_and_req(self):
        """Task 2's surviving spool mutant: both fields are in the queued file and on the delivered job."""
        with self.board() as b:
            self.assertTrue(b.set_job_data("J", "drive.enabled", "1"))
        path = spool.spool_verdict(self.cfg, "J", self.judge, "met", "goal met", scope="goal", generation=1,
                                   candidate="c" * 40, req="sha256:r")
        queued = json.loads(path.read_text())
        self.assertEqual((queued["candidate"], queued["req"], queued["scope"]), ("c" * 40, "sha256:r", "goal"))
        self.assertEqual(spool.typed_verdict_fields("goal", 1, None, None, "c" * 40, "sha256:r"),
                         {"scope": "goal", "generation": 1, "candidate": "c" * 40, "req": "sha256:r"})
        only_req = json.loads(spool.spool_verdict(self.cfg, "J", self.judge, "met", "again", scope="goal",
                                                  req="sha256:r").read_text())
        self.assertNotIn("candidate", only_req)
        with self.board() as b:
            self.assertEqual(spool.flush_spool(b, self.cfg), 2)
            js = b.job_status("J")
        self.assertEqual((js.verdict, js.verdict_scope, js.verdict_req), ("met", "goal", "sha256:r"))
        self.assertIsNone(js.verdict_candidate)       # the newer queued verdict (req only) is the latest

    def test_an_oversized_report_is_refused_clearly_and_nothing_is_recorded(self):
        rc, _, err = self.verdict("not_met", "--reason", "r", "--next", "n", "--details", "-",
                                  stdin="x" * (VERDICT_DETAILS_MAX + 1))
        self.assertEqual(rc, 1)
        self.assertIn(f"over {VERDICT_DETAILS_MAX} bytes", err)
        self.assertIsNone(self.job().verdict)

    def test_an_empty_or_missing_report_is_refused(self):
        rc, _, err = self.verdict("met", "ok", "--details", "-", stdin="  \n")
        self.assertEqual((rc, "is empty" in err), (1, True))
        rc, _, err = self.verdict("met", "ok", "--details", str(self.tmp / "nope.md"))
        self.assertEqual((rc, "cannot read" in err), (1, True))
        self.assertIsNone(self.job().verdict)

    def test_not_met_still_needs_reason_and_next_even_with_details(self):
        rc, _, err = self.verdict("not_met", "--details", "-", stdin=REPORT)
        self.assertEqual(rc, 1)
        self.assertIn("--reason", err)

    def test_a_queued_verdict_carries_its_details(self):
        spool.spool_verdict(self.cfg, "J", self.judge, "not_met", "why", "fix", "a1", REPORT)
        with self.board() as b:
            spool.flush_spool(b, self.cfg)
            self.assertEqual(b.verdict_details("J", "a1").details, REPORT)

    def test_a_queued_verdict_without_details_still_loads(self):
        spool.spool_verdict(self.cfg, "J", self.judge, "met", "ok")
        with self.board() as b:
            spool.flush_spool(b, self.cfg)
            self.assertEqual(b.job_status("J").verdict, "met")
            self.assertIsNone(b.verdict_details("J"))


# ---- hooks and briefs --------------------------------------------------------------------------

class JudgeTeachingTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()

    def test_the_gate_refusal_for_a_write_names_details(self):
        self.spawn_judge()
        out = self.hook("turn", agent_id="judge-1", tool_name="Write",
                        tool_input={"file_path": "verdict.md", "content": REPORT},
                        transcript_path=self.main_transcript())["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn("Judges only judge", reason)
        self.assertIn("--details", reason)
        self.assertIn("verdict.md", reason)

    def test_a_verdict_with_a_heredoc_report_is_not_mistaken_for_a_write(self):
        self.spawn_judge()
        judge = self.member("judge-1").name
        command = ("swarm verdict --job J --artifact a1 not_met --reason r --next n --details - <<'EOF'\n"
                   "# Report\n1. a -> b\nrm -rf x; git push origin main\nEOF\n")
        out = self.hook("turn", agent_id="judge-1", tool_name="Bash", tool_input={"command": command},
                        transcript_path=self.main_transcript())
        self.assertNotEqual(((out or {}).get("hookSpecificOutput") or {}).get("permissionDecision"), "deny", out)
        rewritten = hooks.own_name_verdict_command(command, judge, "J")
        self.assertIn(f"--as '{judge}'", rewritten)
        self.assertIn("1. a -> b", rewritten)   # the body is untouched


class SeatQuestionIdentityTests(GoalEnv):
    """`swarm ask` and `swarm answer` run as the caller's own agent, as `swarm verdict` does: the hook adds
    the caller's --key (QA live-unreachable: a driver-launched lead got "no board identity")."""

    def setUp(self):
        super().setUp()
        self.activate_goal()
        self.spawn_worker("lead-agent")

    def rewritten(self, command):
        out = self.hook("turn", agent_id="lead-agent", tool_name="Bash", tool_input={"command": command},
                        transcript_path=self.main_transcript()) or {}
        return ((out.get("hookSpecificOutput") or {}).get("updatedInput") or {}).get("command")

    def test_ask_without_key_runs_with_the_callers_own_key(self):
        cmd = self.rewritten("swarm ask --job J --to owner --default unreachable 'Rule it?'")
        self.assertEqual(cmd, "swarm ask --key lead-agent --job J --to owner --default unreachable 'Rule it?'")

    def test_another_key_is_replaced_by_the_callers_own(self):
        cmd = self.rewritten("swarm ask --job J --key lead-gen1 --to owner --default x 'q?'")
        self.assertEqual(cmd, "swarm ask --job J --key lead-agent --to owner --default x 'q?'")
        cmd = self.rewritten("swarm answer 3 --key=other go")
        self.assertEqual(cmd, "swarm answer 3 --key=lead-agent go")

    def test_answer_gets_the_key_and_an_ask_for_another_job_is_left_alone(self):
        self.assertEqual(self.rewritten("swarm answer Q3 --option a"), "swarm answer --key lead-agent Q3 --option a")
        self.assertIsNone(hooks.own_key_question_command("swarm ask --job K --to owner q", "lead-agent", "J"))
        self.assertIsNone(hooks.own_key_question_command("swarm ask --job J --key lead-agent --to owner q",
                                                         "lead-agent", "J"))
        self.assertIsNone(hooks.own_key_question_command("echo swarm ask --job J q", "lead-agent", "J"))


class FixWorkerBriefTests(unittest.TestCase):
    def action(self, verdict):
        job = types.SimpleNamespace(job="J", goal="g", task="t", description="d")
        handoff = types.SimpleNamespace(id=1, artifact="b@" + "a" * 40, agent_name="X", summary="s")
        return types.SimpleNamespace(job=job, handoff=handoff, role="worker", verdict=verdict)

    def board(self, details):
        from swarm.board import VerdictDetails
        d = None if details is None else VerdictDetails(details, "not_met", "Judge", None, "b@" + "a" * 40)
        return types.SimpleNamespace(verdict_details=lambda job, artifact=None: d)

    def test_the_fix_worker_is_pointed_at_the_report_and_gets_its_start(self):
        from swarm.supervisor import pipeline
        action = self.action({"reason": "bad", "next_steps": "fix it"})
        text = pipeline.prompt_for(action, {}, self.board(REPORT))
        self.assertIn("swarm verdict show --job J --artifact", text)
        self.assertIn("1. The drill was never run.", text)
        self.assertIn("fix it", text)

    def test_no_report_no_pointer(self):
        from swarm.supervisor import pipeline
        action = self.action({"reason": "bad", "next_steps": "fix it"})
        self.assertNotIn("verdict show", pipeline.prompt_for(action, {}, self.board(None)))
        self.assertNotIn("verdict show", pipeline.prompt_for(action, {}))

    def test_the_orchestrator_brief_names_the_report(self):
        from swarm import respawn
        js = types.SimpleNamespace(verdict_by="Judge", verdict_reason="bad", verdict_next="fix it")
        self.assertIn("swarm verdict show --job J", respawn.brief(js, "J", details=True))
        self.assertNotIn("verdict show", respawn.brief(js, "J"))


if __name__ == "__main__":
    unittest.main()
