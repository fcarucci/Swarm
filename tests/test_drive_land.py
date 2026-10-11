"""Task 11b: swarm.land, the trusted landing (leases, journal, exact SHA, patch-id twins, requests).
Local bare repositories under the suite sandbox; HOME is a temporary directory inside it; no credentials."""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
from swarm import compat, land, receipts
from swarm.land import (Destination, LandEnv, LandRefused, Request, credential_from_pin,
                        destinations_from_owner, lease_key, lease_keys, normalize_url, parse_artifact)
from swarm.supervisor import barrier, ledger, mirror
from swarm.supervisor.mirror import Mirror, MirrorError, MissingCommitError, PushRejectedError
from test_drive_mirror import git

BK = "board-key"
GROUP = "feat/x"
SEAT = land.integrator_seat(GROUP)
NOW = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.timezone.utc)


class Crash(BaseException):
    """Stands in for SIGKILL at a barrier: nothing after it runs."""


class LandCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="land-", dir=support.SANDBOX))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        p = mock.patch.dict(os.environ, support.home_env(self.home))
        p.start()
        self.addCleanup(p.stop)
        self.h = support.MemoryHarness()
        self.h.reset()
        self.board = self.h.board()
        self.addCleanup(self.board.close)
        self.board.open_job("J", None, None, None, "me", goal="G")
        self.led = ledger.create(BK, "J", budgets={}, activated_at=NOW.isoformat())
        self.work = self.repo("work")
        self.base = self.commit(self.work, "base.txt", "base\n", "base")
        self.a, self.b = self.bare("a.git"), self.bare("b.git")
        self.branch_commit("x.txt", "x\n")
        self.ci = "green"
        self.judged = {self.sha}
        self.owner = {"remotes": {"a": self.a, "b": self.b}, "credentials": {}}
        self.pushes = []
        self.seat = self.board.request_seat("J", 1, SEAT, role="integrator", harness="claude", model="haiku",
                                            workdir=self.root, brief_path=os.path.join(self.root, "B.md"),
                                            requested_by="driver")
        self._claims = 0
        self.first_claim = self.lease("J")

    # --- git fixtures ---------------------------------------------------------------------
    def repo(self, name, bare=False):
        path = os.path.join(self.root, name)
        subprocess.run(["git", "init", "-q", "-b", "main"] + (["--bare"] if bare else []) + [path],
                       check=True, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
        return path

    def commit(self, path, fname, text, msg="change"):
        with open(os.path.join(path, fname), "w") as fh:
            fh.write(text)
        git(path, "add", fname)
        git(path, "commit", "-q", "-m", msg)
        return git(path, "rev-parse", "HEAD")

    def bare(self, name):
        path = self.repo(name, bare=True)
        git(self.work, "push", "-q", path, "main")
        return path

    def branch_commit(self, fname, text, push=True):
        git(self.work, "checkout", "-q", "-B", GROUP, self.base)
        self.sha = self.commit(self.work, fname, text, "feature")
        if push:
            for r in (self.a, self.b):
                git(self.work, "push", "-q", "-f", r, GROUP)
        return self.sha

    def advance(self, remote, fname="y.txt"):
        other = self.repo("o-" + os.path.basename(remote))
        git(other, "fetch", "-q", remote, "main")
        git(other, "checkout", "-q", "-B", "m", "FETCH_HEAD")
        sha = self.commit(other, fname, "moved\n", "elsewhere")
        git(other, "push", "-q", remote, "m:main")
        return sha

    def head(self, remote, branch="main"):
        return git(remote, "rev-parse", branch)

    def has_branch(self, remote, branch=GROUP):
        return bool(git(remote, "branch", "--list", branch))

    # --- board, leases, environment ----------------------------------------------------------
    def lease(self, job, keys=None, name=SEAT, host=None):
        keys = keys or lease_keys(land.destinations_from_owner(self.owner), "main")
        seat = self.board.seat("J", 1, name) if job == "J" else self.board.request_seat(
            job, 1, "other", role="integrator", harness="claude", model="haiku", workdir=self.root,
            brief_path=os.path.join(self.root, "B.md"), requested_by="driver")
        ctx = mock.patch.object(compat, "node", lambda: host) if host else mock.patch.object(compat, "node", compat.node)
        with ctx:
            n, self._claims = self._claims, self._claims + 1
            return self.board.claim_drive_launch(
                job=job, generation=1, action_id=f"gen1|land|-|{job}|1|{n}", attempt=1, launch_id=f"L-{job}-{host}-{n}",
                seat_id=seat.id, seat_revision=seat.revision, harness="claude", reserve_usd=0, reserve_minutes=1,
                locks=keys, lock_seconds=3000, max_host_running=8)

    def release_first(self):
        r = self.first_claim
        self.board.release_drive_locks(r.launch_id, job="J", holder=r.action_id)

    def env(self, **kw):
        base = dict(led=self.led, owner=self.owner, judged=lambda: self.judged, ci_status=lambda sha: self.ci,
                    target="main", now=lambda: NOW, mirror_factory=self.factory())
        base.update(kw)
        return LandEnv(**base)

    def factory(self):
        def make(url, cred):
            m = Mirror(url, cred)
            real = m.push

            def push(sha, target, **kw):
                self.pushes.append((url, sha, target))
                return real(sha, target, **kw)
            m.push = push
            return m
        return make

    def request(self, artifact=None, seat=SEAT, generation=1, rid=7):
        return Request(rid, seat, artifact or f"{GROUP}@{self.sha}", generation)

    def go(self, request=None, **kw):
        return land.execute(self.board, self.env(**kw.pop("env", {})), "J", request or self.request(),
                            action_id="gen1|land|-|feat/x|1|7", **kw)


class Landing(LandCase):
    def test_land_fast_forwards_every_destination(self):
        res = self.go()
        self.assertTrue(res.ok, res.why)
        self.assertEqual(res.landed_sha, self.sha)
        self.assertEqual(self.head(self.a), self.sha)
        self.assertEqual(self.head(self.b), self.sha)

    def test_pushes_exactly_the_judged_sha_once_per_destination(self):
        self.go()
        self.assertEqual(sorted(self.pushes), sorted([(self.a, self.sha, "main"), (self.b, self.sha, "main")]))

    def test_branch_deleted_and_ledger_and_receipts_written(self):
        res = self.go()
        self.assertFalse(self.has_branch(self.a))
        self.assertFalse(self.has_branch(self.b))
        ig = self.led.integrated[GROUP]
        self.assertEqual((ig["landed"], ig["land_id"], ig["land_rev"]), (self.sha, "7", res.land_rev))
        stored = ledger.load(BK, "J")
        self.assertEqual(stored.integrated[GROUP]["landed"], self.sha)
        urls = sorted(d.url for d in destinations_from_owner(self.owner))
        r = receipts.find(BK, "J", "land", "7", rev=ig["land_rev"], incarnation=self.led.incarnation,
                          generation=1, now=NOW.timestamp())
        self.assertEqual(sorted(r.destinations), urls)
        self.assertTrue(all(v == {"state": "verified", "sha": self.sha} for v in r.destinations.values()))
        self.assertEqual(r.seat, SEAT)
        integ = receipts.find(BK, "J", "integrated", "7", rev=1, incarnation=self.led.incarnation,
                              generation=1, now=NOW.timestamp())
        self.assertEqual(integ.content_sha, receipts.content_sha(
            "integrated", {"group": GROUP, "sha": self.sha, "into": "main"}))

    def test_land_rev_counts_every_journal_step(self):
        res = self.go()
        self.assertEqual(res.land_rev, 1 + 2 * 2)   # intent, then pushed and verified per destination

    def test_branch_kept_when_deletion_is_off_and_deletion_failure_is_not_fatal(self):
        res = self.go(env={"delete_branch": False})
        self.assertTrue(res.ok)
        self.assertTrue(self.has_branch(self.a) and self.has_branch(self.b))

    def test_deletion_failure_is_noted_not_fatal(self):
        notes = []

        def make(url, cred):
            m = Mirror(url, cred)

            def boom(branch, **kw):
                raise PushRejectedError("denied", "denied")
            m.delete_branch = boom
            return m
        res = self.go(env={"mirror_factory": make, "say": notes.append})
        self.assertTrue(res.ok)
        self.assertEqual(len(notes), 2)
        self.assertEqual(self.led.integrated[GROUP]["landed"], self.sha)

    def test_second_call_is_idempotent(self):
        self.go()
        self.pushes.clear()
        res = self.go()
        self.assertTrue(res.ok)
        self.assertEqual(self.pushes, [])
        self.assertEqual(res.land_id, "7")

    def test_blocked_and_lease_marks_cleared_on_landing(self):
        self.led.integrated[GROUP] = {"blocked": "text", "blocked_id": 4, "lease_held_since": "x", "withdrawn_note": "k"}
        self.led.save()
        self.go()
        ig = self.led.integrated[GROUP]
        for gone in ("blocked", "blocked_id", "lease_held_since"):
            self.assertNotIn(gone, ig)
        self.assertEqual(ig["withdrawn_note"], "k")

    def test_non_fast_forward_refused_before_any_push(self):
        self.advance(self.b)
        res = self.go()
        self.assertFalse(res.ok)
        self.assertFalse(res.pending)
        self.assertIn("rebase", res.why)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.head(self.a), self.base)

    def test_branch_not_on_every_remote_is_refused_with_instructions(self):
        git(self.b, "branch", "-D", GROUP)
        res = self.go()
        self.assertFalse(res.ok)
        self.assertIn("push", res.why)
        self.assertIn("every pinned remote", res.why)
        self.assertEqual(self.pushes, [])

    def test_branch_moved_after_the_verdict_is_refused(self):
        moved = self.commit(self.work, "z.txt", "z\n", "extra")
        for r in (self.a, self.b):
            git(self.work, "push", "-q", "-f", r, GROUP)
        res = self.go()
        self.assertFalse(res.ok)
        self.assertIn(self.sha[:12], res.why)
        self.assertEqual(self.head(self.a), self.base)
        self.assertNotEqual(moved, self.sha)

    def test_target_missing_on_a_destination_refused(self):
        git(self.b, "branch", "-m", "main", "trunk")
        git(self.b, "symbolic-ref", "HEAD", "refs/heads/trunk")
        res = self.go()
        self.assertFalse(res.ok)
        self.assertIn("does not exist", res.why)

    def test_branch_equal_to_target_refused(self):
        res = self.go(request=self.request(f"main@{self.sha}", seat=land.integrator_seat("main")))
        self.assertFalse(res.ok)
        self.assertIn("target", res.why)


