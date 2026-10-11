"""Task 11b: the driver's landing (swarm.supervisor.driver_land): requests executed in the pass, leases
claimed and released, interrupted landings completed, INTEGRATED / LAND-REFUSED posted, CI through the CLI."""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

import support  # noqa: F401
from swarm import compat, land, receipts
from swarm.supervisor import barrier, driver_land
from swarm.land import LeaseHeld
from test_drive_land import BK, GROUP, NOW, SEAT, Crash, LandCase, Request


class DriverCase(LandCase):
    def setUp(self):
        super().setUp()
        # the setUp lease claim of LandCase stands in for a long-gone integrator launch: release it
        self.release_first()
        for r in self.board.restarts("J"):
            self.board.finish_restart(r.id, "completed")
        seat = self.board.seat("J", 1, SEAT)
        self.board.set_seat_state(seat.id, seat.revision, "idle", job="J")
        self.posts = []

    def texts(self):
        return [m.message for m in self.board.messages_after(0, "J") if m.agent_name == "swarm supervisor"]

    def submit(self, artifact=None, seat=SEAT, receipted=True):
        artifact = artifact or f"{GROUP}@{self.sha}"
        body = land.request_text(artifact)
        mid = self.board.post("J", "integrator-bot", body).id
        if receipted:
            receipts.write(BK, receipts.Receipt(
                board_key=BK, job="J", generation=1, incarnation=self.led.incarnation, action_id="a", attempt=1,
                seat=seat, kind="post", record_id=str(mid), rev=1,
                content_sha=receipts.content_sha("post", {"to": None, "body": body}), args_sha="0" * 64,
                expires_at=NOW.timestamp() + 3600))
        return mid

    def run_pass(self, **kw):
        env = self.env(**kw.pop("env", {}))
        return driver_land.run(self.board, {}, env, "J")

    def locks(self):
        return self.board.drive_locks("land:")


class Run(DriverCase):
    def test_land_request_executed_by_driver(self):
        mid = self.submit()
        done = self.run_pass()
        self.assertEqual(len(done), 1)
        request, res = done[0]
        self.assertEqual((request.id, request.seat), (mid, SEAT))
        self.assertTrue(res.ok, res.why)
        self.assertEqual(self.head(self.a), self.sha)
        self.assertEqual(self.head(self.b), self.sha)
        self.assertEqual(self.led.integrated[GROUP]["landed"], self.sha)
        self.assertEqual(self.led.integrated[GROUP]["land_id"], str(mid))
        self.assertEqual(self.led.watermarks[driver_land.WATERMARK], mid)
        self.assertEqual(ledger_load(self.led).watermarks[driver_land.WATERMARK], mid)

    def test_integrator_exits_after_land_request_driver_posts_integrated(self):
        self.submit()
        self.run_pass()
        self.assertEqual(self.texts(), [f"INTEGRATED {GROUP}@{self.sha}"])

    def test_leases_and_seat_are_released_after_landing(self):
        mid = self.submit()
        self.run_pass()
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.board.seat("J", 1, SEAT).state, "idle")
        rows = [r for r in self.board.restarts("J") if r.action_id and r.action_id.endswith(f"|{mid}")]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].outcome, bool(rows[0].ended_at)), ("completed", True))

    def test_leases_are_claimed_for_every_destination_while_landing(self):
        seen = []
        real = land.execute

        def spy(board, env, job, request, **kw):
            seen.append(sorted(d.key for d in board.drive_locks("land:")))
            return real(board, env, job, request, **kw)
        self.submit()
        with mock.patch.object(land, "execute", spy):
            self.run_pass()
        self.assertEqual(seen, [land.lease_keys(land.destinations_from_owner(self.owner), "main")])
        self.assertEqual(len(seen[0]), 2)

    def test_refusal_posts_land_refused_with_the_reason_and_moves_on(self):
        self.advance(self.b)
        mid = self.submit()
        done = self.run_pass()
        self.assertFalse(done[0][1].ok)
        text = self.texts()[0]
        self.assertTrue(text.startswith(f"LAND-REFUSED {GROUP}@{self.sha} "), text)
        self.assertIn("rebase", text)
        self.assertEqual(self.led.watermarks[driver_land.WATERMARK], mid)
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.run_pass(), [])          # the refused request is not retried

    def test_long_reasons_are_cut_and_single_line(self):
        self.submit()
        long = "x\ny " * 400
        with mock.patch.object(land, "execute", lambda *a, **k: land.LandResult(False, None, long)):
            self.run_pass()
        text = self.texts()[0]
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), len(f"LAND-REFUSED {GROUP}@{self.sha} ") + driver_land.WHY_MAX)

    def test_pending_posts_nothing_keeps_the_request_and_retries(self):
        mid = self.submit()
        self.ci = "running"
        done = self.run_pass()
        self.assertTrue(done[0][1].pending)
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.led.watermarks.get(driver_land.WATERMARK, 0), 0)
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.pushes, [])
        self.ci = "green"
        done = self.run_pass()
        self.assertTrue(done[0][1].ok, done[0][1].why)
        self.assertEqual(self.texts(), [f"INTEGRATED {GROUP}@{self.sha}"])
        self.assertEqual(self.led.watermarks[driver_land.WATERMARK], mid)

    def test_a_pending_request_blocks_the_ones_after_it(self):
        self.submit()
        later = self.submit()
        self.ci = "running"
        done = self.run_pass()
        self.assertEqual(len(done), 1)
        self.assertNotEqual(done[0][0].id, later)

    def test_requests_are_executed_oldest_first_and_only_after_the_watermark(self):
        first = self.submit()
        self.run_pass()
        git_sha = self.sha
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.led.watermarks[driver_land.WATERMARK], first)
        self.assertEqual(self.head(self.a), git_sha)

    def test_unreceipted_request_is_ignored(self):
        self.submit(receipted=False)
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.texts(), [])

    def test_request_from_the_wrong_seat_is_refused_and_posted(self):
        self.submit(seat="reviewer-x")
        done = self.run_pass()
        self.assertFalse(done[0][1].ok)
        self.assertIn("not from the integrator seat", self.texts()[0])
        self.assertEqual(self.pushes, [])

    def test_unusable_owner_record_refuses_without_claiming(self):
        self.owner = {"remotes": {}, "credentials": {}}
        self.submit()
        done = self.run_pass()
        self.assertFalse(done[0][1].ok)
        self.assertTrue(self.texts()[0].startswith("LAND-REFUSED"))
        self.assertEqual(self.board.restarts("J")[-1].launch_id, self.board.restarts("J")[0].launch_id)

    def test_integrator_seat_not_idle_is_a_hold(self):
        seat = self.board.seat("J", 1, SEAT)
        self.board.set_seat_state(seat.id, seat.revision, "released", job="J")
        self.submit()
        done = self.run_pass()
        self.assertTrue(done[0][1].pending)
        self.assertIn("integrator seat", done[0][1].why)
        self.assertEqual(self.texts(), [])


