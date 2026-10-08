"""Event sources on the plugin mechanism, the listener (security: verify before parse, body cap, no
secrets logged), its keep-alive, and the safety-net checks."""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import http.client
import json
import threading
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

from swarm import events_listener as el, events_safety as es, plugins

SECRET = b"s3cr3t-value-never-logged"


class FakeBoard:
    """The Eng A interface (Board.post_event) as a double."""
    def __init__(self):
        self.events, self.data, self.msgs, self.jobs_, self.rosters, self.states = {}, {}, [], [], {}, {}

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def post_event(self, job, kind, key, text, to=None, source=None):
        k = (job, kind, key)
        if k in self.events:
            return self.events[k]["id"], False
        self.events[k] = {"id": len(self.events) + 1, "text": text, "to": to, "source": source}
        return self.events[k]["id"], True

    def pending_events(self, job, to=None): return []
    def job_data(self, job): return dict(self.data.get(job, {}))
    def set_job_data(self, job, k, v): self.data.setdefault(job, {})[k] = v; return True
    def post(self, job, name, text, **kw): self.msgs.append((job, name, text))
    def jobs(self, inc=False): return self.jobs_
    def roster(self, job): return self.rosters.get(job, [])
    def sync_state(self, key): return self.states.get(key)
    def messages_after(self, after, job=None): return []


def hmac_ok(headers, body):
    return hmac.compare_digest(headers.get("X-Sig", ""), hmac.new(SECRET, body, hashlib.sha256).hexdigest())


class Source:
    def __init__(self, verify=hmac_ok):
        self.parsed = []
        self.verify_calls = 0
        self._verify = verify
        self.polls = 0

    def verify(self, headers, body):
        self.verify_calls += 1
        return self._verify(headers, body)

    def handle(self, headers, body, ctx):
        self.parsed.append(body)
        p = json.loads(body)
        return [{"job": p["job"], "kind": "NEEDS-REVIEW", "key": p["key"], "text": p["text"], "to": "@pm"}]

    def poll(self, ctx):
        self.polls += 1
        return [{"job": "J", "kind": "POLLED", "key": "p1", "text": "polled " + ctx.config.get("name", "")}]


def registry(src, **kw):
    reg = plugins.Registry({}, "/nonexistent/config.toml", core_commands=("post",))
    plugins.PluginAPI(reg, "plug", reg.config_path.parent).register_event_source(
        "gitea", routes=["/gitea"], verify=src.verify, handle=src.handle, **kw)
    return reg


@contextlib.contextmanager
def running(src, cfg_events=None, board=None, logs=None, **kw):
    board = board or FakeBoard()
    cfg = {"events": {"enabled": True, "port": 1, **(cfg_events or {})}}
    lst = el.Listener(cfg, registry(src, **kw), lambda: board, log=(logs.append if logs is not None else None))
    lst.cfg["port"] = 0
    host, port = lst.bind()
    t = threading.Thread(target=lst.serve_forever, daemon=True)
    t.start()
    try:
        yield lst, board, port
    finally:
        lst.shutdown()
        t.join(5)