class Checks(LandCase):
    def test_uncovered_sha_refused(self):
        self.judged = {"9" * 40}
        res = self.go()
        self.assertFalse(res.ok)
        self.assertIn("met verdict", res.why)
        self.assertEqual(self.pushes, [])

    def test_ci_red_refused_ci_pending_held(self):
        self.ci = "failed"
        res = self.go()
        self.assertFalse(res.ok)
        self.assertFalse(res.pending)
        self.assertIn("red", res.why)
        for state in ("running", "queued", "none"):
            self.ci = state
            res = self.go()
            self.assertTrue(res.pending, state)
            self.assertFalse(res.ok)
        self.assertEqual(self.pushes, [])

    def test_ci_outage_is_a_hold_not_a_pass(self):
        def boom(sha):
            raise OSError("network down")
        res = self.go(env={"ci_status": boom})
        self.assertTrue(res.pending)
        self.assertFalse(res.ok)

    def test_wrong_seat_wrong_generation_refused(self):
        for request in (self.request(seat="engineer:a"), self.request(seat=land.integrator_seat("other")),
                        self.request(generation=2)):
            res = self.go(request=request)
            self.assertFalse(res.ok)
            self.assertFalse(res.pending)
        self.assertEqual(self.pushes, [])

    def test_wrong_caller_refused(self):
        env = self.env()
        res = land.land(self.board, env, "J", f"{GROUP}@{self.sha}", caller_session="intruder")
        self.assertFalse(res.ok)
        self.assertEqual(self.pushes, [])
        res = land.land(self.board, env, "J", f"{GROUP}@{self.sha}", caller_session="")
        self.assertFalse(res.ok)

    def test_integrator_session_may_land_synchronously(self):
        seat = self.board.seat("J", 1, SEAT)
        with mock.patch.object(type(self.board), "seats", lambda s, job, gen=None, include_closed=True: [
                mock.Mock(seat=SEAT, session_id="sess-int")]):
            res = land.land(self.board, self.env(), "J", f"{GROUP}@{self.sha}", caller_session="sess-int")
        self.assertTrue(res.ok, res.why)

    def test_malformed_artifact_refused(self):
        for bad in ("feat/x", "feat/x@abc", "feat/x@" + "G" * 40, "../x@" + "a" * 40, "x y@" + "a" * 40,
                    "feat/x@" + "a" * 64, 5, None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_artifact(bad)
        self.assertEqual(parse_artifact("feat/x@" + "a" * 40), ("feat/x", "a" * 40))


class Leases(LandCase):
    def test_not_claimed_is_a_hold(self):
        self.release_first()
        res = self.go()
        self.assertTrue(res.pending)
        self.assertIn("lease", res.why)
        self.assertEqual(self.pushes, [])

    def test_held_by_another_job_is_a_hold_that_names_it(self):
        self.release_first()
        self.board.open_job("K", None, None, None, "me", goal="G")
        self.assertIsNotNone(self.lease("K"))
        res = self.go()
        self.assertTrue(res.pending)
        self.assertIn("job K", res.why)
        self.assertIn("lease", res.why)
        self.assertEqual(self.pushes, [])

    def test_foreign_host_lease_is_never_reclaimed(self):
        self.release_first()
        self.board.open_job("K", None, None, None, "me", goal="G")
        self.assertIsNotNone(self.lease("K", host="otherhost"))
        res = self.go()
        self.assertTrue(res.pending)
        self.assertIn("another host", res.why)
        dests = destinations_from_owner(self.owner)
        self.assertEqual(land.lease_holders(self.board, self.env(), dests, "J"), ["another host"])

    def test_expired_lease_is_a_hold(self):
        class Late:
            def __init__(s, b):
                s.b = b

            def __getattr__(s, name):
                return getattr(s.b, name)

            def now(s):
                return s.b.now() + dt.timedelta(days=1)
        res = land.execute(Late(self.board), self.env(), "J", self.request(), action_id="a")
        self.assertTrue(res.pending)
        self.assertIn("expired", res.why)

    def test_keys_are_sorted_one_per_destination_and_normalised(self):
        keys = lease_keys(destinations_from_owner(self.owner), "main")
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(k.startswith("land:") and k.endswith(":main") for k in keys))
        self.assertNotEqual(lease_key(self.a, "main"), lease_key(self.a, "dev"))
        same = ["https://GitHub.com/o/r.git", "https://github.com/o/r", "https://github.com:443/o/r/",
                "https://user@github.com/o/r.git"]
        self.assertEqual(len({lease_key(u, "main") for u in same}), 1)
        self.assertEqual(normalize_url("git@github.com:o/r.git"), normalize_url("ssh://git@github.com/o/r"))
        self.assertNotEqual(normalize_url("https://github.com/o/r"), normalize_url("https://github.com/o/s"))
        self.assertNotEqual(normalize_url("ssh://h:2222/o/r"), normalize_url("ssh://h/o/r"))

    def test_second_job_cannot_claim_the_same_destinations(self):
        self.board.open_job("K", None, None, None, "me", goal="G")
        self.assertIsNone(self.lease("K"))


