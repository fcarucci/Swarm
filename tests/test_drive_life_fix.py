"""Fix round for Task 8b: the security probes (each was a working exploit) turned into regression tests,
and the review's gaps. Built on test_drive_lifecycle's LifeCase."""
from __future__ import annotations

import datetime as dt
import os
import unittest
from unittest import mock

from support import MemoryHarness  # noqa: F401
import drive_fixtures as fx
import test_drive_lifecycle as T
from test_drive_lifecycle import life, ledger, MIN, CAND, REQ

from swarm import cli, compat
from swarm.board import base as bb


class ForgedCloseTests(T.LifeCase):
    """H1: crash recovery trusts the ledger's closing marker, never the board's closed_by."""

    def test_a_forged_driver_close_is_not_committed(self):
        self.pending_met()
        self.run.gate = life.Gate("reject", "target moved")
        self.assertTrue(self.b.close_job("J", "completed", "forged", closed_by="driver"))
        for _ in range(2):
            self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIsNone(self.reload().terminal)
        self.assertNotIn(("gate",), self.run.calls)
        self.assertEqual(len(self.posts("DRIVER-TAMPER")), 1)          # said once, not every pass
        self.assertEqual(self.b.job_data("J").get("drive.enabled"), "1")

    def test_a_forged_close_cannot_commit_a_mechanical_unreachable_either(self):
        life.begin_terminal(self.b, self.led, "unreachable", "ruling", life.MECHANICAL, "x", self.t0)
        self.b.close_job("J", "failed", "forged", closed_by="driver")
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIsNone(self.reload().terminal)

    def test_the_marker_is_in_the_ledger_before_the_board_close(self):
        self.pending_met()
        seen = {}
        real = self.b.close_job

        def spy(*a, **kw):
            seen["marker"] = [k for k in self.reload().watermarks if k.startswith("closing:")]
            return real(*a, **kw)
        with mock.patch.object(self.b, "close_job", side_effect=spy):
            self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertEqual(len(seen["marker"]), 1)

    def test_a_crash_after_the_real_close_still_commits(self):
        self.pending_met()
        with self.barrier_crash("terminal_closed_before_ledger"), self.assertRaises(T.Crash):
            life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        led = self.reload()
        self.assertIsNone(led.terminal)
        self.assertTrue(life.terminalize(self.b, self.cfg, led, self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "met")
        self.assertEqual(self.run.calls.count(("gate",)), 1)           # not asked again

    def test_the_cli_never_records_a_machine_closer(self):
        for user, want in (("driver", None), ("auto", None), ("alice", "alice")):
            with self.subTest(user=user), mock.patch.dict(os.environ, {"USER": user}):
                self.assertEqual(cli.cli_closed_by(), want)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("USER", None)
            self.assertIsNone(cli.cli_closed_by())


class QuestionExpiryTests(T.LifeCase):
    def ask(self, until=240, seat="a", **q):
        b = self.b.open_blocker("J", "question", "human", "which way?", until=self.t0 + until * MIN,
                                default_value="left", created_by=seat, seat=seat, generation=1)
        return b, fx.q(b.id, seat=seat, default="left", until=self.t0 + until * MIN, receipt_id=1, **q)

    def test_a_backdated_board_created_at_does_not_shorten_the_deadline(self):
        b, q = self.ask(until=240)
        with self.h.store.lock:
            next(x for x in self.h.store.blockers if x["id"] == b.id)["created_at"] = self.t0 - 600 * MIN
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(1), [q]), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")

    def test_the_cap_counts_from_the_receipted_opening(self):
        b, q = self.ask(until=24 * 60, opened_at=self.t0)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(239), [q]), [])
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(240), [q]), [b.id])

    def test_without_a_receipted_time_the_cap_counts_from_first_sight_in_the_ledger(self):
        b, q = self.ask(until=24 * 60)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(100), [q]), [])
        led = self.reload()                                           # seen at minute 100: cap at minute 340
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, led, self.at(339), [q]), [])
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, led, self.at(340), [q]), [b.id])

    def test_a_question_with_no_until_gets_the_cap(self):
        b = self.b.open_blocker("J", "question", "human", "which way?", until=None, default_value="left",
                                created_by="a", seat="a", generation=1)
        q = fx.q(b.id, default="left", until=None, receipt_id=1, opened_at=self.t0)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(239), [q]), [])
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(240), [q]), [b.id])

    def test_a_blocker_of_another_job_or_kind_is_not_resolved(self):
        self.b.open_job("K", None, None, None, "me")
        other = self.b.open_blocker("K", "question", "human", "q", until=self.t0 + MIN, default_value="x",
                                    created_by="a", seat="a", generation=1)
        wait = self.b.open_blocker("J", "wait", "external", "w", until=self.t0 + MIN, default_value="x",
                                   created_by="a", seat="a", generation=1)
        qs = [fx.q(other.id, until=self.t0 + MIN, receipt_id=1), fx.q(wait.id, until=self.t0 + MIN, receipt_id=1)]
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(60), qs), [])
        self.assertEqual((self.b.blocker(other.id).state, self.b.blocker(wait.id).state), ("open", "open"))

    def test_the_core_sweep_does_not_apply_a_halted_jobs_default(self):
        life.halt(self.b, self.cfg, self.led, "person", self.at(1), hooks=self.run.hooks())
        b, q = self.ask(until=30)
        self.b.expire_blockers(self.at(31))
        self.assertEqual(self.b.blocker(b.id).state, "open")
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(31), [q]), [])

    def test_rollover_keeps_the_once_only_marks(self):
        b, q = self.ask(until=30)
        self.assertEqual(life.apply_question_expiry(self.b, self.cfg, self.led, self.at(31), [q]), [b.id])
        self.led.generation_requested = 2
        self.led.save()
        self.assertTrue(life.rollover(self.b, self.cfg, self.led, self.at(32), hooks=self.run.hooks()))
        led = self.reload()
        self.assertIn(f"qdone:{b.id}", led.watermarks)
        self.assertEqual([k for k in led.watermarks if not k.startswith("qdone:")], [])