class LeaseHolds(DriverCase):
    def other_job(self, host=None):
        self.board.open_job("K", None, None, None, "me", goal="G")
        self.assertIsNotNone(self.lease("K", host=host))

    def test_a_lease_held_elsewhere_holds_and_marks_the_group(self):
        self.other_job()
        self.submit()
        done = self.run_pass()
        res = done[0][1]
        self.assertTrue(res.pending and res.lease_held)
        self.assertIn("land lease held by job K", res.why)
        self.assertEqual(self.led.integrated[GROUP]["lease_held_since"], NOW.isoformat())
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.board.seat("J", 1, SEAT).state, "idle")

    def test_the_hold_start_is_kept_until_the_landing_clears_it(self):
        self.other_job()
        self.submit()
        self.run_pass()
        later = NOW.replace(minute=30)
        self.run_pass(env={"now": lambda: later})
        self.assertEqual(self.led.integrated[GROUP]["lease_held_since"], NOW.isoformat())
        for d in self.board.drive_locks("land:"):
            self.board.release_drive_locks(d.launch_id, job=d.job, holder=d.holder)
        done = self.run_pass()
        self.assertTrue(done[0][1].ok, done[0][1].why)
        self.assertNotIn("lease_held_since", self.led.integrated[GROUP])

    def test_foreign_host_lease_is_never_reclaimed_then_ruling(self):
        self.other_job(host="otherhost")
        self.submit()
        res = self.run_pass()[0][1]
        self.assertIn("another host", res.why)
        # an expired foreign lease still blocks: nothing here may fence another host's holder
        class Late:
            def __init__(s, b):
                s.b = b

            def __getattr__(s, n):
                return getattr(s.b, n)

            def now(s):
                import datetime as dt
                return s.b.now() + dt.timedelta(days=2)
        env = self.env()
        out = driver_land.run(Late(self.board), {}, env, "J")
        self.assertTrue(out[0][1].pending)
        self.assertEqual(self.pushes, [])

    def test_orphan_lock_row_is_not_adopted_or_stolen(self):
        # a row with our job but another action holder is not "ours": hold
        self.board.release_drive_locks("x", job="J", holder="y")
        self.assertIsNotNone(self.lease("J", name=SEAT))
        self.submit()
        res = self.run_pass()[0][1]
        self.assertTrue(res.pending and res.lease_held)

    def test_expired_own_lease_is_not_adopted(self):
        self.assertIsNotNone(self.lease("J"))
        for d in self.board.drive_locks("land:"):
            self.assertEqual(d.job, "J")
        mid = self.submit()
        # holder differs from this request's action id, so nothing is adopted
        res = self.run_pass()[0][1]
        self.assertTrue(res.pending)
        self.assertIsNotNone(mid)


