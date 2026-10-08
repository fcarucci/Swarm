"""CI adapters of the ci plugin: GitHub webhooks (callbacks first), Gitea, BRANCH-READY."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "skills" / "ci"


def load(name):
    spec = importlib.util.spec_from_file_location("t_" + name, ROOT / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["t_" + name] = mod
    spec.loader.exec_module(mod)
    return mod


fe = load("ci_events")
SHA = "a" * 40
OLD = "b" * 40


class Fake:
    """A CI host with canned PRs, comments and CI, standing in for the HTTP API."""
    def __init__(self, prs, comments=None, ci=None, parse=None):
        self.prs, self._comments, self._ci, self._parse = prs, comments or {}, ci or {}, parse

    def open_prs(self):
        return self.prs

    def comments(self, n):
        return self._comments.get(n, [])

    def ci(self, sha):
        return self._ci.get(sha, ("pending", ""))

    def parse(self, headers, payload):
        return self._parse


def kinds(specs):
    return [s["key"] for s in specs]


class DecideTests(unittest.TestCase):
    def test_new_head_without_verdict_needs_review(self):
        out = fe.decide("J", 7, SHA, "Title", [], ("pending", ""))
        self.assertEqual(kinds(out), [f"NEEDS-REVIEW:7@{SHA}"])
        self.assertEqual(out[0]["to"], "@pm")
        self.assertEqual(out[0]["job"], "J")

    def test_ready_only_when_verdict_and_ci_agree_on_the_exact_head(self):
        ok = f"REVIEW #7 @ {SHA}: VERIFIED\nlooks fine"
        self.assertEqual(kinds(fe.decide("J", 7, SHA, "t", [ok], ("success", ""))), [f"READY-TO-LAND:7@{SHA}"])
        self.assertEqual(fe.decide("J", 7, SHA, "t", [ok], ("pending", "")), [])
        stale = f"REVIEW #7 @ {OLD}: VERIFIED"
        self.assertEqual(kinds(fe.decide("J", 7, SHA, "t", [stale], ("success", ""))), [f"NEEDS-REVIEW:7@{SHA}"])

    def test_later_changes_required_beats_an_earlier_verified(self):
        comments = [f"REVIEW #7 @ {SHA}: VERIFIED", f"REVIEW #7 @ {SHA}: CHANGES REQUIRED: fix x"]
        out = fe.decide("J", 7, SHA, "t", comments, ("success", ""))
        self.assertEqual(kinds(out), [f"REVIEW-CHANGES:7@{SHA}"])
        self.assertIn("fix x", out[0]["text"])

    def test_ci_failure_lists_contexts(self):
        out = fe.decide("J", 7, SHA, "t", [], ("failure", "ci/test:failure"), new_head=False)
        self.assertEqual(kinds(out), [f"CI-FAILED:7@{SHA}"])
        self.assertIn("ci/test:failure", out[0]["text"])

    def test_a_verdict_for_another_pr_is_ignored(self):
        self.assertEqual(fe.verdict_for([f"REVIEW #8 @ {SHA}: VERIFIED"], 7, SHA), (None, ""))

    def test_text_capped_at_500(self):
        self.assertLessEqual(len(fe.spec("J", "CI-FAILED", 1, SHA, "x" * 900)["text"]), 500)


class EvaluateTests(unittest.TestCase):
    def test_current_head_only(self):
        fake = Fake([(7, SHA, "T")], {7: [f"REVIEW #7 @ {SHA}: VERIFIED"]}, {SHA: ("success", "")})
        self.assertEqual(kinds(fe.evaluate(fake, "J", 7)), [f"READY-TO-LAND:7@{SHA}"])
        self.assertEqual(fe.evaluate(fake, "J", 7, OLD), [])        # CI result of a superseded head
        self.assertEqual(fe.evaluate(fake, "J", 9), [])             # closed or unknown PR

    def test_poll_covers_every_open_pr(self):
        fake = Fake([(1, SHA, "a"), (2, OLD, "b")], {}, {})
        self.assertEqual(sorted(kinds(fe.poll_prs(fake, "J"))), sorted([f"NEEDS-REVIEW:1@{SHA}", f"NEEDS-REVIEW:2@{OLD}"]))

    def test_status_webhook_finds_the_pr_by_head(self):
        fake = Fake([(7, SHA, "T")], {}, {SHA: ("failure", "ci:error")}, parse=[(None, SHA, "", False)])
        out = fe.handle_webhook(fake, "J", {}, b"{}")
        self.assertEqual(kinds(out), [f"CI-FAILED:7@{SHA}"])


class GiteaTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.secret = Path(self.dir.name, "secret")
        self.secret.write_text("s3cret-value\n")
        self.gitea = fe.Gitea({"repo": "o/r", "api_url": "http://x/api/v1", "secret_file": str(self.secret)})

    def sign(self, body):
        return hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()

    def test_hmac_verify(self):
        body = b'{"a":1}'
        self.assertTrue(self.gitea.verify({"X-Gitea-Signature": self.sign(body)}, body))
        self.assertFalse(self.gitea.verify({"X-Gitea-Signature": "0" * 64}, body))
        self.assertFalse(self.gitea.verify({}, body))

    def test_no_secret_file_never_verifies(self):
        self.assertFalse(fe.Gitea({"repo": "o/r"}).verify({"X-Gitea-Signature": "x"}, b""))

    def test_pull_request_opened_yields_head(self):
        payload = {"action": "opened", "pull_request": {"number": 5, "head": {"sha": SHA}, "title": "T"}}
        self.assertEqual(self.gitea.parse({"X-Gitea-Event": "pull_request"}, payload), [(5, SHA, "T", True)])
        payload["action"] = "closed"
        self.assertEqual(self.gitea.parse({"X-Gitea-Event": "pull_request"}, payload), [])

    def test_review_comment_triggers_a_recheck_of_that_pr(self):
        payload = {"action": "created", "is_pull": True, "issue": {"number": 5},
                   "comment": {"body": f"REVIEW #5 @ {SHA}: VERIFIED"}}
        self.assertEqual(self.gitea.parse({"X-Gitea-Event": "issue_comment"}, payload), [(5, None, "", False)])
        payload["comment"]["body"] = "chatter"
        self.assertEqual(self.gitea.parse({"X-Gitea-Event": "issue_comment"}, payload), [])

    def test_ci_from_commit_status(self):
        calls = {"/repos/o/r/commits/%s/status" % SHA: {"state": "success", "statuses": [{"context": "ci", "status": "success"}]}}
        self.gitea.get = lambda path: calls[path]
        self.assertEqual(self.gitea.ci(SHA), ("success", ""))
        calls["/repos/o/r/commits/%s/status" % SHA] = {"state": "failure", "statuses": [
            {"context": "ci/a", "status": "failure"}, {"context": "ci/b", "status": "success"}]}
        self.assertEqual(self.gitea.ci(SHA), ("failure", "ci/a:failure"))
        calls["/repos/o/r/commits/%s/status" % SHA] = {"state": "success", "statuses": []}
        self.assertEqual(self.gitea.ci(SHA)[0], "pending")        # no statuses yet is not green


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        secret = Path(self.dir.name, "secret")
        secret.write_text("gh-secret\n")
        self.gh = fe.GitHub({"repo": "o/r", "secret_file": str(secret)})

    def test_hmac_sha256_header(self):
        body = b"{}"
        sig = "sha256=" + hmac.new(b"gh-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(self.gh.verify({"X-Hub-Signature-256": sig}, body))
        self.assertFalse(self.gh.verify({"X-Hub-Signature-256": "sha256=" + "0" * 64}, body))

    def test_default_api(self):
        self.assertEqual(self.gh.api, "https://api.github.com")

    def test_events(self):
        pr = {"action": "synchronize", "pull_request": {"number": 3, "head": {"sha": SHA}, "title": "T"}}
        self.assertEqual(self.gh.parse({"X-GitHub-Event": "pull_request"}, pr), [(3, SHA, "T", True)])
        rv = {"action": "submitted", "pull_request": {"number": 3}, "review": {"body": f"REVIEW #3 @ {SHA}: VERIFIED"}}
        self.assertEqual(self.gh.parse({"X-GitHub-Event": "pull_request_review"}, rv), [(3, None, "", False)])
        cs = {"action": "completed", "check_suite": {"head_sha": SHA, "pull_requests": [{"number": 3}]}}
        self.assertEqual(self.gh.parse({"X-GitHub-Event": "check_suite"}, cs), [(3, SHA, "", False)])
        st = {"state": "failure", "sha": SHA}
        self.assertEqual(self.gh.parse({"X-GitHub-Event": "status"}, st), [(None, SHA, "", False)])

    def test_ci_combines_check_runs_and_statuses(self):
        data = {}
        self.gh.get = lambda path: data["runs"] if "check-runs" in path else data["status"]
        data["runs"] = {"check_runs": [{"name": "t", "status": "completed", "conclusion": "success"}]}
        data["status"] = {"statuses": []}
        self.assertEqual(self.gh.ci(SHA), ("success", ""))
        data["runs"] = {"check_runs": [{"name": "t", "status": "in_progress", "conclusion": None}]}
        self.assertEqual(self.gh.ci(SHA)[0], "pending")
        data["runs"] = {"check_runs": [{"name": "t", "status": "completed", "conclusion": "failure"}]}
        self.assertEqual(self.gh.ci(SHA), ("failure", "t:failure"))
        data["runs"] = {"check_runs": []}
        self.assertEqual(self.gh.ci(SHA)[0], "pending")


class PushTests(unittest.TestCase):
    """GitHub callbacks: events from pushed payloads and remembered state, never an API call."""
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state = fe.PushState(Path(self.dir.name, "state.json"))

    def push(self, event, payload):
        return fe.handle_push(self.state, "J", {"X-GitHub-Event": event}, json.dumps(payload).encode())

    def opened(self, sha=SHA):
        return self.push("pull_request", {"action": "opened", "pull_request": {"number": 3, "head": {"sha": sha}, "title": "T"}})

    def verdict(self, text):
        return self.push("pull_request_review", {"action": "submitted", "pull_request": {"number": 3}, "review": {"body": text}})

    def wfrun(self, concl, sha=SHA, name="Test", prs=({"number": 3},)):
        """PR-keyed events only; wfrun_all keeps the commit-level CI-GREEN too."""
        return [e for e in self.wfrun_all(concl, sha, name, prs) if e["kind"] != "CI-GREEN"]

    def wfrun_all(self, concl, sha=SHA, name="Test", prs=({"number": 3},)):
        return self.push("workflow_run", {"action": "completed", "workflow_run": {
            "head_sha": sha, "conclusion": concl, "name": name, "pull_request": None, "pull_requests": list(prs)}})

    def test_opened_needs_review(self):
        self.assertEqual(kinds(self.opened()), [f"NEEDS-REVIEW:3@{SHA}"])

    def test_workflow_run_failure_is_ci_failed_without_api(self):
        self.opened()
        self.assertEqual(kinds(self.wfrun("failure")), [f"CI-FAILED:3@{SHA}"])

    def test_workflow_run_alone_yields_ci_events_before_any_pr_event(self):
        # no pull_request webhook seen yet: the run's own pull_requests list is enough
        self.assertEqual(kinds(self.wfrun("failure")), [f"CI-FAILED:3@{SHA}"])
        self.assertEqual(kinds(self.wfrun("failure", sha=OLD, prs=())), [f"CI-FAILED:@{OLD}"])
        self.assertEqual(kinds(self.wfrun_all("success", sha="c" * 40, prs=())), [f"CI-GREEN:@{'c' * 40}"])

    def test_verdict_then_green_run_is_ready_to_land_either_order(self):
        self.opened()
        self.assertEqual(self.verdict(f"REVIEW #3 @ {SHA}: VERIFIED fine"), [])
        self.assertEqual(kinds(self.wfrun("success")), [f"READY-TO-LAND:3@{SHA}"])
        other = fe.PushState()
        h = {"X-GitHub-Event": "workflow_run"}
        fe.handle_push(other, "J", {"X-GitHub-Event": "pull_request"}, json.dumps(
            {"action": "opened", "pull_request": {"number": 3, "head": {"sha": SHA}, "title": "T"}}).encode())
        fe.handle_push(other, "J", h, json.dumps({"action": "completed", "workflow_run": {
            "head_sha": SHA, "conclusion": "success", "name": "Test", "pull_requests": [{"number": 3}]}}).encode())
        out = fe.handle_push(other, "J", {"X-GitHub-Event": "pull_request_review"}, json.dumps(
            {"action": "submitted", "pull_request": {"number": 3}, "review": {"body": f"REVIEW #3 @ {SHA}: VERIFIED"}}).encode())
        self.assertEqual([k for k in kinds(out) if not k.startswith("CI-GREEN")], [f"READY-TO-LAND:3@{SHA}"])

    def test_a_green_run_on_an_old_head_is_not_ready(self):
        self.opened(OLD)
        self.verdict(f"REVIEW #3 @ {OLD}: VERIFIED")
        self.opened(SHA)                                    # new head supersedes
        self.assertEqual(kinds(self.wfrun("success", OLD)), [])
        self.assertEqual(kinds(self.wfrun("success", SHA)), [])   # verdict names the old head

    def test_check_suite_without_pr_list_maps_by_head(self):
        self.opened()
        out = self.push("check_suite", {"action": "completed", "check_suite": {"head_sha": SHA, "conclusion": "failure", "pull_requests": []}})
        self.assertEqual(kinds(out), [f"CI-FAILED:3@{SHA}"])

    def test_state_survives_a_restart(self):
        self.opened()
        self.verdict(f"REVIEW #3 @ {SHA}: VERIFIED")
        again = fe.PushState(self.state.path)
        out = fe.handle_push(again, "J", {"X-GitHub-Event": "workflow_run"}, json.dumps({"action": "completed", "workflow_run": {
            "head_sha": SHA, "conclusion": "success", "name": "Test", "pull_requests": [{"number": 3}]}}).encode())
        self.assertEqual([k for k in kinds(out) if not k.startswith("CI-GREEN")], [f"READY-TO-LAND:3@{SHA}"])
        self.assertNotIn("secret", self.state.path.read_text())

    def test_bad_json_and_unknown_events_are_ignored(self):
        self.assertEqual(fe.handle_push(self.state, "J", {"X-GitHub-Event": "pull_request"}, b"not json"), [])
        self.assertEqual(self.push("ping", {"zen": "x"}), [])

    def test_forward_argv_reads_the_secret_from_its_file_and_poll_has_a_floor(self):
        secret = Path(self.dir.name, "sec")
        secret.write_text("hmac-value\n")
        argv = fe.forward_argv({"repo": "o/r", "secret_file": str(secret)}, 8923)
        self.assertEqual(argv[:3], ["gh", "webhook", "forward"])
        self.assertIn("--repo=o/r", argv)
        self.assertIn("--url=http://127.0.0.1:8923/github", argv)
        self.assertEqual(argv[-2:], ["--secret", "hmac-value"])
        self.assertTrue(any(a.startswith("--events=workflow_run,check_suite,pull_request,pull_request_review") for a in argv))
        self.assertEqual(fe.poll_interval({}), 600)
        self.assertEqual(fe.poll_interval({"poll_interval_s": 5}), 600)
        self.assertEqual(fe.poll_interval({"poll_interval_s": 1200}), 1200)

    def test_registration_polls_no_faster_than_ten_minutes_and_github_has_a_forwarder(self):
        sp = load("swarm_plugin")
        seen = {}

        class Api:
            config_dir = Path(self.dir.name)

            def register_event_source(self, name, **kw):
                seen[name] = kw
        (Path(self.dir.name) / "config.toml").write_text(
            '[events]\nport = 9000\n[ci.github]\nrepo = "o/r"\nforward = true\n')
        sp.register_event_sources(Api())
        self.assertGreaterEqual(seen["github"]["poll_interval_s"], 600)
        self.assertGreaterEqual(seen["gitea"]["poll_interval_s"], 600)
        helpers = seen["github"]["helpers"]({"forward": True})
        self.assertEqual(helpers[0]["name"], "forward")
        self.assertIn("--url=http://127.0.0.1:9000/github", helpers[0]["argv"])
        (Path(self.dir.name) / "config.toml").write_text('[ci.github]\nrepo = "o/r"\n')   # forward off
        self.assertEqual(seen["github"]["helpers"]({}), [])


class BranchReadyTests(unittest.TestCase):
    def test_board_posts_become_events(self):
        out = fe.branch_ready_specs("J", ["hello", f"BRANCH READY feat/x {SHA} body /tmp/b.md", "BRANCH READY feat/y@abc1234"])
        self.assertEqual(kinds(out), [f"BRANCH-READY:feat/x@{SHA}", "BRANCH-READY:feat/y@abc1234"])
        self.assertTrue(all(e["to"] == "@pm" and e["job"] == "J" for e in out))


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def test_registers_sources_through_core_interface_and_not_without_it(self):
        sp = load("swarm_plugin")
        sources = {}

        class Api:
            config_dir = Path(tempfile.gettempdir())

            def register_event_source(self, name, **kw):
                sources[name] = kw
        sp.register_event_sources(Api())
        self.assertEqual(set(sources), {"gitea", "github", "branch-ready"})
        self.assertEqual(sources["gitea"]["routes"], ["/gitea"])
        self.assertFalse(sources["gitea"]["verify"]({}, b""))      # no secret configured: reject
        sp.register_event_sources(object())                          # a core without the interface

    def test_sources_follow_ci_sections_and_a_workflow_run_needs_no_api_call(self):
        """[ci.github] activates the source; a signed workflow_run yields CI events, urlopen never called."""
        import unittest.mock
        sp = load("swarm_plugin")
        seen = {}

        class Api:
            config_dir = Path(self.dir.name)

            def register_event_source(self, name, **kw):
                seen[name] = kw
        secret = Path(self.dir.name, "sec")
        secret.write_text("s3cret\n")
        cfg = Path(self.dir.name, "config.toml")
        cfg.write_text("")
        sp.register_event_sources(Api())
        github = seen["github"]
        body = json.dumps({"action": "completed", "workflow_run": {
            "head_sha": SHA, "conclusion": "failure", "name": "Test", "pull_requests": [{"number": 3}]}}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        headers = {"X-GitHub-Event": "workflow_run", "X-Hub-Signature-256": sig}
        self.assertFalse(github["verify"](headers, body))          # no [ci.github]: inactive
        self.assertEqual(github["helpers"]({}), [])
        cfg.write_text(f'[ci.github]\nrepo = "o/r"\nsecret_file = {json.dumps(str(secret))}\n')
        self.assertTrue(github["verify"](headers, body))
        self.assertFalse(github["verify"](dict(headers, **{"X-Hub-Signature-256": "sha256=00"}), body))
        ctx = type("Ctx", (), {"board": type("B", (), {"jobs": lambda self: [type("J", (), {"job": "J", "status": "active"})()]})()})()
        with unittest.mock.patch("urllib.request.urlopen", side_effect=AssertionError("API call")):
            out = github["handle"](headers, body, ctx)
            green = github["handle"](headers, body.replace(b"failure", b"success"), ctx)
        self.assertEqual(kinds(out), [f"CI-FAILED:3@{SHA}"])
        self.assertEqual(kinds(green), [f"CI-GREEN:@{SHA}"])
        self.assertTrue(all(e["job"] == "J" and e["to"] == "@pm" for e in out + green))


if __name__ == "__main__":
    unittest.main()