class OrphanQuestionContractTests(T.LifeCase):
    def test_a_job_with_a_ledger_here_is_not_the_orphan_paths(self):
        b = self.b.open_blocker("J", "question", "human", "q", until=self.t0 + MIN, default_value="left",
                                created_by="a", seat="a", generation=1)
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(5), board_key="bk"), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")
        self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(5)), [b.id])   # the board-only path


class OrphanQuestionLedgerTests(T.LifeCase):
    def test_a_damaged_ledger_still_keeps_the_orphan_path_out(self):
        b = self.b.open_blocker("J", "question", "human", "q", until=self.t0 + MIN, default_value="left",
                                created_by="a", seat="a", generation=1)
        with mock.patch.object(ledger, "load", side_effect=ledger.LedgerBadError("bad")):
            self.assertEqual(life.expire_orphan_questions(self.b, "J", self.at(5), board_key="bk"), [])
        self.assertEqual(self.b.blocker(b.id).state, "open")


class LockHostTests(T.LifeCase):
    def test_a_row_on_another_host_is_neither_fenced_nor_ended(self):
        lid = self.launch(locks=["repo:x"])
        with self.h.store.lock:
            for r in self.h.store.restarts:
                if r["launch_id"] == lid:
                    r["host"] = "host-a"
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 0)
        self.assertIsNone(self.row(lid).ended_at)
        self.assertEqual(len(self.b.drive_locks()), 1)                 # it keeps blocking
        self.assertEqual(self.run.calls, [])                           # nothing was stopped

    def test_a_lock_rehomed_to_another_host_is_not_touched_here(self):
        lid = self.launch(locks=["repo:x"])
        with self.h.store.lock:
            for l in self.h.store.drive_locks:
                l["host"] = "host-b"
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 0)
        self.assertIsNone(self.row(lid).ended_at)

    def test_a_forged_ended_duplicate_row_does_not_release_a_live_launchs_locks(self):
        lid = self.launch(locks=["repo:x"])
        live = self.row(lid)
        with self.h.store.lock:                       # a later row with the same launch id, already ended
            dup = dict(next(r for r in self.h.store.restarts if r["launch_id"] == lid))
            dup["id"] = max(r["id"] for r in self.h.store.restarts) + 1
            dup["ended_at"] = self.t0
            self.h.store.restarts.append(dup)
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(1), hooks=self.run.hooks()), 0)
        self.assertEqual(len(self.b.drive_locks()), 1)
        self.assertIsNone(next(r for r in self.b.restarts("J") if r.id == live.id).ended_at)

    def test_a_forged_ended_row_cannot_free_a_launch_the_ledger_holds_open(self):
        lid = self.launch(locks=["repo:x"])
        self.b.finish_restart(self.row(lid).id, "cancelled")            # the board row says it ended
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(1), hooks=self.run.hooks(),
                                                  board_key="bk"), 0)
        self.assertEqual(len(self.b.drive_locks()), 1)
        for i in self.led.intents:                                       # once the ledger concludes it
            i.state, i.outcome, i.success, i.charged_usd = "concluded", "cancelled", False, 1.0
        self.led.save()
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(1), hooks=self.run.hooks(),
                                                  board_key="bk"), 1)

    def test_an_unreadable_ledger_holds_the_lock(self):
        lid = self.launch(locks=["repo:x"])
        self.b.finish_restart(self.row(lid).id, "cancelled")
        with mock.patch.object(ledger, "load", side_effect=ledger.LedgerBadError("bad")):
            self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(1), hooks=self.run.hooks(),
                                                      board_key="bk"), 0)

    def test_another_hosts_lock_is_never_released_here_even_when_its_row_ended(self):
        lid = self.launch(locks=["repo:x"])
        self.b.finish_restart(self.row(lid).id, "cancelled")
        with self.h.store.lock:
            for l in self.h.store.drive_locks:
                l["host"] = "host-b"
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(1), hooks=self.run.hooks()), 0)
        self.assertEqual(len(self.b.drive_locks()), 1)

    def test_a_row_on_this_host_is_still_fenced_and_released(self):
        lid = self.launch(locks=["repo:x"])
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 1)
        self.assertIsNotNone(self.row(lid).ended_at)
        self.assertEqual(self.run.calls, [("stop", [lid])])


