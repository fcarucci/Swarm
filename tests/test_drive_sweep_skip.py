"""A drive job belongs to the driver: no core sweep (stale-waiting, stall, orphan and quiet auto-close,
generic blocker expiry, the orphan-coordinator and stuck-agent passes) closes, restarts or answers for it.
Identified by the ledger on this host OR the board claim `drive.enabled`."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

from swarm import cli as swarm
from swarm.board.autoinit import store_key
from swarm.supervisor import ledger, settings

MIN = 60
CLOSE_AT = 241 * MIN


class SkipBase(Env):
    def activate(self, job="J", *extra):
        if "--goal" in extra:      # a goal job nothing drives (activation would otherwise need a supervisor pass)
            extra = (*extra, )
        rc, _, err = self.cli("activate", "--job", job, *extra)
        self.assertEqual(rc, 0, err)

    def job(self, job="J"):
        with self.board() as b:
            return b.job_status(job)

    def finished_agent(self, job="J", key="agent-1"):
        with self.board() as b:
            b.allocate_name(key, job, "worker")
            b.agent_stopped(key)

    def quiet(self, seconds, job="J", agents=("agent-1",)):
        with self.board() as b:
            ids = [m.id for m in b.messages_after(0, job)]
        self.h.backdate_job(job, created_at=seconds + 120, activated_at=seconds + 120)
        for a in agents:
            self.h.backdate_agent(a, joined_at=seconds + 60, last_seen=seconds, left_at=seconds)
        for i in ids:
            self.h.backdate_message(i, seconds)

    def claim(self, job="J", on=True):
        with self.board() as b:
            b.set_job_data(job, "drive.enabled", "1" if on else None)

    def ledger(self, job="J"):
        cfg = swarm.load_config(self.config)
        return ledger.create(store_key(cfg), job, budgets={}, activated_at=dt.datetime.now(dt.timezone.utc).isoformat())

    def sweep(self):
        return self.cli("purge")

    def notices(self, job="J"):
        with self.board() as b:
            return [m for m in b.messages_after(0, job) if m.agent_name == "swarm" and "stale" in m.message.lower()]


class StaleWaitingSkip(SkipBase):
    def waiting_goal_job(self):
        """A goal-less job waiting on a wait with its agent gone: the stale rule closes it unless it is the driver's."""
        self.activate("J")
        self.finished_agent()
        rc, _, err = self.cli("wait", "--job", "J", "--on", "the human")
        self.assertEqual(rc, 0, err)
        self.quiet(CLOSE_AT + 3600)

    def test_control_an_ordinary_waiting_job_is_closed(self):
        self.waiting_goal_job()
        self.sweep()
        self.assertEqual(self.job().status, "failed")

    def test_a_job_claiming_drive_is_not_closed_nor_noticed(self):
        self.waiting_goal_job()
        self.claim()
        for _ in range(3):
            self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.notices(), [])

    def test_a_job_with_a_ledger_is_not_closed_even_when_the_claim_is_gone(self):
        self.waiting_goal_job()
        self.ledger()
        self.claim(on=False)
        self.sweep()
        self.assertEqual(self.job().status, "active")
        self.assertEqual(self.notices(), [])

    def test_a_bad_ledger_still_protects_the_job(self):
        cfg = swarm.load_config(self.config)
        led = self.ledger()
        path = settings.private_dir() / ledger.SUBDIR / ledger._name(store_key(cfg), "J")
        path.write_bytes(b"{ not a ledger")
        self.assertRaises(ledger.LedgerBadError, ledger.load, store_key(cfg), "J")
        self.assertTrue(swarm.drive_ledger_present(cfg, "J"))

    def test_asking_creates_nothing(self):
        cfg = swarm.load_config(self.config)
        before = sorted(p for p in settings.private_dir().rglob("*")) if settings.private_dir().exists() else []
        self.assertFalse(swarm.drive_ledger_present(cfg, "J"))
        after = sorted(p for p in settings.private_dir().rglob("*")) if settings.private_dir().exists() else []
        self.assertEqual(before, after)

    def test_drive_ledger_present_follows_the_ledger(self):
        cfg = swarm.load_config(self.config)
        self.assertFalse(swarm.drive_ledger_present(cfg, "J"))
        self.ledger()
        self.assertTrue(swarm.drive_ledger_present(cfg, "J"))