def post(port, path, body, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", path, body=body, headers=headers or {})
    r = c.getresponse()
    r.read()
    return r.status


def signed(body):
    return {"X-Sig": hmac.new(SECRET, body, hashlib.sha256).hexdigest()}


class RegisterTests(unittest.TestCase):
    def reg(self):
        r = plugins.Registry({}, "/nonexistent/config.toml", core_commands=())
        return r, plugins.PluginAPI(r, "plug", r.config_path.parent)

    def test_registers_a_source(self):
        r, api = self.reg()
        api.register_event_source("g", routes=["/g"], verify=lambda h, b: True, handle=lambda h, b, c: [],
                                  poll=lambda c: None, poll_interval_s=30)
        self.assertEqual(r.event_sources["g"].routes, ("/g",))
        self.assertEqual(r.event_sources["g"].poll_interval_s, 30)

    def test_rejects_bad_registrations(self):
        r, api = self.reg()
        ok = dict(routes=["/g"], verify=lambda h, b: True, handle=lambda h, b, c: [])
        api.register_event_source("g", **ok)
        for name, kw in (("g", ok), ("h", {**ok, "routes": ["/g"]}), ("h", {**ok, "routes": []}),
                         ("h", {**ok, "routes": ["nolead"]}), ("h", {**ok, "verify": None}),
                         ("h", {**ok, "routes": ["/h"], "poll": lambda c: None}),   # poll without interval
                         ("bad name", {**ok, "routes": ["/h"]})):
            with self.assertRaises((ValueError, TypeError), msg=name):
                api.register_event_source(name, **kw)

    def test_a_failing_plugin_loses_its_sources(self):
        r, api = self.reg()
        api.register_event_source("g", routes=["/g"], verify=lambda h, b: True, handle=lambda h, b, c: [])
        r._drop_blocker_hooks("plug")
        self.assertEqual(r.event_sources, {})


class ListenerTests(unittest.TestCase):
    def body(self, **kw):
        return json.dumps({"job": "J", "key": "k1", "text": "PR 1 opened", **kw}).encode()

    def test_a_signed_request_posts_an_idempotent_event(self):
        src = Source()
        with running(src) as (_, board, port):
            b = self.body()
            self.assertEqual(post(port, "/gitea", b, signed(b)), 204)
            self.assertEqual(post(port, "/gitea", b, signed(b)), 204)   # a redelivery: no second event
        (k, ev), = board.events.items()
        self.assertEqual(k, ("J", "NEEDS-REVIEW", "k1"))
        self.assertEqual((ev["to"], ev["source"]), ("@pm", "gitea"))

    def test_verify_runs_before_the_body_is_parsed(self):
        src = Source()
        with running(src) as (_, board, port):
            b = self.body()
            self.assertEqual(post(port, "/gitea", b, {"X-Sig": "0" * 64}), 403)
            self.assertEqual(post(port, "/gitea", b"{not json", {"X-Sig": "x"}), 403)   # never parsed
        self.assertEqual(src.parsed, [])
        self.assertEqual(board.events, {})

    def test_a_verify_that_raises_is_a_refusal(self):
        def boom(h, b): raise RuntimeError("no secret file")
        with running(Source(boom)) as (_, board, port):
            self.assertEqual(post(port, "/gitea", self.body(), {}), 403)

    def test_body_cap(self):
        src = Source()
        with running(src, {"max_body_bytes": 100}) as (_, board, port):
            big = b"x" * 101
            self.assertEqual(post(port, "/gitea", big, signed(big)), 413)
            self.assertEqual(src.verify_calls, 0)   # refused before it was read, let alone verified
            ok = json.dumps({"job": "J", "key": "k", "text": "t"}).encode()
            self.assertEqual(post(port, "/gitea", ok, signed(ok)), 204)

    def test_chunked_or_unsized_bodies_are_refused(self):
        with running(Source()) as (_, _, port):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.putrequest("POST", "/gitea"); c.putheader("Transfer-Encoding", "chunked"); c.endheaders()
            self.assertEqual(c.getresponse().status, 411)

    def test_routes_and_methods(self):
        with running(Source()) as (_, _, port):
            self.assertEqual(post(port, "/nope", b"{}"), 404)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/healthz"); self.assertEqual(c.getresponse().status, 200)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/gitea"); self.assertEqual(c.getresponse().status, 404)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("PUT", "/gitea", body=b"x"); self.assertEqual(c.getresponse().status, 405)

    def test_nothing_secret_is_logged(self):
        logs = []
        src = Source()
        with running(src, logs=logs) as (_, _, port):
            b = self.body(text="token=" + SECRET.decode())
            post(port, "/gitea?token=" + SECRET.decode(), b, {"X-Sig": SECRET.decode()})   # refused
            post(port, "/gitea", b"{bad", signed(b"{bad"))   # signed but unparseable: the handler fails
        text = "\n".join(logs)
        self.assertNotIn(SECRET.decode(), text)
        self.assertIn("gitea", text)
        self.assertIn("handler failed (JSONDecodeError)", text)   # the type, not the message

    def test_a_malformed_spec_is_dropped_and_the_rest_post(self):
        class S(Source):
            def handle(self, h, b, ctx):
                return [{"job": "J"}, "junk", {"job": "J", "kind": "OK", "key": "k", "text": "x" * 900}]
        logs = []
        with running(S(), logs=logs) as (_, board, port):
            self.assertEqual(post(port, "/gitea", b"{}", signed(b"{}")), 204)
        (k, ev), = board.events.items()
        self.assertEqual(len(ev["text"]), el.TEXT_MAX)
        self.assertEqual(sum("malformed" in m for m in logs), 2)

    def test_polls_run_on_their_interval_and_survive_errors(self):
        src = Source()
        with running(src, {"min_poll_interval_s": 1, "sources": {"gitea": {"name": "cfg"}}}, poll=src.poll, poll_interval_s=60) as (lst, board, _):
            self.assertEqual(lst.poll_due(1000.0), 1)
            self.assertEqual(lst.poll_due(1030.0), 0)   # not due yet
            self.assertEqual(lst.poll_due(1061.0), 1)
        self.assertEqual(src.polls, 2)
        (k, ev), = board.events.items()
        self.assertEqual((k, ev["text"], ev["source"]), (("J", "POLLED", "p1"), "polled cfg", "gitea"))
        bad = Source(); bad.poll = mock.Mock(side_effect=RuntimeError("token abc"))
        logs = []
        with running(bad, {"min_poll_interval_s": 1}, logs=logs, poll=bad.poll, poll_interval_s=5) as (lst, _, _):
            lst.poll_due(1.0)
        self.assertEqual(logs, ["gitea: poll failed (RuntimeError)"])