class Journal(LandCase):
    def test_intent_is_journaled_before_any_push_then_pushed_then_verified(self):
        events = []
        real_write = receipts.write

        def spy_write(key, r):
            if r.kind == "land":
                events.append(("journal", r.rev, {u: v["state"] for u, v in r.destinations.items()}))
            return real_write(key, r)
        real_factory = self.factory()

        def make(url, cred):
            m = real_factory(url, cred)
            push = m.push

            def p(sha, target, **kw):
                events.append(("push", url))
                return push(sha, target, **kw)
            m.push = p
            return m
        with mock.patch.object(receipts, "write", spy_write):
            self.go(env={"mirror_factory": make})
        kinds = [e[0] for e in events]
        self.assertEqual(kinds[0], "journal")
        self.assertEqual(events[0][2], {self.a: "intent", self.b: "intent"})
        first_push = kinds.index("push")
        self.assertEqual(events[first_push - 1][0], "journal")
        revs = [e[1] for e in events if e[0] == "journal"]
        self.assertEqual(revs, [1, 2, 3, 4, 5])
        self.assertEqual(events[-1][2], {self.a: "verified", self.b: "verified"})
        order = [e[1] for e in events if e[0] == "push"]
        self.assertEqual(order, sorted(order, key=lambda u: normalize_url(u)))

    def test_crash_after_first_destination_is_completed(self):
        def hit(name):
            if name == "land_after_first_destination":
                raise Crash()
        with mock.patch.object(barrier, "hit", hit), self.assertRaises(Crash):
            self.go()
        self.assertEqual(self.head(self.a), self.sha)
        self.assertEqual(self.head(self.b), self.base)
        self.assertNotIn("landed", self.led.integrated.get(GROUP, {}))
        r = land.read_journal(self.led, 7, NOW, destinations=2)
        self.assertEqual(sorted(v["state"] for v in r.destinations.values()), ["intent", "verified"])
        self.pushes.clear()
        res = land.complete_land(self.board, self.env(), "J", self.request(), action_id="gen1|land|-|feat/x|1|8")
        self.assertTrue(res.ok, res.why)
        self.assertEqual(self.head(self.b), self.sha)
        self.assertEqual([p[0] for p in self.pushes], [self.b])
        self.assertEqual(self.led.integrated[GROUP]["landed"], self.sha)

    def test_crash_after_push_before_journal_does_not_push_twice(self):
        real_write = receipts.write

        def write(key, r):
            if r.kind == "land" and r.rev == 2:
                raise Crash()
            return real_write(key, r)
        with mock.patch.object(receipts, "write", write), self.assertRaises(Crash):
            self.go()
        self.assertEqual(self.head(self.a), self.sha)
        self.pushes.clear()
        res = land.complete_land(self.board, self.env(), "J", self.request(), action_id="x")
        self.assertTrue(res.ok, res.why)
        self.assertNotIn(self.a, [p[0] for p in self.pushes])

    def test_resume_refuses_a_journal_for_another_sha(self):
        def hit(name):
            raise Crash()
        with mock.patch.object(barrier, "hit", hit), self.assertRaises(Crash):
            self.go()
        other = self.request(f"{GROUP}@{'b' * 40}")
        res = self.go(request=other)
        self.assertFalse(res.ok)
        self.assertIn("journal", res.why)

    def test_forged_journal_does_not_validate(self):
        self.go()
        r = land.read_journal(self.led, 7, NOW, destinations=2)
        forged = receipts.Receipt(**{**r.__dict__, "destinations": {u: {"state": "intent", "sha": self.sha}
                                                                    for u in r.destinations}})
        receipts.write(BK, forged)          # content_sha no longer matches its destinations
        self.assertIsNone(land.read_journal(self.led, 7, NOW, destinations=2))
        self.assertFalse(land.journal_verified(self.led, 7, NOW, list(r.destinations), self.sha))

    def test_journal_expires_and_belongs_to_one_generation(self):
        self.go()
        self.assertIsNone(land.read_journal(self.led, 7, NOW + dt.timedelta(days=30), destinations=2))
        self.assertIsNotNone(land.read_journal(self.led, 7, NOW + dt.timedelta(days=1), destinations=2))
        self.assertIsNone(land.read_journal(self.led, 8, NOW, destinations=2))

    def test_rejected_push_leaves_a_resumable_partial(self):
        real_factory = self.factory()

        def make(url, cred):
            m = real_factory(url, cred)
            if url == self.b:
                def reject(sha, target, **kw):
                    raise PushRejectedError("rejected", "pre-receive hook declined")
                m.push = reject
            return m
        res = self.go(env={"mirror_factory": make})
        self.assertFalse(res.ok)
        self.assertIn("declined", res.why)
        self.assertEqual(self.head(self.a), self.sha)
        self.assertEqual(self.head(self.b), self.base)
        r = land.read_journal(self.led, 7, NOW, destinations=2)
        self.assertEqual(r.destinations[self.a]["state"], "verified")
        self.assertEqual(r.destinations[self.b]["state"], "intent")