class RolloverLeadTests(T.LifeCase):
    def lead(self):
        s = self.b.request_seat("J", 1, "lead", role="lead", harness="claude", model="opus",
                                workdir="/w/lead", brief_path="/w/lead/B.md", requested_by="me")
        self.receipt_seat(s)
        return s

    def roll(self):
        self.led.generation_requested = 2
        self.led.save()
        return life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=life.Hooks(
            stop_rows=self.run.stop_rows, rows_live=self.run.rows_live))

    def test_the_new_lead_is_built_from_the_receipted_seat(self):
        self.lead()
        self.assertTrue(self.roll())
        new = self.b.seat("J", 2, "lead")
        self.assertEqual((new.brief_path, new.workdir, new.model, new.role), ("/w/lead/B.md", "/w/lead", "opus", "lead"))

    def test_an_edited_board_row_does_not_pick_the_new_leads_brief_or_workdir(self):
        s = self.lead()
        with self.h.store.lock:
            row = next(x for x in self.h.store.seats if x["id"] == s.id)
            row["brief_path"], row["workdir"] = "/evil/B.md", "/evil"
        self.assertTrue(self.roll())
        self.assertIsNone(self.b.seat("J", 2, "lead"))
        self.assertEqual(len(self.posts("matches its receipted request")), 1)

    def test_an_unreceipted_lead_row_gives_no_new_lead(self):
        self.b.request_seat("J", 1, "lead", role="lead", harness="claude", model="opus",
                            workdir="/w/lead", brief_path="/w/lead/B.md", requested_by="me")
        self.roll()
        self.assertIsNone(self.b.seat("J", 2, "lead"))

    def test_an_older_receipted_generation_is_used_when_the_latest_is_forged(self):
        self.lead()
        self.led.generation = 2
        s2 = self.b.request_seat("J", 2, "lead", role="lead", harness="claude", model="opus",
                                 workdir="/evil", brief_path="/evil/B.md", requested_by="me")   # never receipted
        self.led.generation_requested = 3
        self.led.save()
        life.default_request_lead(self.b, self.cfg, self.led, 3)
        new = self.b.seat("J", 3, "lead")
        self.assertEqual(new.workdir, "/w/lead")