class PollFloorTests(unittest.TestCase):
    def test_floor_applies_unless_the_operator_lowers_it(self):
        src = Source()
        with running(src, poll=src.poll, poll_interval_s=30) as (lst, _, _):
            s = lst.registry.event_sources["gitea"]
            self.assertEqual(lst.poll_interval(s), 600)   # the plugin asked for 30: floored
            lst.cfg["sources"] = {"gitea": {"poll_interval_s": 45}}
            self.assertEqual(lst.poll_interval(s), 45)    # explicit config wins, below the floor too
            lst.cfg["sources"] = {"gitea": {"poll_interval_s": True}}
            self.assertEqual(lst.poll_interval(s), 600)
        with running(src, poll=src.poll, poll_interval_s=900) as (lst, _, _):
            self.assertEqual(lst.poll_interval(lst.registry.event_sources["gitea"]), 900)

    def test_every_poll_is_logged_at_debug(self):
        src = Source()
        with running(src, {"min_poll_interval_s": 1}, poll=src.poll, poll_interval_s=5) as (lst, _, _):
            with self.assertLogs("swarm.events", "DEBUG") as cm:
                lst.poll_due(1.0)
        self.assertIn("gitea: polling", cm.output[0])


class FakeProc:
    def __init__(self, pid): self.pid, self.rc, self.returncode = pid, None, None
    def poll(self): return self.rc
    def terminate(self): self.rc = self.returncode = -15


