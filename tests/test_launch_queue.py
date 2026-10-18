"""Durable launch queue contract on every local board backend."""
import unittest
import json
from pathlib import Path
from unittest import mock
import support
from support import add_backend_variants, drive_backends


class LaunchQueue(unittest.TestCase):
    harness_factory = staticmethod(drive_backends()["memory"])

    def setUp(self):
        self.h = self.harness_factory(self.id())
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", None, None, None, "me", goal="g")

    def queue(self, name="engineer", **over):
        fields = dict(role="engineer", harness="codex", model="gpt-6-astra", workdir="/work/shared",
                      brief="B.md", queued_by="lead")
        fields.update(over)
        return self.b.queue_launch("J", name, **fields)

    def claim(self, row, suffix="1", **over):
        fields = dict(job="J", action_id="gen1|launch|" + row.name + "|-|0|" + suffix,
                      attempt=1, launch_id=row.name + suffix, launch_row_id=row.id, queue_version=row.revision,
                      harness=row.harness, reserve_usd=1, reserve_minutes=1, locks=[], lock_seconds=60,
                      max_job_running=4)
        fields.update(over)
        return self.b.claim_launch(**fields)

    def test_requeue_every_state_reuses_name_and_changes_queue_version(self):
        first = self.queue()
        again = self.queue()
        self.assertEqual(first.id, again.id)
        self.assertGreater(again.revision, first.revision)
        for state in ("done", "failed"):
            end = self.b.set_launch_state(again.id, again.revision, state, job="J", last_outcome=state)
            again = self.queue(model="new-model")
            self.assertEqual((again.id, again.state, again.last_outcome), (first.id, "queued", None))
        running = self.claim(again)
        self.assertIsNotNone(running)
        again = self.queue()
        self.assertEqual(again.state, "queued")
        self.assertIsNone(self.claim(again, "2"))  # don't duplicate a still-running name
        self.b.finish_restart(running.id, "failed")
        self.assertIsNotNone(self.claim(again, "2"))

    def test_same_workdir_runs_concurrently_until_this_jobs_cap(self):
        rows = [self.queue(n) for n in ("a", "b", "c")]
        self.assertIsNotNone(self.claim(rows[0], max_job_running=2))
        self.assertIsNotNone(self.claim(rows[1], max_job_running=2))
        self.assertIsNone(self.claim(rows[2], max_job_running=2))
        self.assertEqual(self.b.drive_locks("wt:"), [])
        self.assertEqual(self.b.get_launch("J", "c").state, "queued")

    def test_host_cap_is_optional_and_independent(self):
        a, b = self.queue("a"), self.queue("b")
        self.assertIsNotNone(self.claim(a, max_host_running=1))
        self.assertIsNone(self.claim(b, max_host_running=1))
        self.assertIsNotNone(self.claim(b))

    def test_queue_and_claim_survive_reopening_board(self):
        row = self.queue()
        with self.h.board() as other:
            self.assertEqual(other.get_launch("J", row.name), row)
        self.assertIsNotNone(self.claim(row))
        with self.h.board() as other:
            self.assertEqual(other.get_launch("J", row.name).state, "running")

    def test_stale_claim_and_stale_settlement_do_not_overwrite_requeue(self):
        old = self.queue()
        new = self.queue()
        self.assertIsNone(self.claim(old))
        self.assertIsNone(self.b.set_launch_state(old.id, old.revision, "failed", job="J"))
        self.assertEqual(self.b.get_launch("J", old.name), new)

    def test_judges_keep_requested_harness_and_model(self):
        row = self.queue(role="judge")
        self.assertEqual((row.harness, row.model), ("codex", "gpt-6-astra"))
        self.assertEqual(self.b.recent_messages(10, job="J"), [])


add_backend_variants(globals(), LaunchQueue)


class LaunchMigration(unittest.TestCase):
    def test_sqlite_upgrades_legacy_rows_and_removes_old_table(self):
        from swarm.board import setup_board
        h = drive_backends()["sqlite"](self.id())
        self.addCleanup(h.close)
        h.reset()
        with h.board() as b:
            b.open_job("J", None, None, None, "me", goal="g")
            for state in ("requested", "idle", "running", "released", "failed", "cancelled", "refused"):
                b.queue_launch("J", state, role="engineer", harness="claude", model="haiku", workdir="/work",
                               brief="B.md", queued_by="lead")
            with b._tx() as c:
                c.execute("CREATE TABLE seats AS SELECT id, job, generation, name AS seat, role, harness, model, "
                          "workdir, brief AS brief_path, state, attempts AS launches, last_outcome, "
                          "queued_by AS requested_by, revision, agent_key, session_id, updated_at, launch_id FROM launches")
                c.execute("UPDATE seats SET state=seat, launch_id=CASE WHEN seat='running' THEN 'live' ELSE NULL END")
                c.execute("DELETE FROM launches")
                c.execute("PRAGMA user_version=25")
        setup_board(h.cfg, {})
        setup_board(h.cfg, {})  # migration is repeatable
        with h.board() as b:
            states = {r.name: r.state for r in b.launches("J")}
            self.assertEqual(states, dict(requested="queued", idle="done", running="running", released="done",
                                          failed="failed", cancelled="failed", refused="failed"))
            self.assertIsNone(b._conn.execute("SELECT name FROM sqlite_master WHERE name='seats'").fetchone())
            self.assertEqual(b.queue_launch("J", "failed", role="engineer", harness="claude", model="haiku",
                                            workdir="/work", brief="B.md", queued_by="lead").state, "queued")

    def test_file_backend_reads_legacy_rows_as_launches(self):
        h = drive_backends()["file"](self.id())
        self.addCleanup(h.close)
        h.reset()
        with h.board() as b:
            b.open_job("J", None, None, None, "me", goal="g")
            b.queue_launch("J", "engineer", role="engineer", harness="claude", model="haiku", workdir="/work",
                           brief="B.md", queued_by="lead")
        path = Path(h.cfg["file"]["path"]) / "state.json"
        raw = json.loads(path.read_text())
        rows = raw.pop("launches")
        for row in rows:
            for old, new in (("name", "seat"), ("brief", "brief_path"), ("attempts", "launches"),
                             ("queued_by", "requested_by")):
                row[new] = row.pop(old)
            row["state"] = "requested"
            row.update(title="old title", complexity="standard", task_ref=None)
        raw["seats"] = rows
        raw["next_seat_id"] = raw.pop("next_launch_id")
        path.write_text(json.dumps(raw))
        with h.board() as b:
            row = b.get_launch("J", "engineer")
            self.assertEqual((row.name, row.state, row.brief), ("engineer", "queued", "B.md"))
            self.assertFalse(hasattr(row, "complexity"))