class StuckTerminalTests(T.LifeCase):
    def stuck_pending(self):
        lid = self.launch()
        self.run.live.add(lid)
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(60 * 27), hooks=self.run.hooks()))
        return lid

    def test_a_notice_is_posted_once_after_the_close_deadline(self):
        self.stuck_pending()
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(60 * 27 + 59), hooks=self.run.hooks()))
        self.assertEqual(self.posts("DRIVER-TERMINAL-STUCK"), [])
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(60 * 27 + 60), hooks=self.run.hooks()))
        self.assertEqual(len(self.posts("DRIVER-TERMINAL-STUCK")), 1)       # exactly at the close deadline
        for minutes in (120, 24 * 60):
            self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(60 * 27 + minutes), hooks=self.run.hooks()))
        self.assertEqual(len(self.posts("DRIVER-TERMINAL-STUCK")), 1)
        self.assertIsNone(self.reload().terminal)                      # still never closes past a live session

    def test_a_stuck_mechanical_intent_is_left_as_it_is(self):
        self.stuck_pending()
        at = self.reload().terminal_intent["at"]
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(60 * 29), hooks=self.run.hooks()))
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(60 * 40), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal_intent["at"], at)
        self.assertEqual(len(self.posts("DRIVER-MECHANICAL-UNREACHABLE")), 1)

    def test_a_stuck_pending_met_is_superseded_by_the_deadline(self):
        lid = self.launch()
        self.run.live.add(lid)
        self.pending_met(now=self.at(1))
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(100), hooks=self.run.hooks()))
        self.assertEqual(len(self.posts("DRIVER-TERMINAL-STUCK")), 1)
        far = self.at(60 * 30)
        self.assertTrue(life.enforce_deadlines(self.b, self.cfg, self.led, far, hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal_intent["verdict"], "unreachable")
        self.run.live.clear()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.reload(), far, hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "unreachable")

    def test_a_pending_met_not_yet_stuck_is_not_superseded(self):
        self.pending_met()
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(60 * 30), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal_intent["verdict"], "met")


class HaltLedgerTests(T.LifeCase):
    def test_a_halt_killed_attempt_is_halted_but_a_board_edit_cannot_make_one(self):
        keep = self.launch("old")
        old_ended = self.launch("pre")
        self.run.live.clear()
        # "pre" concluded cancelled BEFORE the halt (a genuine cancel); then the halt begins
        for i in self.led.intents:
            if i.launch_id == old_ended:
                i.state, i.outcome, i.success, i.charged_usd = "concluded", "cancelled", False, 1.0
        self.led.save()
        self.b.finish_restart(self.row(old_ended).id, "cancelled")
        life.halt(self.b, self.cfg, self.led, "person", self.at(1), hooks=self.run.hooks())
        # a reconcile concludes the halt-killed attempt as cancelled afterwards
        for i in self.led.intents:
            if i.launch_id == keep and i.state == "concluded":
                i.outcome = "cancelled"
        life.settle_halt(self.b, self.cfg, self.led, self.at(2), hooks=self.run.hooks())
        got = {i.launch_id: i.outcome for i in self.reload().intents}
        self.assertEqual(got[keep], "halted")
        # the earlier cancel: the board's ended_at moved past the halt must not reclassify it
        with self.h.store.lock:
            for r in self.h.store.restarts:
                if r["launch_id"] == old_ended:
                    r["ended_at"] = self.at(5)
        life.settle_halt(self.b, self.cfg, self.led, self.at(6), hooks=self.run.hooks())
        self.assertEqual({i.launch_id: i.outcome for i in self.reload().intents}[old_ended], "cancelled")

    def test_sessions_ended_by_a_halt_without_open_marks_are_still_halted(self):
        lid = self.launch("a")
        self.led.halted = {"by": "person", "at": fx.iso(self.at(1))}      # a ledger halted before the marks existed
        self.led.save()
        self.assertTrue(life.settle_halt(self.b, self.cfg, self.led, self.at(2), hooks=self.run.hooks()))
        self.assertEqual({i.launch_id: i.outcome for i in self.reload().intents}[lid], "halted")

    def test_unhalt_forgets_the_open_marks(self):
        self.launch("a")
        life.halt(self.b, self.cfg, self.led, "person", self.at(1), hooks=self.run.hooks())
        self.assertTrue([k for k in self.reload().watermarks if k.startswith("hopen:")])
        life.unhalt(self.b, self.cfg, self.reload(), "person", self.at(5))
        self.assertEqual([k for k in self.reload().watermarks if k.startswith("hopen:")], [])