class BoardLevelSkip(SkipBase):
    def test_stall_and_orphan_rules_skip_a_drive_job(self):
        self.activate("G", "--goal", "ship it")        # the stall rule (goal job)
        self.activate("K")                              # the orphan rule (no goal)
        self.finished_agent("G", "g1")
        self.finished_agent("K", "k1")
        self.quiet(10 * 3600, "G", ("g1",))
        self.quiet(10 * 3600, "K", ("k1",))
        self.claim("G")
        self.claim("K")
        with self.board() as b:
            self.assertEqual(b.sweep_expiry(1, 30, goal_stall_hours=1), [])
        self.assertEqual((self.job("G").status, self.job("K").status), ("active", "active"))
        self.claim("G", on=False)
        self.claim("K", on=False)
        with self.board() as b:
            closed = b.sweep_expiry(1, 30, goal_stall_hours=1)
        self.assertEqual(sorted(c.job for c in closed), ["G", "K"])

    def test_the_ledger_hook_alone_skips_the_job(self):
        self.activate("K")
        self.finished_agent("K", "k1")
        self.quiet(10 * 3600, "K", ("k1",))
        with self.board() as b:
            b.drive_ledger = lambda job: job == "K"
            self.assertEqual(b.sweep_expiry(0, 30), [])
            self.assertEqual(b.sweep_auto_close(5), [])
        self.assertEqual(self.job("K").status, "active")

    def test_quiet_auto_close_skips_a_drive_job(self):
        self.activate("K")
        self.finished_agent("K", "k1")
        self.quiet(3 * 3600, "K", ("k1",))
        self.claim("K")
        with self.board() as b:
            self.assertEqual(b.sweep_auto_close(5), [])
        self.assertEqual(self.job("K").status, "active")
        self.claim("K", on=False)
        with self.board() as b:
            self.assertEqual([c.job for c in b.sweep_auto_close(5)], ["K"])

    def test_generic_expire_blockers_leaves_a_drive_question_to_the_driver(self):
        self.activate("K")
        with self.board() as b:
            q = b.open_blocker("K", "question", "human", "which way?", until=b.now() + dt.timedelta(minutes=1),
                               default_value="left", created_by="a", launch="a", generation=1)
        self.claim("K")
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        with self.board() as b:
            b.expire_blockers(later)
            self.assertEqual(b.blocker(q.id).state, "open")
        self.claim("K", on=False)
        with self.board() as b:
            b.expire_blockers(later)
            self.assertEqual(b.blocker(q.id).state, "expired")

    def test_the_ledger_hook_alone_keeps_generic_expiry_off_a_question(self):
        self.activate("K")
        with self.board() as b:
            q = b.open_blocker("K", "question", "human", "which way?", until=b.now() + dt.timedelta(minutes=1),
                               default_value="left", created_by="a", launch="a", generation=1)
            b.drive_ledger = lambda job: job == "K"
            b.expire_blockers(dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
            self.assertEqual(b.blocker(q.id).state, "open")


class StuckSkip(SkipBase):
    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + '\n[supervise]\nenabled = true\n')
        self.cfg = swarm.load_config(self.config)
        self.activate("J", "--session", "0b9f6c1e-2d3a-4e5f-8a7b-9c0d1e2f3a4b")
        self.hook("start", agent_id="a1", session="0b9f6c1e-2d3a-4e5f-8a7b-9c0d1e2f3a4b")
        self.h.backdate_agent("a1", last_seen=31 * 60)

    def found(self):
        from swarm.supervisor import stuck
        with self.board() as b:
            return stuck.find_stuck_owned(b, self.cfg)

    def test_control_a_stuck_agent_is_found(self):
        self.assertEqual(len(self.found()), 1)

    def test_a_drive_job_is_not_scanned(self):
        self.claim()
        self.assertEqual(self.found(), [])


if __name__ == "__main__":
    unittest.main()