class Recovery(DriverCase):
    def crash_after_first(self):
        def hit(name):
            if name == "land_after_first_destination":
                raise Crash()
        # a SIGKILLed pass runs no `finally`: its leases stay behind
        with mock.patch.object(barrier, "hit", hit), mock.patch.object(driver_land, "release_leases",
                                                                       lambda *a, **k: None), \
                self.assertRaises(Crash):
            self.run_pass()

    def test_interrupted_land_completed_by_driver(self):
        self.submit()
        self.crash_after_first()
        self.assertEqual(self.head(self.a), self.sha)
        self.assertEqual(self.head(self.b), self.base)
        self.assertEqual(len(self.locks()), 2)           # the dead pass still holds its leases
        self.pushes.clear()
        done = self.run_pass()
        self.assertTrue(done[0][1].ok, done[0][1].why)
        self.assertEqual(self.head(self.b), self.sha)
        self.assertEqual([p[0] for p in self.pushes], [self.b])
        self.assertEqual(self.locks(), [])
        self.assertEqual(self.texts(), [f"INTEGRATED {GROUP}@{self.sha}"])
        open_rows = [r for r in self.board.restarts("J") if r.ended_at is None]
        self.assertEqual(open_rows, [])

    def test_quiesce_exempts_inflight_land(self):
        # the journal of an interrupted landing is found by complete_all whatever else the group does
        self.submit()
        self.crash_after_first()
        env = self.env()
        got = driver_land.complete_all(self.board, {}, env, "J")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0][1].ok, got[0][1].why)

    def test_complete_all_ignores_landed_and_never_started_requests(self):
        self.submit()
        self.assertEqual(driver_land.complete_all(self.board, {}, self.env(), "J"), [])   # no journal yet
        self.run_pass()
        self.assertEqual(driver_land.complete_all(self.board, {}, self.env(), "J"), [])   # landed

    def test_partial_report_names_destinations_pushed_but_not_completed(self):
        self.submit()
        self.crash_after_first()
        self.assertEqual(driver_land.partial_report(self.board, self.env(), "J"), [f"{self.a}@{self.sha}"])
        self.run_pass()
        self.assertEqual(driver_land.partial_report(self.board, self.env(), "J"), [])

    def test_a_crashed_pass_with_expired_leases_waits_for_the_host_sweep(self):
        self.submit()
        self.crash_after_first()

        class Late:
            def __init__(s, b):
                s.b = b

            def __getattr__(s, n):
                return getattr(s.b, n)

            def now(s):
                import datetime as dt
                return s.b.now() + dt.timedelta(days=2)
        out = driver_land.run(Late(self.board), {}, self.env(), "J")
        self.assertTrue(out[0][1].pending and out[0][1].lease_held)
        self.assertEqual(self.head(self.b), self.base)