class RereviewTests(T.LifeCase):
    def test_a_forged_open_duplicate_does_not_hide_the_real_session(self):
        lid = self.launch(locks=["repo:x"])
        real = self.row(lid)
        self.b.finish_restart(real.id, "cancelled")
        with self.h.store.lock:
            dup = dict(next(r for r in self.h.store.restarts if r["launch_id"] == lid))
            dup["id"] = max(r["id"] for r in self.h.store.restarts) + 1
            dup["ended_at"] = None
            self.h.store.restarts.append(dup)
        self.run.live.add(lid)                                       # the real session still runs
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 0)
        self.assertEqual(len(self.b.drive_locks()), 1)
        self.assertEqual(self.run.calls[0][0], "stop")

    def test_an_ended_row_on_another_host_does_not_free_the_lock(self):
        lid = self.launch(locks=["repo:x"])
        self.b.finish_restart(self.row(lid).id, "cancelled")
        with self.h.store.lock:
            for r in self.h.store.restarts:
                r["host"] = "host-a"
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 0)
        self.assertEqual(len(self.b.drive_locks()), 1)

    def _dup_rows(self, lid):
        real = self.row(lid)
        with self.h.store.lock:
            dup = dict(next(r for r in self.h.store.restarts if r["launch_id"] == lid))
            dup["id"] = max(r["id"] for r in self.h.store.restarts) + 1
            self.h.store.restarts.append(dup)
        return real.id, dup["id"]

    def test_every_row_of_the_launch_is_fenced_and_checked(self):
        for live_index in (0, 1):
            with self.subTest(live=live_index):
                self.setUp()
                lid = self.launch(locks=["repo:x"])
                ids = self._dup_rows(lid)
                stopped = []
                hooks = life.Hooks(stop_rows=lambda b, c, rows: stopped.append(sorted(r.id for r in rows)),
                                   rows_live=lambda b, c, rows: [r for r in rows if r.id == ids[live_index]])
                self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=hooks), 0)
                self.assertEqual(stopped, [sorted(ids)])
                self.assertEqual(len(self.b.drive_locks()), 1)

    def test_all_rows_are_ended_when_the_lock_is_released(self):
        lid = self.launch(locks=["repo:x"])
        ids = self._dup_rows(lid)
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 1)
        self.assertTrue(all(r.ended_at is not None for r in self.b.restarts("J") if r.id in ids))

    def test_a_lock_and_row_on_another_host_are_left_alone(self):
        lid = self.launch(locks=["repo:x"])
        with self.h.store.lock:
            for r in self.h.store.restarts:
                r["host"] = "host-b"
            for l in self.h.store.drive_locks:
                l["host"] = "host-b"
        self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(200), hooks=self.run.hooks()), 0)
        self.assertEqual(self.run.calls, [])
        self.assertIsNone(self.row(lid).ended_at)

    def test_a_finished_ledger_is_not_a_drive_view(self):
        from swarm.supervisor import orphans
        with mock.patch("swarm.board.autoinit.store_key", return_value="bk"):
            self.assertEqual(orphans.drive_view(self.b, self.cfg, "J"), (True, 1))
            self.pending_met()
            self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
            self.assertEqual(orphans.drive_view(self.b, self.cfg, "J"), (False, None))

    def test_the_ledger_is_asked_by_default_through_the_stores_key(self):
        lid = self.launch(locks=["repo:x"])
        self.b.finish_restart(self.row(lid).id, "cancelled")
        with mock.patch("swarm.board.autoinit.store_key", return_value="bk"):
            self.assertEqual(life.sweep_locks_by_host(self.b, self.cfg, self.at(1), hooks=self.run.hooks()), 0)
        self.assertEqual(len(self.b.drive_locks()), 1)

    def test_a_finished_ledger_does_not_claim_the_job(self):
        self.pending_met()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertFalse(ledger.claims("bk", "J"))
        self.assertTrue(ledger.exists("bk", "J"))

    def test_an_unfinished_ledger_claims_the_job(self):
        self.assertTrue(ledger.claims("bk", "J"))
        self.assertFalse(ledger.claims("bk", "nope"))

    def test_crash_recovery_needs_the_board_status_to_match_the_intent(self):
        self.pending_met()
        self.led.watermarks[life._closing_key(self.led.terminal_intent)] = 1
        self.led.save()
        self.b.close_job("J", "failed", "writer", closed_by="driver")          # not "completed" for a MET
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIsNone(self.reload().terminal)

    def test_cancel_does_not_overwrite_a_met_whose_close_began(self):
        self.pending_met()
        with self.barrier_crash("terminal_closed_before_ledger"), self.assertRaises(T.Crash):
            life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        self.assertTrue(life.cancel_job(self.b, self.cfg, self.reload(), "alice", self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "met")


class CancelTests(T.LifeCase):
    """CANCELLED: a person ended the job; ledger first, every terminal state clears the claim."""

    def cancel(self, at=1, reason="stop it", **kw):
        return life.cancel_job(self.b, self.cfg, self.led, "alice", self.at(at), reason=reason,
                               hooks=self.run.hooks(), **kw)

    def test_an_open_job_is_closed_cancelled_and_the_ledger_commits(self):
        lid = self.launch(locks=["repo:x"])
        self.assertTrue(self.cancel())
        led = self.reload()
        self.assertEqual((led.terminal["verdict"], led.terminal["scope"]), ("cancelled", "job"))
        js = self.job()
        self.assertEqual((js.status, js.closed_by), ("cancelled", "driver"))
        self.assertIn("stop it", js.outcome)
        self.assertEqual(self.b.drive_locks(), [])
        self.assertIsNone(self.b.job_data("J").get("drive.enabled"))
        self.assertEqual(led.intents[0].outcome, "cancelled")
        self.assertIn(("stop", [lid]), self.run.calls)

    def test_questions_are_settled_and_seats_released(self):
        seat = self.seat("idle-b")
        q = self.b.open_blocker("J", "question", "human", "which way?", until=self.t0 + 600 * MIN,
                                default_value="left", created_by="a", seat="idle-b", generation=1)
        self.assertTrue(self.cancel())
        self.assertEqual(self.b.blocker(q.id).state, "expired")
        self.assertEqual(self.b.seats("J", include_closed=True)[0].state, "released")

    def test_a_job_a_person_already_closed_is_cleaned_up_and_committed(self):
        self.launch(locks=["repo:x"])
        self.b.close_job("J", "cancelled", "by hand", closed_by="alice")
        self.assertTrue(self.cancel())
        self.assertEqual(self.reload().terminal["verdict"], "cancelled")
        self.assertEqual(self.job().closed_by, "alice")                # not re-closed or re-labelled
        self.assertEqual(self.b.drive_locks(), [])
        self.assertIsNone(self.b.job_data("J").get("drive.enabled"))

    def test_it_waits_for_live_sessions_then_finishes(self):
        lid = self.launch()
        self.run.live.add(lid)
        self.assertFalse(self.cancel())
        self.assertEqual(self.job().status, "active")
        self.assertIsNone(self.reload().terminal)
        self.assertEqual(self.reload().terminal_intent["verdict"], "cancelled")     # written first
        self.run.live.clear()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.reload(), self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "cancelled")

    def test_a_person_overrides_a_pending_met_and_a_halt(self):
        self.pending_met()
        life.halt(self.b, self.cfg, self.led, "person", self.at(1), hooks=self.run.hooks())
        self.assertTrue(self.cancel(at=2))
        self.assertEqual(self.reload().terminal["verdict"], "cancelled")
        self.assertNotIn(("gate",), self.run.calls)                     # no gate, no land for a cancel
        self.assertNotIn(("land",), self.run.calls)

    def test_a_stuck_cancel_is_not_superseded_by_the_deadline(self):
        lid = self.launch()
        self.run.live.add(lid)
        self.assertFalse(self.cancel())
        self.assertFalse(life.terminalize(self.b, self.cfg, self.led, self.at(61), hooks=self.run.hooks()))
        self.assertEqual(len(self.posts("DRIVER-TERMINAL-STUCK")), 1)
        self.assertFalse(life.enforce_deadlines(self.b, self.cfg, self.led, self.at(60 * 40), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal_intent["verdict"], "cancelled")

    def test_a_crash_after_the_close_resumes(self):
        with self.barrier_crash("terminal_closed_before_ledger"), self.assertRaises(T.Crash):
            self.cancel()
        self.assertIsNone(self.reload().terminal)
        self.assertTrue(life.terminalize(self.b, self.cfg, self.reload(), self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "cancelled")

    def test_cancelling_a_terminal_job_does_nothing_but_repair_the_claim(self):
        self.assertTrue(self.cancel())
        self.b.set_job_data("J", "drive.enabled", "1")
        self.assertFalse(self.cancel(at=3))
        self.assertIsNone(self.b.job_data("J").get("drive.enabled"))


class EveryTerminalClearsTheClaim(T.LifeCase):
    def test_met_unreachable_and_cancelled(self):
        self.pending_met()
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
        self.assertIsNone(self.b.job_data("J").get("drive.enabled"))

    def test_the_mechanical_unreachable(self):
        life.enforce_deadlines(self.b, self.cfg, self.led, self.at(60 * 27), hooks=self.run.hooks())
        self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(60 * 27 + 1), hooks=self.run.hooks()))
        self.assertIsNone(self.b.job_data("J").get("drive.enabled"))


