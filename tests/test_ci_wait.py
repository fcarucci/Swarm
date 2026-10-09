"""`swarm ci status|wait` (skills/ci/ci_wait.py): one shared poller, the 60 s floor and
backoff, the 403 fallback, the low-budget slowdown, exit codes and event short-circuit. All mocked:
a fake host, a fake clock, a temp cache. No network, no gh."""
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from support import ROOT  # noqa: F401  (sets sys.path)

_spec = importlib.util.spec_from_file_location("ci_wait_under_test", ROOT / "skills/ci/ci_wait.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

REPO, SHA = "me/proj", "a" * 40


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class FakeHost:
    """Returns the scripted results in order (the last one repeats); an item may be an exception."""

    def __init__(self, *script, remaining=None):
        self.script, self.calls, self.public_calls, self.rate_calls, self.remaining = list(script), 0, 0, 0, remaining
        self.lock = threading.Lock()

    def fetch(self, repo, sha, public=False):
        with self.lock:
            i = min(self.calls, len(self.script) - 1)
            self.calls += 1
            self.public_calls += bool(public)
        item = self.script[i]
        if isinstance(item, Exception):
            raise item
        return cw.result(item, runs=[{"id": 1, "name": "ci", "status": "completed" if item in ("green", "failed") else "queued",
                                      "conclusion": {"green": "success", "failed": "failure"}.get(item), "url": "u"}],
                         remaining=self.remaining)

    def rate_limit(self):
        self.rate_calls += 1
        return (self.remaining if self.remaining is not None else 4000), 2_000_000

    def failure_detail(self, repo, sha, res, public=False):
        return [{"name": "ci / unit", "url": "u", "id": 7}], "line1\nboom"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swarm-ci-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.clock = Clock()

    def poller(self, host, **kw):
        return cw.Poller(host, self.tmp, now=self.clock.now, sleep=self.clock.sleep, **kw)

    def wait(self, poller, timeout=3600, events=None, as_json=False):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cw.run_wait(poller, REPO, SHA, timeout, as_json, events)
        return rc, out.getvalue(), err.getvalue()


class SharedPollerTests(Base):
    def test_two_concurrent_waits_make_one_poller(self):
        host = FakeHost("running", "running", "green")
        results = []

        def waiter():
            p = cw.Poller(host, self.tmp, floor=0.3, backoff=(0.3,))   # real clock, tiny floor
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                results.append(cw.run_wait(p, REPO, SHA, 20))
        ts = [threading.Thread(target=waiter) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(results, [0, 0])
        # one poller: 3 calls total, not 3 per waiter (the floor is per repo, not per process)
        self.assertEqual(host.calls, 3)

    def test_second_process_reads_the_cache_without_calling(self):
        host = FakeHost("green")
        self.poller(host).poll(REPO, SHA)
        other = FakeHost("failed")
        res = self.poller(other).poll(REPO, SHA)
        self.assertEqual((res["state"], other.calls), ("green", 0))

    def test_cache_layout(self):
        self.poller(FakeHost("green")).poll(REPO, SHA)
        self.assertTrue((self.tmp / REPO / f"{SHA}.json").is_file())
        self.assertTrue((self.tmp / REPO / ".lock").is_file())

    def test_held_lock_means_no_call(self):
        host = FakeHost("green")
        with cw._Lock(self.tmp / REPO / ".lock") as lk:
            self.assertTrue(lk.held)
            self.assertIsNone(self.poller(host).poll(REPO, SHA))
        self.assertEqual(host.calls, 0)


class FloorAndBackoffTests(Base):
    def test_floor_one_call_per_repo_per_60s_across_shas(self):
        host = FakeHost("running")
        p = self.poller(host)
        p.poll(REPO, SHA)
        p.poll(REPO, "b" * 40)
        self.clock.t += 59
        p.poll(REPO, SHA)
        self.assertEqual(host.calls, 1)
        self.clock.t += 2
        p.poll(REPO, SHA)
        self.assertEqual(host.calls, 2)

    def test_queued_backs_off_60_120_300_cap(self):
        host = FakeHost("queued")
        p = self.poller(host)
        gaps = []
        for _ in range(5):
            before = self.clock.t
            p.poll(REPO, SHA)
            gaps.append(p.next_call_at(REPO) - before)
            self.clock.t = p.next_call_at(REPO)
        self.assertEqual(gaps, [60, 120, 300, 300, 300])

    def test_running_stays_at_the_floor_and_resets_backoff(self):
        host = FakeHost("queued", "queued", "running")
        p = self.poller(host)
        for _ in range(3):
            p.poll(REPO, SHA)
            self.clock.t = p.next_call_at(REPO)
        p.poll(REPO, SHA)
        self.assertEqual(p.next_call_at(REPO) - self.clock.t, 60)

    def test_wait_polls_no_faster_than_the_floor(self):
        host = FakeHost("running", "running", "green")
        rc, out, _ = self.wait(self.poller(host))
        self.assertEqual((rc, host.calls), (0, 3))
        self.assertGreaterEqual(self.clock.t - 1_000_000.0, 120)


class BudgetTests(Base):
    def test_403_falls_back_to_the_public_api_and_says_so(self):
        host = FakeHost(cw.RateLimited(0, None), "green")
        rc, out, err = self.wait(self.poller(host))
        self.assertEqual(rc, 0)
        self.assertEqual(host.public_calls, host.calls - 1)
        self.assertGreaterEqual(host.public_calls, 1)
        self.assertEqual(host.rate_calls, 1)       # the free rate_limit read
        self.assertIn("unauthenticated public API", out + err)

    def test_public_calls_keep_the_same_floor(self):
        host = FakeHost(cw.RateLimited(0, None), "running", "running")
        p = self.poller(host)
        p.poll(REPO, SHA)
        calls = host.calls
        self.clock.t += 30
        p.poll(REPO, SHA)
        self.assertEqual(host.calls, calls)

    def test_public_403_slows_to_10_minutes(self):
        p = self.poller(FakeHost(cw.RateLimited(0, None), cw.RateLimited(0, None)))
        p.poll(REPO, SHA)
        self.assertEqual(p.next_call_at(REPO) - self.clock.t, 600)

    def test_low_remaining_slows_to_10_minutes(self):
        host = FakeHost("running", remaining=120)
        p = self.poller(host)
        res = p.poll(REPO, SHA)
        self.assertEqual(host.rate_calls, 1)
        self.assertEqual(p.next_call_at(REPO) - self.clock.t, 600)
        self.assertTrue(any("calls left" in n for n in res["notes"]))

    def test_plenty_of_budget_keeps_the_floor(self):
        host = FakeHost("running", remaining=4900)
        p = self.poller(host)
        p.poll(REPO, SHA)
        self.assertEqual((p.next_call_at(REPO) - self.clock.t, host.rate_calls), (60, 0))


class ExitCodeTests(Base):
    def test_green_exit_0(self):
        self.assertEqual(self.wait(self.poller(FakeHost("green")))[0], 0)

    def test_failed_exit_1_prints_failing_jobs_and_tail(self):
        rc, out, _ = self.wait(self.poller(FakeHost("failed")))
        self.assertEqual(rc, 1)
        self.assertIn("ci / unit", out)
        self.assertIn("boom", out)

    def test_timeout_exit_124(self):
        rc, out, err = self.wait(self.poller(FakeHost("running")), timeout=200)
        self.assertEqual(rc, 124)
        self.assertIn("timed out", err)

    def test_json_output(self):
        rc, out, _ = self.wait(self.poller(FakeHost("green")), as_json=True)
        self.assertEqual((rc, json.loads(out)["state"]), (0, "green"))

    def test_status_reports_without_failing(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cw.run_status(self.poller(FakeHost("running")), REPO, SHA, True)
        self.assertEqual((rc, json.loads(out.getvalue())["state"]), (0, "running"))

    def test_bad_repo_and_sha_rejected(self):
        p = self.poller(FakeHost("green"))
        with self.assertRaises(cw.CiError):
            p.poll("../etc/passwd", SHA)
        with self.assertRaises(cw.CiError):
            p.poll(REPO, "../../x")

    def test_duration(self):
        self.assertEqual([cw.parse_duration(x) for x in ("90m", "30s", "2h", "45")], [5400, 30, 7200, 45])
        with self.assertRaises(cw.CiError):
            cw.parse_duration("soon")

    def test_classify(self):
        r = lambda s, c=None: {"status": s, "conclusion": c}   # noqa: E731
        self.assertEqual([cw.classify(x) for x in (
            [], [r("queued")], [r("in_progress")], [r("completed", "success"), r("queued")],
            [r("completed", "success"), r("completed", "skipped")], [r("completed", "failure"), r("in_progress")])],
            ["none", "queued", "running", "running", "green", "failed"])


class EventTests(Base):
    def test_event_short_circuits_without_polling(self):
        host = FakeHost("running")
        rc, out, _ = self.wait(self.poller(host), events=lambda: ("READY-TO-LAND", "READY-TO-LAND 7@" + SHA))
        self.assertEqual((rc, host.calls), (0, 0))
        self.assertIn("event", out)

    def test_ci_failed_event_exits_1(self):
        host = FakeHost("running")
        rc, _, _ = self.wait(self.poller(host), events=lambda: ("CI-FAILED", "CI-FAILED 7@x unit"))
        self.assertEqual((rc, host.calls), (1, 0))

    def status(self, poller, events=None):
        out = io.StringIO()
        with redirect_stdout(out):
            cw.run_status(poller, REPO, SHA, True, events)
        return json.loads(out.getvalue())

    def test_status_agrees_with_a_wait_an_event_ended(self):
        """Live report (swarm-orphans): `ci wait` ended green on a CI-GREEN event, and `ci status`
        for the same SHA still said running (the cache had the last poll)."""
        host = FakeHost("running", "running", "running")
        p = self.poller(host)
        self.assertEqual(self.status(p)["state"], "running")       # a poll cached "running"
        self.clock.t += 120
        rc, _, _ = self.wait(p, events=lambda: ("CI-GREEN", "CI-GREEN @" + SHA))
        self.assertEqual(rc, 0)
        res = self.status(p)                                     # no event lookup at all: the cache says it
        self.assertEqual((res["state"], res.get("event")), ("green", "CI-GREEN"))
        self.assertEqual(host.calls, 1)

    def test_status_reads_the_event_itself_when_nothing_waited(self):
        host = FakeHost("running")
        res = self.status(self.poller(host), events=lambda: ("CI-FAILED", "CI-FAILED @" + SHA + " unit"))
        self.assertEqual(res["state"], "failed")

    def test_confirmer_asks_the_host_once_even_inside_the_floor(self):
        """A completion event is only a trigger: the source confirms the whole commit with one call
        through the shared poller, whatever the 60 s floor; a final host answer in the cache is reused."""
        host = FakeHost("running", "green")
        p = self.poller(host)
        confirm = cw.confirmer(REPO, p)
        self.assertEqual(confirm(SHA), "running")
        self.assertEqual(confirm(SHA), "green")          # the floor has not elapsed: still asked
        self.assertEqual(host.calls, 2)
        self.assertEqual(confirm(SHA), "green")          # final and fresh in the cache: no call
        self.assertEqual(host.calls, 2)

    def test_no_event_falls_back_to_polling(self):
        host = FakeHost("green")
        self.assertEqual(self.wait(self.poller(host), events=lambda: None)[0], 0)
        self.assertEqual(host.calls, 1)

    def test_event_lookup_without_event_core_says_so(self):
        import swarm.board as sb
        had = hasattr(sb.Board, "events")
        if had:
            self.skipTest("event core is installed: the 'not installed' path cannot run")
        err = io.StringIO()
        with redirect_stderr(err):
            look = cw.event_lookup(SimpleNamespace(), SHA, "J", note=print)
        self.assertIsNone(look)
        self.assertIn("event core not installed", err.getvalue())

    def test_event_lookup_reads_board_events(self):
        import swarm.board as sb
        if not hasattr(sb.Board, "events"):
            self.skipTest("event core not installed on this branch (swarm event is absent)")
        ev = SimpleNamespace(kind="CI-FAILED", key=f"CI-FAILED:7@{SHA}", text="x")
        board = SimpleNamespace(jobs=lambda: [], events=lambda j: [ev])

        class Ctx:
            def open_board(self):
                import contextlib
                return contextlib.nullcontext(board)
        self.assertEqual(cw.event_lookup(Ctx(), SHA, "J")(), ("CI-FAILED", "x"))
        self.assertIsNone(cw.event_lookup(Ctx(), "b" * 40, "J")())


class HostTests(Base):
    def test_github_parses_runs_and_remaining_from_gh(self):
        body = json.dumps({"workflow_runs": [
            {"id": 1, "name": "test", "status": "completed", "conclusion": "success", "head_sha": SHA, "html_url": "u"},
            {"id": 2, "name": "other", "status": "queued", "conclusion": None, "head_sha": "b" * 40}]})
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="HTTP/2.0 200 OK\r\nX-Ratelimit-Remaining: 4321\r\n\r\n" + body, stderr="")
        res = cw.GitHubHost(run=run).fetch(REPO, SHA)
        self.assertEqual((res["state"], res["remaining"], len(res["runs"])), ("green", 4321, 1))
        self.assertIn(f"head_sha={SHA}", calls[0][-1])

    def test_github_403_raises_rate_limited(self):
        run = lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="gh: API rate limit exceeded (HTTP 403)")  # noqa: E731
        with self.assertRaises(cw.RateLimited):
            cw.GitHubHost(run=run).fetch(REPO, SHA)

    def test_github_public_uses_http_without_gh(self):
        seen = []

        def http(url, headers=None):
            seen.append(url)
            return {"workflow_runs": []}, {"X-RateLimit-Remaining": "59"}
        res = cw.GitHubHost(run=lambda *a, **k: self.fail("gh used"), http=http).fetch(REPO, SHA, public=True)
        self.assertEqual((res["state"], res["remaining"]), ("none", 59))
        self.assertTrue(seen[0].startswith("https://api.github.com/repos/me/proj/actions/runs?head_sha="))

    def test_gitea_commit_status(self):
        tok = self.tmp / "tok"
        tok.write_text("secret\n")
        seen = {}

        def http(url, headers=None):
            seen.update(url=url, headers=headers)
            return {"state": "failure", "statuses": [{"id": 1, "context": "ci/unit", "status": "failure", "target_url": "t"},
                                                      {"id": 2, "context": "ci/lint", "status": "success"}]}, {}
        f = cw.GiteaHost("https://git.example/api/v1/", str(tok), http=http)
        res = f.fetch(REPO, SHA)
        self.assertEqual(res["state"], "failed")
        self.assertEqual(seen["url"], f"https://git.example/api/v1/repos/{REPO}/commits/{SHA}/status")
        self.assertEqual(seen["headers"]["Authorization"], "token secret")
        self.assertEqual([x["name"] for x in f.failure_detail(REPO, SHA, res)[0]], ["ci/unit"])

    def test_make_host_never_assumes_github(self):
        with self.assertRaises(cw.CiError):
            cw.make_host("none", {})
        with self.assertRaises(cw.CiError):
            cw.make_host("gitea", {})
        self.assertIsInstance(cw.make_host("github", {}), cw.GitHubHost)


from test_team_plugin import TeamEnv  # noqa: E402


class CliTests(TeamEnv):
    """Through `swarm ci`, the ci plugin's command."""

    def test_ci_is_listed_and_needs_a_ci_host(self):
        rc, out, err = self.cli("ci", "wait", "--repo", REPO, "--sha", SHA)
        self.assertEqual(rc, 2)
        self.assertIn("no CI host selected", err)

    def test_ci_without_subcommand_is_a_usage_error(self):
        rc, _, err = self.cli("ci")
        self.assertEqual(rc, 2)
        self.assertIn("swarm ci status", err)

    def test_status_uses_the_configured_ci_host_and_cache(self):
        import os
        os.environ["SWARM_CI_CACHE"] = str(self.tmp / "cicache")
        self.team_toml.write_text('[ci]\nkind = "gitea"\n')   # no api_url: a clear config error
        rc, _, err = self.cli("ci", "status", "--repo", REPO, "--sha", SHA)
        self.assertEqual(rc, 2)
        self.assertIn("api_url", err)


if __name__ == "__main__":
    unittest.main()