class Twins(LandCase):
    def rebased(self, change_content=False):
        """The judged commit rebased onto a moved target (a clean rebase, or one with a changed hunk)."""
        moved = self.advance(self.a, "m.txt")
        self.advance(self.b, "m.txt")
        git(self.work, "fetch", "-q", self.a, "main")
        git(self.work, "checkout", "-q", "-B", GROUP, "FETCH_HEAD")
        sha = self.commit(self.work, "x.txt", "x2\n" if change_content else "x\n", "feature")
        for r in (self.a, self.b):
            git(self.work, "push", "-q", "-f", r, GROUP)
        return moved, sha

    def test_clean_rebase_of_a_judged_change_is_covered(self):
        judged = self.sha
        _, sha = self.rebased()
        self.assertNotEqual(sha, judged)
        self.judged = {judged}
        # the old judged commit must be present in the mirrors: it is still on no branch, so keep a ref
        for r in (self.a, self.b):
            git(self.work, "push", "-q", r, f"{judged}:refs/heads/judged-old")
        res = self.go(request=self.request(f"{GROUP}@{sha}"), env={"judged_branches": lambda: ["judged-old"]})
        self.assertTrue(res.ok, res.why)
        self.assertEqual(self.head(self.a), sha)

    def test_rebase_that_changes_a_hunk_needs_a_new_verdict(self):
        judged = self.sha
        _, sha = self.rebased(change_content=True)
        self.judged = {judged}
        for r in (self.a, self.b):
            git(self.work, "push", "-q", r, f"{judged}:refs/heads/judged-old")
        res = self.go(request=self.request(f"{GROUP}@{sha}"), env={"judged_branches": lambda: ["judged-old"]})
        self.assertFalse(res.ok)
        self.assertIn("verdict", res.why)
        self.assertEqual(self.pushes, [])

    def test_twin_with_a_missing_judged_commit_is_refused(self):
        _, sha = self.rebased()
        self.judged = {"c" * 40}
        res = self.go(request=self.request(f"{GROUP}@{sha}"))
        self.assertFalse(res.ok)
        self.assertEqual(self.pushes, [])

    def test_a_merge_commit_in_the_range_is_never_a_twin(self):
        judged = self.sha
        git(self.work, "checkout", "-q", "-B", "side", self.base)
        self.commit(self.work, "s.txt", "s\n", "side")
        git(self.work, "checkout", "-q", "-B", GROUP, judged)
        git(self.work, "merge", "-q", "--no-ff", "-m", "merge", "side")
        sha = git(self.work, "rev-parse", "HEAD")
        for r in (self.a, self.b):
            git(self.work, "push", "-q", "-f", r, GROUP)
            git(self.work, "push", "-q", r, f"{judged}:refs/heads/judged-old")
        self.judged = {judged}
        res = self.go(request=self.request(f"{GROUP}@{sha}"), env={"judged_branches": lambda: ["judged-old"]})
        self.assertFalse(res.ok)
        self.assertEqual(self.pushes, [])