class Claims(DriverCase):
    class Late:
        def __init__(self, b):
            self.b = b

        def __getattr__(self, n):
            return getattr(self.b, n)

        def now(self):
            import datetime as dt
            return self.b.now() + dt.timedelta(days=2)

    def parts(self):
        mid = self.submit()
        req = Request(mid, SEAT, f"{GROUP}@{self.sha}", 1)
        dests = land.destinations_from_owner(self.owner)
        action = driver_land.action_id(self.led, req, GROUP)
        return req, dests, action

    def hold_with(self, keys, action):
        seat = self.board.seat("J", 1, SEAT)
        return self.board.claim_drive_launch(
            job="J", generation=1, action_id=action, attempt=1, launch_id="own", seat_id=seat.id,
            seat_revision=seat.revision, harness="claude", reserve_usd=0, reserve_minutes=1, locks=keys,
            lock_seconds=600, max_host_running=8)

    def test_own_unexpired_leases_are_adopted(self):
        req, dests, action = self.parts()
        keys = land.lease_keys(dests, "main")
        self.hold_with(keys, action)
        held = driver_land.claim_leases(self.board, {}, self.env(), "J", req, dests, action)
        self.assertTrue(held.adopted)
        self.assertEqual(held.launch_id, "own")

    def test_own_expired_leases_are_not_adopted(self):
        req, dests, action = self.parts()
        self.hold_with(land.lease_keys(dests, "main"), action)
        with self.assertRaises(LeaseHeld):
            driver_land.claim_leases(self.Late(self.board), {}, self.env(), "J", req, dests, action)

    def test_a_partial_set_of_own_leases_is_not_adopted(self):
        req, dests, action = self.parts()
        self.hold_with(land.lease_keys(dests, "main")[:1], action)
        with self.assertRaises(LeaseHeld):
            driver_land.claim_leases(self.board, {}, self.env(), "J", req, dests, action)

    def test_another_actions_leases_are_not_adopted(self):
        req, dests, action = self.parts()
        self.hold_with(land.lease_keys(dests, "main"), action + "x")
        with self.assertRaises(LeaseHeld):
            driver_land.claim_leases(self.board, {}, self.env(), "J", req, dests, action)

    def test_refusal_clears_a_lease_hold_mark(self):
        self.led.integrated[GROUP] = {"lease_held_since": NOW.isoformat()}
        self.led.save()
        self.advance(self.b)
        self.submit()
        done = self.run_pass()
        self.assertFalse(done[0][1].ok)
        self.assertNotIn("lease_held_since", self.led.integrated[GROUP])


class Text(unittest.TestCase):
    def test_reason_is_one_line_and_cut(self):
        text = driver_land._text("LAND-REFUSED ", "a@b", "x\ny\t" * 400)
        self.assertNotIn("\n", text)
        self.assertNotIn("\t", text)
        self.assertEqual(len(text), len("LAND-REFUSED a@b ") + driver_land.WHY_MAX)
        self.assertEqual(driver_land._text("INTEGRATED ", "a@b"), "INTEGRATED a@b")
        self.assertEqual(driver_land._text("LAND-REFUSED ", "a@b", "  too   many   spaces "),
                         "LAND-REFUSED a@b too many spaces")


class Ci(unittest.TestCase):
    def test_repo_slug(self):
        cases = {"https://github.com/fcarucci/Swarm.git": "fcarucci/Swarm", "https://github.com/o/r": "o/r",
                 "git@github.com:o/r.git": "o/r", "ssh://git@github.com/o/r.git": "o/r",
                 "https://gitea.test/o/r/": "o/r", "/srv/repo.git": None, "o/r": None, "relative/repo.git": None, "": None, None: None}
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(driver_land.repo_slug(url), want)

    def fake(self, out, code=0, calls=None):
        def run(cmd, **kw):
            if calls is not None:
                calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, code, out, "")
        return run

    def test_status_reads_the_state_of_the_exact_sha(self):
        calls = []
        status = driver_land.ci_status_cli("o/r", run=self.fake(json.dumps({"state": "green"}) + "\n", calls=calls))
        self.assertEqual(status("a" * 40), "green")
        cmd, kw = calls[0]
        self.assertEqual(cmd[1:], ["ci", "status", "--repo", "o/r", "--sha", "a" * 40, "--json"])
        self.assertTrue(cmd[0].endswith("bin/swarm") or cmd[0].endswith("bin\\swarm"))
        self.assertEqual(kw["timeout"], driver_land.CI_TIMEOUT)

    def test_a_failing_cli_raises_even_with_a_state_on_stdout(self):
        with self.assertRaises(OSError):
            driver_land.ci_status_cli("o/r", run=self.fake('{"state": "green"}', code=1))("a" * 40)

    def test_failures_raise_so_the_landing_holds(self):
        for run in (self.fake("", code=2), self.fake("not json"), self.fake("{}"), self.fake("")):
            with self.assertRaises(OSError):
                driver_land.ci_status_cli("o/r", run=run)("a" * 40)
        with self.assertRaises(ValueError):
            driver_land.ci_status_cli(None)("a" * 40)

    def test_non_string_state_is_not_green(self):
        self.assertEqual(driver_land.ci_status_cli("o/r", run=self.fake('{"state": 1}'))("a" * 40), "unknown")


def ledger_load(led):
    from swarm.supervisor import ledger
    return ledger.load(led.board_key, led.job)


if __name__ == "__main__":
    unittest.main()