class HelperTests(unittest.TestCase):
    def sup(self):
        src = Source()
        reg = registry(src, helpers=lambda cfg: [{"name": "forward", "argv": ["gh", "webhook", "forward", cfg.get("repo", "r")]},
                                                 {"bad": 1}])
        lst = el.Listener({"events": {"enabled": True}}, reg, FakeBoard, log=None)
        lst.cfg["sources"] = {"gitea": {"repo": "o/n"}}
        self.t, self.logs, self.written, self.procs = [1000.0], [], [], []

        def popen(argv, **kw):
            self.procs.append((argv, FakeProc(100 + len(self.procs))))
            return self.procs[-1][1]
        hs = el.HelperSupervisor(lst, self.logs.append, popen=popen, clock=lambda: self.t[0], write=self.written.append)
        hs.load()
        return hs

    def test_started_with_config_and_a_malformed_spec_ignored(self):
        hs = self.sup()
        hs.step()
        self.assertEqual(self.procs[0][0], ["gh", "webhook", "forward", "o/n"])
        self.assertTrue(any("malformed" in m for m in self.logs))
        self.assertTrue(self.written[-1]["helpers"]["gitea/forward"]["up"])

    def test_restart_with_backoff_and_reset_after_healthy(self):
        hs = self.sup()
        hs.step()
        for want in (5, 15, 60, 300, 300):
            self.procs[-1][1].rc = self.procs[-1][1].returncode = 1
            self.t[0] += 1
            hs.step()
            self.assertFalse(self.written[-1]["helpers"]["gitea/forward"]["up"])
            n = len(self.procs)
            self.t[0] += want - 1
            hs.step()
            self.assertEqual(len(self.procs), n)   # not yet
            self.t[0] += 1
            hs.step()
            self.assertEqual(len(self.procs), n + 1, want)
        self.t[0] += el.HEALTHY_AFTER + 1   # healthy long enough: the next failure restarts at once-ish
        self.procs[-1][1].rc = self.procs[-1][1].returncode = 1
        hs.step()
        self.t[0] += 5
        hs.step()
        self.assertEqual(len(self.procs), 7)

    def test_health_makes_forwarder_down_problems(self):
        up = {"beat": NOW.timestamp(), "helpers": {"g/f": {"up": True, "restarts": 0}}}
        self.assertEqual(es.helper_problems(up, NOW), [])
        down = {"beat": NOW.timestamp(), "helpers": {"g/f": {"up": False, "restarts": 3}}}
        self.assertEqual([p.code for p in es.helper_problems(down, NOW)], ["forwarder-down"])
        stale = {"beat": NOW.timestamp() - 120, "helpers": {"g/f": {"up": True}}}
        self.assertEqual([p.code for p in es.helper_problems(stale, NOW)], ["forwarder-down"])
        self.assertEqual(es.helper_problems(None, NOW), [])
        b = FakeBoard(); b.jobs_ = [js()]; b.rosters["J"] = [agent()]
        probs = es.check(b, {"events": {"enabled": True}}, NOW, listener_up=True, health=down)
        self.assertEqual([p.code for p in probs], ["forwarder-down"])
        self.assertEqual(es.report(b, {"events": {"enabled": True}}, probs, NOW), 1)
        self.assertTrue(next(iter(b.events.values()))["text"].startswith("SWARM-ALERT forwarder-down"))


class SettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_local(self):
        s = el.settings({})
        self.assertEqual((s["enabled"], s["bind"], s["model"]), (False, "127.0.0.1", "haiku"))

    def test_bad_values(self):
        for bad in ({"enabled": "yes"}, {"port": 0}, {"port": "80"}, {"max_body_bytes": 0},
                    {"model": "x; rm -rf"}, {"sources": {"a": 1}}):
            with self.assertRaises(el.EventSettingsError, msg=bad):
                el.settings({"events": bad})


class KeepAliveTests(unittest.TestCase):
    cfg = {"events": {"enabled": True}}

    def test_off_up_started_stuck(self):
        say = mock.Mock()
        self.assertEqual(el.ensure_running({}, say), "off")
        self.assertEqual(el.ensure_running(self.cfg, say, probe_fn=lambda c: True), "up")
        start = mock.Mock(return_value=42)
        self.assertEqual(el.ensure_running(self.cfg, say, probe_fn=lambda c: False, lock_fn=lambda: False,
                                           start=start), "started")
        start.assert_called_once()
        start.reset_mock()
        self.assertEqual(el.ensure_running(self.cfg, say, probe_fn=lambda c: False, lock_fn=lambda: True,
                                           start=start), "stuck")
        start.assert_not_called()

    def test_start_detached_runs_events_serve(self):
        popen = mock.Mock(return_value=NS(pid=7))
        self.assertEqual(el.start_detached({"_config_path": "/c.toml"}, popen), 7)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[-2:], ["events", "serve"])
        self.assertIn("/c.toml", cmd)
        self.assertTrue(popen.call_args[1]["start_new_session"])

    def test_probe_sees_a_live_listener_and_a_dead_port(self):
        with running(Source()) as (lst, _, port):
            self.assertTrue(el.probe({"events": {"port": port}}))
        self.assertFalse(el.probe({"events": {"port": port}}))


NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)


def js(job="J", ago=5):
    return NS(job=job, status="active", last_activity_at=NOW - dt.timedelta(minutes=ago))


def agent(role="orchestrator", key="k1", name="Otto"):
    return NS(agent_key=key, name=name, role=role, active=True)