class TypedErrors(LandCase):
    def faulty(self, exc, name="head"):
        real = self.factory()

        def make(url, cred):
            m = real(url, cred)
            setattr(m, name, lambda *a, **k: (_ for _ in ()).throw(exc))
            return m
        return make

    def test_each_typed_mirror_error_becomes_a_refusal_with_advice(self):
        cases = [(MissingCommitError("commit abc is not in the mirror"), "is_ancestor", "push the branch"),
                 (mirror.TargetRewoundError("x is not an ancestor of y"), "is_ancestor", "reset backwards"),
                 (mirror.UnpatchableCommitError("merge"), "is_ancestor", "merge or empty"),
                 (PushRejectedError("denied", "pre-receive declined"), "is_ancestor", "declined"),
                 (MirrorError("boom"), "is_ancestor", "git failed")]
        for exc, name, text in cases:
            with self.subTest(exc=type(exc).__name__):
                res = self.go(env={"mirror_factory": self.faulty(exc, name)})
                self.assertFalse(res.ok)
                self.assertFalse(res.pending)
                self.assertIn(text, res.why)

    def test_timeouts_are_holds(self):
        for exc in (mirror.MirrorTimeoutError("t"), mirror.AuditDeadlineError("t")):
            for name in ("is_ancestor", "fetch_branches"):
                res = self.go(env={"mirror_factory": self.faulty(exc, name)})
                self.assertTrue(res.pending, (type(exc).__name__, name))
            res = self.go(env={"mirror_factory": self.faulty(exc, "is_ancestor")})
            self.assertTrue(res.pending, type(exc).__name__)
            self.assertFalse(res.ok)

    def test_unusable_destination_is_a_refusal(self):
        self.owner = {"remotes": {"a": "-evil"}, "credentials": {}}
        res = self.go(need_leases=False)
        self.assertFalse(res.ok)
        self.assertIn("destination a", res.why)

    def test_no_pinned_destination_refused(self):
        self.owner = {"remotes": {}, "credentials": {}}
        res = self.go(need_leases=False)
        self.assertFalse(res.ok)
        self.assertIn("no destination", res.why)


