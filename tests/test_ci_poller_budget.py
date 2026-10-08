"""QA: the CI poller budget contract (skills/ci/ci_wait.py): one shared poller per repo, a 60 s floor,
and a 403 falls back to the public API. Fake host and fake clock, temp cache: no network, no gh."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from support import ROOT

_spec = importlib.util.spec_from_file_location("ci_wait_qa", ROOT / "skills/ci/ci_wait.py")
cw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cw)

REPO = "me/proj"
SHA = "a" * 40
SHA2 = "b" * 40


class Host:
    def __init__(self, forbid_authed=False):
        self.authed = self.public = 0
        self.forbid_authed = forbid_authed

    def fetch(self, repo, sha, public=False):
        if public:
            self.public += 1
        else:
            self.authed += 1
            if self.forbid_authed:
                raise cw.RateLimited(0, None)
        return cw.result("running", runs=[{"id": 1, "name": "ci", "status": "in_progress",
                                           "conclusion": None, "url": ""}], remaining=4000)

    def rate_limit(self):
        return 0, 2_000_000_000

    def failure_detail(self, *a, **k):
        return [], ""


class PollerBudget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = 1_000_000.0

    def poller(self, host):
        return cw.Poller(host, Path(self.tmp.name), now=lambda: self.t, sleep=lambda s: None)

    def test_floor_is_60_seconds(self):
        self.assertEqual(cw.FLOOR_S, 60)

    def test_two_pollers_share_the_cache_and_floor_for_one_repo(self):
        host = Host()
        a, b = self.poller(host), self.poller(host)
        a.poll(REPO, SHA)
        b.poll(REPO, SHA)
        a.poll(REPO, SHA2)           # different sha, same repo: still inside the floor
        self.assertEqual(host.authed, 1)
        self.t += 59
        b.poll(REPO, SHA)
        self.assertEqual(host.authed, 1)
        self.t += 2
        b.poll(REPO, SHA)
        self.assertEqual(host.authed, 2)

    def test_other_repo_has_its_own_floor(self):
        host = Host()
        p = self.poller(host)
        p.poll(REPO, SHA)
        p.poll("me/other", SHA)
        self.assertEqual(host.authed, 2)

    def test_403_falls_back_to_public_api_and_says_so(self):
        host = Host(forbid_authed=True)
        res = self.poller(host).poll(REPO, SHA)
        self.assertEqual(host.public, 1)
        self.assertEqual(res["state"], "running")
        self.assertTrue(any("public" in n.lower() for n in res["notes"]))

    def test_public_mode_keeps_the_floor_and_skips_authed_calls(self):
        host = Host(forbid_authed=True)
        p = self.poller(host)
        p.poll(REPO, SHA)
        authed = host.authed
        self.t += 61            # drained budget (remaining 0) also slows to 10 min
        p.poll(REPO, SHA)
        self.assertEqual(host.public, 1)
        self.t += 601
        p.poll(REPO, SHA)
        self.assertEqual(host.authed, authed)   # still in public mode
        self.assertEqual(host.public, 2)

    def test_low_budget_slows_polling_to_10_minutes(self):
        class Low(Host):
            def fetch(self, repo, sha, public=False):
                r = super().fetch(repo, sha, public)
                r["remaining"] = 100
                return r

            def rate_limit(self):
                return 100, 2_000_000_000
        host = Low()
        p = self.poller(host)
        p.poll(REPO, SHA)
        self.t += 300
        p.poll(REPO, SHA)
        self.assertEqual(host.authed, 1)
        self.t += 301
        p.poll(REPO, SHA)
        self.assertEqual(host.authed, 2)


if __name__ == "__main__":
    unittest.main()