class SafetyTests(unittest.TestCase):
    cfg = {"events": {"enabled": False}}

    def board(self, **kw):
        b = FakeBoard()
        b.jobs_ = [js(**kw)]
        b.rosters["J"] = [agent()]
        return b

    def codes(self, b, cfg=None):
        return [p.code for p in es.check(b, cfg or self.cfg, NOW)]

    def test_a_quiet_healthy_job_has_no_problem(self):
        self.assertEqual(self.codes(self.board()), [])

    def test_stalled(self):
        self.assertEqual(self.codes(self.board(ago=61)), ["stalled"])

    def test_listener_down_only_when_enabled(self):
        b = self.board()
        self.assertEqual([p.code for p in es.check(b, {"events": {"enabled": True}}, NOW, listener_up=False)],
                         ["listener-down"])
        self.assertEqual(es.check(b, self.cfg, NOW, listener_up=False), [])

    def test_waiter_heartbeat(self):
        b = self.board()
        b.pending_events = lambda job, to=None: [NS(to=None, kind="NEEDS-REVIEW", created_at=NOW)]
        self.assertEqual(self.codes(b), ["waiter-stale"])   # pending and nobody armed
        fresh = (NOW - dt.timedelta(seconds=20)).isoformat()
        b.data["J"] = {"events.waiter.orchestrator": fresh}
        self.assertEqual(self.codes(b), [])
        b.data["J"] = {"events.waiter.pm": (NOW - dt.timedelta(seconds=300)).isoformat()}
        self.assertEqual(self.codes(b), ["waiter-stale"])
        b.pending_events = lambda job, to=None: []
        self.assertEqual(self.codes(b), ["waiter-stale"])   # a waiter that died armed

    def test_unacked_events_and_unread_messages(self):
        b = self.board()
        b.data["J"] = {"events.waiter.pm": NOW.isoformat()}
        b.pending_events = lambda job, to=None: [NS(to="@pm", kind="CI-FAILED", created_at=NOW - dt.timedelta(minutes=31))]
        self.assertEqual(self.codes(b), ["unacked"])
        b.pending_events = lambda job, to=None: [NS(to="@pm", kind="X", created_at=NOW - dt.timedelta(minutes=5))]
        self.assertEqual(self.codes(b), [])
        b.states["k1"] = NS(last_read_id=3)
        old = NS(id=4, to_agent="Otto", agent_name="Bart", created_at=NOW - dt.timedelta(minutes=45))
        b.messages_after = lambda after, job=None: [old] if after == 3 else []
        self.assertEqual(self.codes(b), ["unread"])

    def test_report_posts_one_alert_per_job_and_pass_deduped_per_window(self):
        b = self.board(ago=90)
        probs = es.check(b, self.cfg, NOW)
        self.assertEqual(es.report(b, self.cfg, probs, NOW), 1)
        self.assertEqual(es.report(b, self.cfg, probs, NOW + dt.timedelta(minutes=10)), 0)
        self.assertEqual(es.report(b, self.cfg, probs, NOW + dt.timedelta(minutes=70)), 1)   # hourly repeat
        ev = list(b.events.items())[0]
        self.assertEqual(ev[0][1], "SWARM-ALERT")
        self.assertEqual(ev[1]["source"], "safety")
        self.assertIsNone(ev[1]["to"])
        self.assertLessEqual(len(ev[1]["text"]), el.TEXT_MAX)

    def test_a_board_without_events_gets_one_notice(self):
        b = self.board(ago=90)
        b.post_event = None   # a board from before events
        probs = es.check(b, self.cfg, NOW)
        self.assertEqual(es.report(b, self.cfg, probs, NOW), 1)
        self.assertEqual(es.report(b, self.cfg, probs, NOW), 0)
        self.assertEqual(len(b.msgs), 1)

    def test_the_model_is_configured_never_inherited(self):
        b = self.board()
        self.assertEqual(es.model_for(b, {}, "J"), "haiku")
        self.assertEqual(es.model_for(b, {"events": {"model": "claude-haiku-9"}}, "J"), "claude-haiku-9")
        b.data["J"] = {"events.model": "other"}
        self.assertEqual(es.model_for(b, {}, "J"), "other")
        b.data["J"] = {"events.model": "bad; model"}
        self.assertEqual(es.model_for(b, {}, "J"), "haiku")
        with mock.patch.dict("os.environ", {"ANTHROPIC_MODEL": "opus", "CLAUDE_MODEL": "opus"}):
            self.assertNotIn("opus", es.triage_command(es.model_for(b, {}, "J"), []))

    def test_triage_is_off_by_default_and_uses_the_configured_model(self):
        b = self.board(ago=90)
        probs = es.check(b, self.cfg, NOW)
        run = mock.Mock(return_value=NS(returncode=0, stdout="orchestrator is stuck, ping it\n"))
        self.assertIsNone(es.triage(b, self.cfg, "J", probs, run=run))
        run.assert_not_called()
        cfg = {"events": {"triage": True, "model": "haiku"}}
        self.assertEqual(es.triage(b, cfg, "J", probs, run=run), "orchestrator is stuck, ping it")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        failing = mock.Mock(return_value=NS(returncode=1, stdout=""))
        self.assertIsNone(es.triage(b, cfg, "J", probs, run=failing))