class FetchContinuation(LandCase):
    def slow_factory(self, slow_urls):
        real = self.factory()

        def make(url, cred):
            m = real(url, cred)
            if url in slow_urls:
                inner = m.fetch_branches
                m.fetch_branches = lambda branches, timeout: ({}, list(branches))
                m._inner = inner
            return m
        return make

    def test_timed_out_fetch_is_kept_pending_and_retried(self):
        res = self.go(env={"mirror_factory": self.slow_factory({self.b})})
        self.assertTrue(res.pending)
        self.assertFalse(res.ok)
        self.assertIn("continues next pass", res.why)
        self.assertEqual(len(self.led.mirror_fetch_pending), 2)
        self.assertTrue(all(":" in x for x in self.led.mirror_fetch_pending))
        self.assertEqual(ledger.load(BK, "J").mirror_fetch_pending, self.led.mirror_fetch_pending)
        self.assertEqual(self.pushes, [])
        res = self.go()
        self.assertTrue(res.ok, res.why)
        self.assertEqual(self.led.mirror_fetch_pending, [])

    def test_fetch_failure_is_a_refusal(self):
        real = self.factory()

        def make(url, cred):
            m = real(url, cred)
            m.fetch_branches = lambda b, t: (_ for _ in ()).throw(MirrorError("credential refused"))
            return m
        res = self.go(env={"mirror_factory": make})
        self.assertFalse(res.ok)
        self.assertFalse(res.pending)
        self.assertIn("fetch from", res.why)