class MidBarrierTests(T.LifeCase):
    def test_a_crash_mid_terminalization_resumes_to_the_same_end(self):
        self.pending_met()
        with self.barrier_crash("mid_terminalization"), self.assertRaises(T.Crash):
            life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        self.assertIsNone(self.reload().terminal)
        self.assertTrue(life.terminalize(self.b, self.cfg, self.reload(), self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "met")

    def test_a_crash_mid_cancel_resumes(self):
        with self.barrier_crash("mid_terminalization"), self.assertRaises(T.Crash):
            life.cancel_job(self.b, self.cfg, self.led, "alice", self.at(1), hooks=self.run.hooks())
        self.assertTrue(life.terminalize(self.b, self.cfg, self.reload(), self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().terminal["verdict"], "cancelled")

    def test_a_crash_mid_rollover_resumes(self):
        self.launch("lead")
        self.led.generation_requested = 2
        self.led.save()
        with self.barrier_crash("mid_rollover"), self.assertRaises(T.Crash):
            life.rollover(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks())
        self.assertEqual(self.reload().generation, 1)
        self.assertTrue(life.rollover(self.b, self.cfg, self.reload(), self.at(2), hooks=self.run.hooks()))
        self.assertEqual(self.reload().generation, 2)


class ClaimRepairTests(T.LifeCase):
    def test_a_failed_claim_clear_is_retried_on_later_passes(self):
        self.pending_met()
        real = self.b.set_job_data
        calls = {"n": 0}

        def flaky(job, key, value):
            if key == "drive.enabled":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("board down")
            return real(job, key, value)
        with mock.patch.object(self.b, "set_job_data", side_effect=flaky):
            self.assertTrue(life.terminalize(self.b, self.cfg, self.led, self.at(1), hooks=self.run.hooks()))
            self.assertEqual(self.b.job_data("J").get("drive.enabled"), "1")        # the first clear failed
            self.assertFalse(life.terminalize(self.b, self.cfg, self.reload(), self.at(2), hooks=self.run.hooks()))
        self.assertIsNone(self.b.job_data("J").get("drive.enabled"))                # a later pass cleared it
        self.assertTrue(life.repair_claim(self.b, self.reload()))                   # and is idempotent

    def test_repair_claim_does_nothing_for_a_job_that_is_not_terminal(self):
        self.assertFalse(life.repair_claim(self.b, self.led))
        self.assertEqual(self.b.job_data("J").get("drive.enabled"), "1")


class QuietStartTests(T.LifeCase):
    """quiet_start and its callers (review Q3, Q5, Q7, Q11, Q12, L1)."""

    def js(self, activated=None, last=None, created=600):
        self.h.backdate_job("J", created_at=created * 60, activated_at=(activated if activated is not None else created) * 60)
        js = self.b.job_status("J")
        return js

    def test_since_is_the_floor_when_it_is_newer_than_last_activity(self):
        js = self.js(activated=600)
        now = self.b.now()
        since = now - dt.timedelta(minutes=10)
        self.assertEqual(bb.quiet_start(js, since, now), since)
        self.assertNotEqual(bb.quiet_start(js, None, now), since)

    def test_since_older_than_the_run_start_loses_to_it(self):
        js = self.js(activated=30, created=600)
        now = self.b.now()
        got = bb.quiet_start(js, now - dt.timedelta(minutes=500), now)
        self.assertEqual(got, bb.run_start(js))

    def test_stale_waiting_uses_since_and_triggers_exactly_at_the_threshold(self):
        js = self.js(activated=600)
        now = self.b.now()
        for since_ago, expect in ((120, True), (119.9, False), (121, True)):
            self.assertEqual(bb.stale_waiting(js, now, 120, since=now - dt.timedelta(minutes=since_ago)), expect, since_ago)
        # without `since` the job's own last activity / run start counts: 600 min quiet
        self.assertTrue(bb.stale_waiting(js, now, 120))

    def test_stale_waiting_threshold_is_inclusive(self):
        js = self.js(activated=120)
        # force last_activity out of the picture: run start exactly 120 min ago
        js = __import__("dataclasses").replace(js, last_activity_at=None)
        now = bb.run_start(js) + dt.timedelta(minutes=120)
        self.assertTrue(bb.stale_waiting(js, now, 120))
        self.assertFalse(bb.stale_waiting(js, now - dt.timedelta(seconds=1), 120))

    def test_a_reopened_job_is_active_not_idle(self):
        js = self.js(activated=1, created=600)                         # reopened a minute ago after hours of quiet
        js = __import__("dataclasses").replace(js, last_activity_at=self.b.now() - dt.timedelta(minutes=500),
                                               started=0, running=0, idle=0, goal=None, waiting_on=None)
        self.assertEqual(bb.derive_job_status(js, 30, self.b.now()), "active")
        old = __import__("dataclasses").replace(js, activated_at=self.b.now() - dt.timedelta(minutes=500))
        self.assertEqual(bb.derive_job_status(old, 30, self.b.now()), "idle")

    def test_activity_newer_than_the_reopening_counts(self):
        js = self.js(activated=600)
        js = __import__("dataclasses").replace(js, last_activity_at=self.b.now() - dt.timedelta(minutes=5),
                                               started=0, running=0, idle=0, goal=None, waiting_on=None)
        self.assertEqual(bb.derive_job_status(js, 30, self.b.now()), "active")

    def test_a_run_start_in_the_future_does_not_hold_a_dead_job_open(self):
        js = self.js(activated=-600)                                   # 10 hours from now
        js = __import__("dataclasses").replace(js, last_activity_at=self.b.now() - dt.timedelta(minutes=500),
                                               started=0, running=0, idle=0)
        now = self.b.now()
        self.assertLess(bb.run_start(js), now + dt.timedelta(hours=11))
        self.assertGreater(bb.run_start(js), now)
        self.assertEqual(bb.quiet_start(js, None, now), js.last_activity_at)
        self.assertTrue(bb.stale_waiting(js, now, 120))
        self.assertTrue(bb.orphaned(js, now, 30))

    def test_derive_ignores_a_future_run_start(self):
        js = self.js(activated=-600)
        js = __import__("dataclasses").replace(js, last_activity_at=self.b.now() - dt.timedelta(minutes=500),
                                               started=0, running=0, idle=0, goal=None, waiting_on=None)
        self.assertEqual(bb.derive_job_status(js, 30, self.b.now()), "idle")

    def test_orphan_rule_counts_from_the_reopening(self):
        js = self.js(activated=10, created=600)
        js = __import__("dataclasses").replace(js, last_activity_at=self.b.now() - dt.timedelta(minutes=500),
                                               started=0, running=0, idle=0, goal=None, waiting_on=None, status="active")
        now = self.b.now()
        self.assertFalse(bb.orphaned(js, now, 30))                      # reopened 10 min ago
        self.assertTrue(bb.orphaned(js, now + dt.timedelta(minutes=21), 30))


if __name__ == "__main__":
    unittest.main()