class CliTests(Env):
    def test_events_serve_refuses_when_off_and_check_runs(self):
        rc, _, err = self.cli("events", "serve")
        self.assertEqual(rc, 1)
        self.assertIn("enabled = false", err)
        self.assertEqual(self.cli("events", "check")[0], 0)

    def test_events_listed_in_plugins_and_in_the_help(self):
        rc, out, _ = self.cli("events")
        self.assertEqual(rc, 2)


class RealBoardTests(Env):
    """The listener and the alerts on the real backend (SWARM_TEST_BACKEND: memory, sqlite, file, postgres)."""

    def test_a_webhook_and_a_poll_land_as_idempotent_events(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        src = Source()
        with running(src, {"min_poll_interval_s": 1}, board=None, poll=src.poll, poll_interval_s=60) as (lst, _, port):
            lst.board_factory = self.board
            b = json.dumps({"job": "J", "key": "k1", "text": "PR 1"}).encode()
            self.assertEqual(post(port, "/gitea", b, signed(b)), 204)
            self.assertEqual(post(port, "/gitea", b, signed(b)), 204)
            lst.poll_due(1.0)
        with self.board() as board:
            evs = board.pending_events("J")
            self.assertEqual(sorted((e.kind, e.key, e.source) for e in evs),
                             [("NEEDS-REVIEW", "k1", "gitea"), ("POLLED", "p1", "gitea")])
            self.assertEqual({e.to for e in evs}, {"@pm", None})

    def test_checks_and_alert_on_a_real_board(self):
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.peer("J", "w1", role="engineer")
        with self.board() as board:
            board.post_event("J", "NEEDS-REVIEW", "1@a", "PR 1", to=None, source="t")
            codes = [p.code for p in es.check(board, {}, None)]
            self.assertEqual(codes, ["waiter-stale"])   # pending, nobody waits
            board.set_job_data("J", "events.waiter.orchestrator", dt.datetime.now(dt.timezone.utc).isoformat())
            self.assertEqual(es.check(board, {}, None), [])
            late = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=45)
            self.assertEqual([p.code for p in es.check(board, {}, late)], ["unacked", "waiter-stale"])
            probs = es.check(board, {}, late)
            self.assertEqual(es.report(board, {}, probs, late), 1)
            alerts = [e for e in board.pending_events("J") if e.kind == "SWARM-ALERT"]
            self.assertEqual(len(alerts), 1)
            self.assertTrue(alerts[0].text.startswith("SWARM-ALERT"))
        self.assertEqual(self.cli("events", "check")[0], 0)   # the heartbeat is fresh now
        with self.board() as board:
            board.set_job_data("J", "events.waiter.orchestrator", None)
        self.assertEqual(self.cli("events", "check")[0], 1)


from test_supervise_command import SuperviseEnv  # noqa: E402


class SupervisePassTests(SuperviseEnv):
    """The supervisor pass keeps the listener up and runs the safety nets, only when [events] is on."""

    def test_pass_ensures_the_listener_and_checks_when_enabled(self):
        self.cfg["events"] = {**el.DEFAULTS, "enabled": True}
        with mock.patch.object(el, "ensure_running", return_value="started") as ens, \
                mock.patch.object(es, "run_check", return_value=[]) as chk:
            self.supervise()
        ens.assert_called_once()
        chk.assert_called_once()

    def test_pass_leaves_events_alone_by_default_and_survives_a_failure(self):
        with mock.patch.object(el, "ensure_running") as ens:
            self.supervise()
        ens.assert_not_called()
        self.cfg["events"] = {**el.DEFAULTS, "enabled": True}
        with mock.patch.object(el, "ensure_running", side_effect=RuntimeError("boom")):
            self.assertEqual(self.supervise(), 0)


if __name__ == "__main__":
    unittest.main()