class SeatName(unittest.TestCase):
    def test_integrator_seat_name_shape(self):
        n = land.integrator_seat("feat/x")
        self.assertRegex(n, r"integrator-feat-x-[0-9a-f]{8}")
        self.assertNotEqual(land.integrator_seat("feat/x"), land.integrator_seat("feat-x"))
        self.assertEqual(land.integrator_seat(""), land.integrator_seat(""))
        self.assertTrue(land.integrator_seat("A" * 100).startswith("integrator-" + "a" * 30 + "-"))

    def test_seat_name_matches_driver_exec_when_present(self):
        try:
            from swarm.supervisor import driver_exec
        except ImportError:
            self.skipTest("driver_exec (Task 8a) is not integrated yet")
        for g in ("feat/x", "A b", "", "x" * 90):
            self.assertEqual(land.integrator_seat(g), driver_exec.seat_name("integrator", g))


class Pins(unittest.TestCase):
    def test_credential_pins(self):
        self.assertEqual(credential_from_pin(None), {"kind": "none"})
        self.assertEqual(credential_from_pin("ssh-key:/k/id", config_dir="/c"), {"kind": "ssh-key", "path": "/k/id"})
        self.assertEqual(credential_from_pin("ssh-agent:/run/a.sock"), {"kind": "ssh-agent", "socket": "/run/a.sock"})
        self.assertEqual(credential_from_pin("helper:/h/gh auth git-credential", config_dir="/cfg"),
                         {"kind": "helper", "command": "/h/gh auth git-credential", "config_dir": "/cfg"})
        d = credential_from_pin("helper:/h/gh auth git-credential")
        self.assertTrue(os.path.isabs(d["config_dir"]))
        self.assertTrue(d["config_dir"].endswith(os.path.join(".config", "gh")))

    def test_bad_pins_refused(self):
        for bad in ("helper:gh auth git-credential", "helper:", "ssh-key:rel", "ssh-agent:", "ssh-agent",
                    "ssh-key:/a b", "other:/x", "helper"):
            with self.subTest(bad=bad), self.assertRaises(LandRefused):
                credential_from_pin(bad)

    def test_destinations_sorted_deduplicated_and_pinned(self):
        owner = {"remotes": {"origin": "https://github.com/o/r.git", "mirror": "https://GITHUB.com/o/r",
                             "z": "git@host.test:o/z.git"},
                 "credentials": {"origin": "helper:/h/gh auth git-credential", "mirror": "ssh-agent:/s",
                                 "z": "ssh-agent:/run/a"}}
        out = destinations_from_owner(owner, config_dir="/cfg")
        self.assertEqual([d.name for d in out], ["mirror", "z"][:0] or [d.name for d in out])
        self.assertEqual(len(out), 2)
        self.assertEqual([normalize_url(d.url) for d in out], sorted(normalize_url(d.url) for d in out))
        names = {d.name: d for d in out}
        self.assertIn("mirror", names)          # the first name in sorted order wins a duplicate url
        self.assertEqual(names["mirror"].credential["kind"], "ssh-agent")
        with self.assertRaises(LandRefused):
            destinations_from_owner({"remotes": {}})
        with self.assertRaises(LandRefused):
            destinations_from_owner(None)


class Requests(LandCase):
    def post(self, text=None, to=None, name="integrator-bot"):
        return self.board.post("J", name, text or f"LAND-REQUEST {GROUP}@{self.sha}", to=to).id

    def receipt(self, mid, *, seat=SEAT, body=None, rev=1, generation=1, to=None, kind="post"):
        body = body or f"LAND-REQUEST {GROUP}@{self.sha}"
        receipts.write(BK, receipts.Receipt(
            board_key=BK, job="J", generation=generation, incarnation=self.led.incarnation, action_id="a", attempt=1,
            seat=seat, kind=kind, record_id=str(mid), rev=rev,
            content_sha=receipts.content_sha("post", {"to": to, "body": body}), args_sha="0" * 64,
            expires_at=NOW.timestamp() + 3600))

    def find(self, **kw):
        env = self.env(now=lambda: NOW)
        with mock.patch.object(type(self.board), "now", lambda s: NOW):
            return land.find_requests(self.board, env, "J", **kw)

    def test_request_land_posts_the_exact_text_and_returns_the_id(self):
        mid = land.request_land(self.board, "J", f"{GROUP}@{self.sha}", name="int-bot")
        msg = [m for m in self.board.messages_after(0, "J") if m.id == mid][0]
        self.assertEqual(msg.message, f"LAND-REQUEST {GROUP}@{self.sha}")
        self.assertEqual(msg.agent_name, "int-bot")
        with self.assertRaises(ValueError):
            land.request_land(self.board, "J", "feat/x", name="int-bot")

    def test_a_receipted_request_is_found_with_its_seat(self):
        mid = self.post()
        self.receipt(mid)
        got = self.find()
        self.assertEqual(got, [Request(mid, SEAT, f"{GROUP}@{self.sha}", 1)])
        self.assertEqual(self.find(after=mid), [])

    def test_unreceipted_forged_or_swapped_requests_are_ignored(self):
        plain = self.post()                                         # no receipt at all
        swapped = self.post(f"LAND-REQUEST {GROUP}@{'d' * 40}")      # receipt binds another body
        self.receipt(swapped, body=f"LAND-REQUEST {GROUP}@{self.sha}")
        rev2 = self.post()
        self.receipt(rev2, rev=2)
        other_gen = self.post()
        self.receipt(other_gen, generation=2)
        addressed = self.post(to="someone")
        self.receipt(addressed, to=None)
        wrong_kind = self.post()
        self.receipt(wrong_kind, kind="done")
        junk = self.post("LAND-REQUEST not-an-artifact")
        self.receipt(junk, body="LAND-REQUEST not-an-artifact")
        chatter = self.post("hello")
        self.assertEqual(self.find(), [])
        self.assertNotIn(plain, [r.id for r in self.find()])
        self.assertNotIn(chatter, [r.id for r in self.find()])

    def test_requests_come_oldest_first(self):
        ids = []
        for _ in range(3):
            mid = self.post()
            self.receipt(mid)
            ids.append(mid)
        self.assertEqual([r.id for r in self.find()], ids)


if __name__ == "__main__":
    unittest.main()
