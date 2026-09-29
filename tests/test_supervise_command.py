"""`swarm supervise`: kill switches, outage, candidates, caps posted once, launch, dry run."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from test_transcript_cli import TranscriptEnv

from swarm import cli as swarm
from swarm.board.autoinit import store_key
from swarm.supervisor import command, outage, runner, settings as st


def claude_transcript(cwd: str) -> str:
    return json.dumps({"type": "user", "cwd": cwd, "timestamp": "2026-09-27T10:00:00Z",
                       "message": {"role": "user", "content": "[swarm job: J]\nDo the thing."}}) + "\n"


def enrol(cfg, key, *, job="J", harness="claude", cwd, session_id=None):
    """The host-private enrolment record the unsandboxed hook writes at SubagentStart: the
    only thing the supervisor takes ownership, work dir and harness from."""
    from swarm import enrolment
    from swarm.board.autoinit import store_key
    return enrolment.write(store_key(cfg), job=job, agent_key=key, harness=harness,
                           session_id=session_id, cwd=str(cwd))


class SuperviseEnv(TranscriptEnv):
    """The pass fixture (no tests of its own)."""
    SUP = 'enabled = true\nbackoff_minutes = [0]\n'

    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + f"\n[supervise]\n{self.SUP}")
        self.cfg = swarm.load_config(self.config)
        # the test work dirs live in the temp dir, not under ~/src (the default allowlist)
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.tmp)]
        self.cli("activate", "--job", "J", "--session", "sess-1")
        self.hook("start", agent_id="orig", session="sess-1")
        self.name = self.agent("orig").name
        self.work = self.tmp / "work"
        self.work.mkdir()
        # the test work dirs live in the temp dir, not under ~/src (the default allowlist)
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.tmp)]
        self.seed("J", "orig", self.name, claude_transcript(str(self.work)))
        self.h.update_agent("orig", harness="claude")
        enrol(self.cfg, "orig", cwd=self.work, session_id="sess-1")
        self.h.backdate_agent("orig", last_seen=31 * 60)
        self.started = []
        self.out = []

    def start_runner(self, cfg, run):
        self.started.append(run)
        return 4242

    def supervise(self, **kw):
        kw.setdefault("start_runner", self.start_runner)
        kw.setdefault("which", lambda b: f"/usr/bin/{b}")
        kw.setdefault("say", self.out.append)
        kw.setdefault("scope_available", lambda: True)
        return command.run_pass(self.cfg, **kw)

    def posts(self):
        with self.board() as b:
            return [m.message for m in b.recent_messages(50, job="J")]

    def restarts(self, job="J"):
        with self.board() as b:
            return b.restarts(job=job)


class SuperviseTests(SuperviseEnv):
    def test_closes_then_restarts_with_brief_and_marker(self):
        self.assertEqual(self.supervise(), 0)
        self.assertEqual(self.agent("orig").left_reason, "stuck:dead")
        [run] = self.started
        self.assertEqual((run["job"], run["name"], run["harness"], run["cwd"]), ("J", self.name, "claude", str(self.work)))
        self.assertIn("You are resuming", run["stdin"])
        self.assertIn("Do the thing.", run["stdin"])
        self.assertEqual(run["argv"][:2], ["claude", "-p"])
        self.assertIn("auto", run["argv"])                       # permission mode auto
        self.assertTrue(Path(run["marker"]).exists())
        self.assertEqual(run["config"], str(Path(os.environ.get("SWARM_CONFIG") or "~/.config/swarm/config.toml").expanduser()))
        [r] = self.restarts()
        self.assertEqual((r.attempt, r.old_agent_key, r.new_agent_key, r.outcome), (1, "orig", run["session_id"], None))
        self.assertTrue(any(p.startswith(f"restarted {self.name} (attempt 1/2): stuck:dead") for p in self.posts()))
        self.assertIn("last_run_at", st.load_state())

    def test_second_pass_does_not_relaunch(self):
        self.supervise()
        self.supervise()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.restarts()), 1)

    def test_kill_switches(self):
        with mock.patch.dict(self.cfg, {"supervise": {"enabled": False}}):
            self.assertEqual(self.supervise(), 0)
        st.off_file().parent.mkdir(parents=True, exist_ok=True)
        st.off_file().write_text("")
        self.assertEqual(self.supervise(), 0)
        st.off_file().unlink()
        with self.board() as b:
            b.set_job_supervise("J", False)
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertIsNone(self.agent("orig").ended_at)

    def test_no_supervise_job_closed_elsewhere_is_never_a_candidate(self):
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            b.set_job_supervise("J", False)
            self.assertEqual(command.candidates(b, self.cfg, st.settings(self.cfg)), [])
        self.supervise()
        self.assertEqual((self.started, self.restarts()), ([], []))

    def test_board_unreachable_notes_outage_and_does_nothing(self):
        import datetime as dt
        # the outage began 25 minutes ago (earlier passes noted it); the agent went quiet 31 min ago
        outage.note_unreachable(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=25))
        self.h.set_available(False)
        self.assertEqual(self.supervise(), 0)
        self.assertIn("board unreachable", " ".join(self.out))
        self.assertIsNotNone(outage.current())
        self.h.set_available(True)
        self.supervise()
        self.assertEqual(self.started, [])          # the agent's silence falls in the outage window: grace
        self.assertIsNotNone(outage.current().recovered)

    def test_cap_posted_once(self):
        self.cfg["supervise"]["max_restarts_per_agent"] = 0
        self.supervise()
        self.supervise()
        caps = [p for p in self.posts() if p.startswith(f"not restarting {self.name}")]
        self.assertEqual(len(caps), 1, caps)
        with self.board() as b:
            self.assertIsNone(b.job_status("J").waiting_on)   # the job may auto-close now

    def test_cap_post_memory_forgotten_when_the_job_closes(self):
        self.cfg["supervise"]["max_restarts_per_agent"] = 0
        self.supervise()
        self.assertEqual(len(st.load_state()["posted"]), 1)
        with self.board() as b:
            b.close_job("J", "cancelled", None)
        self.supervise()
        self.assertEqual(st.load_state()["posted"], {})

    def test_missing_workdir_refused(self):
        import shutil
        shutil.rmtree(self.work)
        (self.markers / "J.json").write_text(json.dumps({"job": "J", "session_id": "sess-1"}))  # no cwd
        self.supervise()
        self.assertEqual(self.started, [])
        [r] = self.restarts()
        self.assertEqual(r.outcome, "refused")
        self.assertTrue(any(p.startswith(f"can't restart {self.name}") and "work directory" in p for p in self.posts()))

    def test_unapproved_project_config_waits_with_one_post_until_approved(self):
        """The pass doesn't launch into a work dir holding unapproved project
        configuration; it posts once naming the file and records nothing, so an approval lets
        the next pass go ahead."""
        (self.work / ".mcp.json").write_text('{"mcpServers": {}}')
        self.supervise()
        self.supervise()
        self.assertEqual((self.started, self.restarts()), ([], []))
        named = [p for p in self.posts() if ".mcp.json" in p and p.startswith(f"not restarting {self.name}")]
        self.assertEqual(len(named), 1, self.posts())
        self.assertIn("supervise approve", named[0])
        command.save_approvals(command.approval_candidates(self.cfg, str(self.work)))
        self.supervise()
        [run] = self.started
        self.assertEqual(run["cwd"], str(self.work))

    def test_the_marker_cwd_is_never_a_workdir(self):
        """An orchestrator marker is sandbox-writable: its cwd is no fallback."""
        other = self.tmp / "other"
        other.mkdir()
        self.seed("J", "orig", self.name, json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n")
        m = json.loads((self.markers / "J.json").read_text())
        (self.markers / "J.json").write_text(json.dumps({**m, "cwd": str(other)}))
        self.supervise()
        self.assertEqual(self.started[0]["cwd"], str(self.work))

    def test_workdir_comes_from_the_enrolment_record(self):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        rec = enrolment.find(store_key(self.cfg), "orig")
        self.assertEqual(command.workdir_for(self.cfg, rec), str(self.work))
        self.assertIsNone(command.workdir_for(self.cfg, None))
        self.assertIsNone(command.workdir_for(self.cfg, enrol(self.cfg, "x", cwd="/")))
        self.assertIsNone(command.workdir_for(self.cfg, enrol(self.cfg, "y", cwd=self.tmp / "gone")))

    def test_missing_binary_refused(self):
        self.supervise(which=lambda b: None)
        self.assertEqual(self.restarts()[0].outcome, "refused")
        self.assertTrue(any("claude not found" in p for p in self.posts()))
        self.assertIn("permanent", st.log_path().read_text())     # refused is final: logged clearly

    def test_other_users_agents_are_not_restarted(self):
        self.supervise(start_runner=lambda c, r: 1)       # closes and restarts ours
        self.hook("start", agent_id="theirs", session="sess-1")
        self.h.update_agent("theirs", os_user="someone-else")
        from swarm import enrolment                       # another user's agent: not enrolled here
        enrolment.remove(store_key(self.cfg), "theirs")
        with self.board() as b:
            b.close_agent("theirs", "stuck:dead")
        before = len(self.restarts())
        self.supervise()
        self.assertEqual(len(self.restarts()), before)

    def test_dry_run_changes_nothing_and_shows_caps(self):
        self.assertEqual(self.supervise(dry_run=True), 0)
        text = "\n".join(self.out)
        self.assertIn(f"would close {self.name} on J: stuck:dead", text)
        self.assertIn("caps", text)
        self.assertIn("daily", text)
        self.assertIsNone(self.agent("orig").ended_at)
        self.assertEqual((self.started, self.restarts()), ([], []))
        self.assertNotIn("last_run_at", st.load_state())

    def test_dry_run_shows_would_restart_for_a_closed_agent(self):
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
        self.supervise(dry_run=True)
        self.assertIn(f"would restart {self.name} on J (attempt 1/2", "\n".join(self.out))
        self.assertEqual((self.started, self.restarts()), ([], []))

    def test_the_pass_prunes_old_records_of_closed_jobs(self):
        # the supervise pass removes enrolment records older than
        # max([transcripts] retention_days, 7) days whose job is closed (or gone); records of
        # active jobs, recent ones and other boards' stay
        import time as _time
        from swarm import enrolment
        self.cfg["transcripts"]["retention_days"] = 1          # -> 7 days
        day = 86400.0
        now = _time.time()
        key = store_key(self.cfg)
        self.cli("job", "K", "--description", "d")
        with self.board() as b:
            b.close_job("K", "completed", "done")
        write = lambda k, job, age, board=key: enrolment.write(   # noqa: E731
            board, job=job, agent_key=k, harness="claude", session_id=None, cwd=str(self.work), now=now - age)
        write("closed-old", "K", 8 * day)
        write("closed-new", "K", 6 * day)
        write("gone-old", "NO-SUCH-JOB", 8 * day)
        write("active-old", "J", 8 * day)
        write("other-board-old", "K", 8 * day, board="postgres:elsewhere")
        enrolment.write_job(key, job="K", harness="claude", session_id=None, cwd=str(self.work), now=now - 8 * day)
        self.supervise()
        self.assertIsNone(enrolment.find(key, "closed-old"))
        self.assertIsNone(enrolment.find(key, "gone-old"))
        self.assertIsNone(enrolment.find_job(key, "K"))
        self.assertIsNotNone(enrolment.find(key, "closed-new"))
        self.assertIsNotNone(enrolment.find(key, "active-old"))
        self.assertIsNotNone(enrolment.find("postgres:elsewhere", "other-board-old"))
        self.assertIsNotNone(enrolment.find(key, "orig"))

    def test_a_dry_run_prunes_nothing(self):
        import time as _time
        from swarm import enrolment
        key = store_key(self.cfg)
        enrolment.write(key, job="NO-SUCH-JOB", agent_key="gone-old", harness="claude", session_id=None,
                        cwd=str(self.work), now=_time.time() - 400 * 86400)
        self.supervise(dry_run=True)
        self.assertIsNotNone(enrolment.find(key, "gone-old"))

    def test_dry_run_shows_the_hold_for_unapproved_project_config(self):
        # --dry-run runs the project-config check, so it never says "would restart"
        # for an agent the real pass will hold for approval
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
        (self.work / ".mcp.json").write_text('{"mcpServers": {}}')
        self.supervise(dry_run=True)
        text = "\n".join(self.out)
        self.assertIn(f"would hold {self.name} on J for approval", text)
        self.assertIn(".mcp.json", text)
        self.assertNotIn(f"would restart {self.name}", text)
        self.assertEqual((self.started, self.restarts()), ([], []))
        self.assertFalse([p for p in self.posts() if ".mcp.json" in p])   # dry run: nothing posted
        command.save_approvals(command.approval_candidates(self.cfg, str(self.work)))
        self.out.clear()
        self.supervise(dry_run=True)
        self.assertIn(f"would restart {self.name} on J (attempt 1/2", "\n".join(self.out))

    def test_launch_failure_after_record_is_finished_failed(self):
        def boom(cfg, run):
            raise OSError("fork failed")
        self.supervise(start_runner=boom)
        [r] = self.restarts()
        self.assertEqual(r.outcome, "failed")
        self.assertFalse(any(self.markers.glob("*--resume-r*.json")))
        self.assertTrue(any(p.startswith(f"restart of {self.name} failed to start") for p in self.posts()))

    def test_job_with_verdict_is_not_restarted(self):
        self.h.update_job("J", verdict="not_met")
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertEqual(self.agent("orig").left_reason, "stuck:dead")   # still closed: closing ignores verdicts

    def test_cli_entry(self):
        rc, out, err = self.cli("supervise", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("would close", out)

    def test_invalid_settings_rc_1(self):
        self.cfg["supervise"]["max_turns"] = "lots"
        self.assertEqual(self.supervise(), 1)

    # ---- no user systemd manager
    def _second_stuck_agent_on_other_job(self):
        self.cli("activate", "--job", "K", "--session", "sess-2")
        self.hook("start", agent_id="other", session="sess-2")
        self.h.update_agent("other", harness="claude")
        enrol(self.cfg, "other", job="K", cwd=self.work)
        self.h.backdate_agent("other", last_seen=31 * 60)

    def test_no_user_manager_records_nothing_and_posts_once_per_host(self):
        self._second_stuck_agent_on_other_job()
        self.supervise(scope_available=lambda: False)
        self.supervise(scope_available=lambda: False)
        self.assertEqual(self.started, [])
        self.assertEqual((self.restarts("J"), self.restarts("K")), ([], []))
        with self.board() as b:
            self.assertIsNone(b.job_status("J").waiting_on)
        with self.board() as b:
            for j in ("J", "K"):   # once on every affected job (per host and job), not every pass
                notes = [m.message for m in b.recent_messages(50, job=j)
                         if m.message.startswith("not restarting: no user systemd manager")]
                self.assertEqual(notes, ["not restarting: no user systemd manager "
                                         "(fix: loginctl enable-linger $USER)"], j)
        self.assertIn("no user systemd manager", st.log_path().read_text())

    def test_no_user_manager_then_manager_back_restarts(self):
        self.supervise(scope_available=lambda: False)
        self.supervise()
        self.assertEqual(len(self.started), 1)

    def test_no_user_manager_dry_run_says_so_per_candidate(self):
        self._second_stuck_agent_on_other_job()
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            b.close_agent("other", "stuck:dead")
        self.supervise(dry_run=True, scope_available=lambda: False)
        lines = [l for l in self.out if "no user systemd manager" in l]
        self.assertEqual(len(lines), 2, self.out)
        self.assertTrue(all(l.startswith("not restarting ") for l in lines))
        self.assertEqual(self.posts(), [p for p in self.posts() if "systemd" not in p])

    # ---- the final transcript before the brief
    def test_final_transcript_captured_before_the_brief(self):
        order = []
        from swarm.supervisor import brief, lost
        real = brief.build_brief
        with self.board() as b:
            b.set_job_supervise("J", False)          # orig is not this test's agent
        self.cli("activate", "--job", "K", "--session", "sess-2")
        self.hook("start", agent_id="fresh", session="sess-2")
        self.h.update_agent("fresh", harness="claude")
        enrol(self.cfg, "fresh", job="K", cwd=self.work)
        name = self.agent("fresh", job="K").name
        self.seed("K", "fresh", name, claude_transcript(str(self.work)), final=False)   # a snapshot only
        with self.board() as b:
            b.close_agent("fresh", "stuck:dead")     # closed earlier: this pass's sweep doesn't capture
        with mock.patch.object(lost, "capture_final", side_effect=lambda *a, **k: order.append("capture") or True), \
                mock.patch.object(brief, "build_brief", side_effect=lambda *a, **k: order.append("brief") or real(*a, **k)):
            self.supervise()
        self.assertEqual(order, ["capture", "brief"])
        self.assertEqual(len(self.started), 1)

    def test_final_transcript_already_stored_is_not_captured_again(self):
        from swarm.supervisor import lost
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
        with mock.patch.object(lost, "capture_final") as cap:
            self.supervise()
        cap.assert_not_called()
        self.assertEqual(len(self.started), 1)

    def test_capture_failure_never_blocks_and_brief_says_not_stored(self):
        from swarm.supervisor import lost
        # an agent whose transcript was never stored, and whose capture fails
        self.hook("start", agent_id="bare", session="sess-1")
        self.h.update_agent("bare", harness="claude")
        enrol(self.cfg, "bare", cwd=self.work)
        m = json.loads((self.markers / "J.json").read_text())
        (self.markers / "J.json").write_text(json.dumps({**m, "cwd": str(self.work)}))
        with self.board() as b:
            b.close_agent("bare", "stuck:dead")
        with mock.patch.object(lost, "capture_final", side_effect=RuntimeError("boom")):
            self.supervise()
        runs = {r["resume_of"]: r for r in self.started}
        self.assertIn("bare", runs)
        self.assertIn("No stored transcript", runs["bare"]["stdin"])
        self.assertIn("not captured before restart", st.log_path().read_text())

    # ---- helpers
    def _tree(self):
        """Every file under the test's temp dir (home, state, markers, board storage): content and mtime."""
        import hashlib
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.name.endswith("-shm"):   # SQLite's WAL index: every reader updates it (not data)
                continue
            if p.is_file() and not p.is_symlink():
                st_ = p.stat()
                # a write-only lock file (0200) can't be read: its size stands for its content
                data = p.read_bytes() if st_.st_mode & 0o400 else str(st_.st_size).encode()
                out[str(p)] = (hashlib.sha256(data).hexdigest(), st_.st_mtime_ns, st_.st_mode)
            elif p.is_dir():
                out[str(p)] = "dir"
        return out

    def _board_dump(self):
        with self.board() as b:
            return (repr(b.jobs(True)), repr(b.agents("J")), repr(b.restarts()),
                    [(m.id, m.message) for m in b.recent_messages(200, job="J")])

    def test_dry_run_makes_no_writes(self):
        import datetime as dt
        # a candidate (closed earlier), a stuck agent to "close", a run file without its lock file,
        # an outage that is over: every read path of a pass
        self.hook("start", agent_id="gone", session="sess-1")
        self.h.update_agent("gone", harness="claude")
        enrol(self.cfg, "gone", cwd=self.work)
        with self.board() as b:
            b.close_agent("gone", "stuck:dead")
        self.hook("start", agent_id="stuck-now", session="sess-1")    # its sweep closed orig meanwhile
        self.h.backdate_agent("stuck-now", last_seen=31 * 60)
        outage.note_unreachable(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3))
        outage.note_reachable(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
        st.ensure_private_dir(self.cfg)
        runner.run_dir(self.cfg).mkdir(mode=0o700)
        (runner.run_dir(self.cfg) / "r99.json").write_text(json.dumps({"restart_id": 99, "job": "J"}))
        st.save_state({"posted": {}})
        before, board_before = self._tree(), self._board_dump()
        with mock.patch.object(swarm, "auto_init", side_effect=AssertionError("auto-init in dry run")), \
                mock.patch.object(runner, "scope_available", return_value=True):
            rc, out, err = self.cli("supervise", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("would close", out)
        self.assertIn("would restart", out)
        self.assertEqual(self._board_dump(), board_before)
        after = self._tree()
        self.assertEqual({k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                          if before.get(k) != after.get(k)}, {})
        self.assertFalse((runner.run_dir(self.cfg) / "r99.lock").exists())

    def test_dry_run_on_a_missing_store_creates_nothing(self):
        import copy
        for backend, section, key, where in (("file", "file", "path", self.tmp / "no-file-board"),
                                             ("sqlite", "sqlite", "path", self.tmp / "no-db" / "board.sqlite3")):
            with self.subTest(backend=backend):
                cfg = copy.deepcopy(self.cfg)
                cfg["board"]["backend"] = backend
                cfg.setdefault(section, {})[key] = str(where)
                before = self._tree()
                out = []
                self.assertEqual(command.run_pass(cfg, dry_run=True, say=out.append,
                                                  scope_available=lambda: True), 0)
                self.assertEqual(out, ["board not initialised: nothing to supervise"])
                self.assertEqual(self._tree(), before)
                self.assertFalse(where.exists())

    # ---- the file backend reads without writing
    def _file_store(self, name):
        """An initialised file board with a job, a closed agent (a candidate) and a message."""
        import copy
        from support import SMALL_POOL
        from swarm.board import open_board, setup_board
        cfg = copy.deepcopy(self.cfg)
        cfg["board"]["backend"] = "file"
        cfg["file"] = {"path": str(self.tmp / name)}
        setup_board(cfg, SMALL_POOL)
        with open_board(cfg) as b:
            b.open_job("FJ", None, None, None, None)
            who = b.allocate_name("fk", "FJ")
            b.post("FJ", who, "hello", agent_key="fk")
            b.close_agent("fk", "stuck:dead")
        return cfg, self.tmp / name

    def _dry_run_file_store(self, cfg):
        out = []
        self.assertEqual(command.run_pass(cfg, dry_run=True, say=out.append,
                                          scope_available=lambda: True), 0)
        return out

    def _changed(self, before, after):
        return {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
                if before.get(k) != after.get(k)}

    def test_dry_run_on_a_file_store_without_state_json_writes_nothing(self):
        cfg, d = self._file_store("fb-nostate")
        (d / "state.json").unlink()
        before = self._tree()
        out = self._dry_run_file_store(cfg)
        self.assertFalse((d / "state.json").exists(), out)
        self.assertEqual(self._changed(before, self._tree()), {})

    def test_dry_run_on_a_file_store_with_a_torn_message_line_leaves_it(self):
        cfg, d = self._file_store("fb-torn")
        with open(d / "messages.jsonl", "ab") as fh:
            fh.write(b'{"id": 999, "job": "FJ", "mess')      # a crashed append
        raw = (d / "messages.jsonl").read_bytes()
        before = self._tree()
        self._dry_run_file_store(cfg)
        self.assertEqual((d / "messages.jsonl").read_bytes(), raw)
        self.assertEqual(self._changed(before, self._tree()), {})

    def test_dry_run_opens_the_board_read_only(self):
        seen = []
        real = command._open
        with mock.patch.object(command, "_open", side_effect=lambda c, d: seen.append(d) or real(c, d)):
            self.supervise(dry_run=True)
        self.assertEqual(seen, [True])
        with command._open(self.cfg, True) as b:
            if self.cfg["board"]["backend"] in ("file", "sqlite"):
                self.assertTrue(b.read_only)

    def test_dry_run_on_a_missing_postgres_database(self):
        from swarm.board import BoardUnavailable
        missing = BoardUnavailable('connection failed: FATAL:  database "nope" does not exist')
        cfg = dict(self.cfg, board={**self.cfg["board"], "backend": "postgres"})
        out = []
        with mock.patch.object(command, "_open", side_effect=missing):
            self.assertEqual(command.run_pass(cfg, dry_run=True, say=out.append), 0)
        self.assertEqual(out, ["board not initialised: nothing to supervise"])
        out.clear()
        with mock.patch.object(command, "_open", side_effect=BoardUnavailable("timeout")):
            command.run_pass(cfg, dry_run=True, say=out.append)
        self.assertEqual(out, ["board unreachable: nothing restarted (dry run: outage not noted)"])

    def test_dry_run_through_pooler_hiding_a_missing_database(self):
        from swarm.board import BoardUnavailable
        hidden = BoardUnavailable("connection failed: FATAL:  unable to get session context")
        cfg = dict(self.cfg, board={**self.cfg["board"], "backend": "postgres"})
        out = []
        with mock.patch.object(command, "_open", side_effect=hidden):
            self.assertEqual(command.run_pass(cfg, dry_run=True, say=out.append), 0)
        self.assertEqual(out, ["board unreachable or not initialised (a connection pooler hides which): nothing to supervise"])
        self.assertIsNone(outage.current())

    def test_real_pass_through_pooler_error_is_an_outage(self):
        from swarm.board import BoardUnavailable
        hidden = BoardUnavailable("connection failed: FATAL:  unable to get session context")
        out = []
        with mock.patch.object(command, "_open", side_effect=hidden):
            self.assertEqual(self.supervise(say=out.append), 0)
        self.assertEqual(out, ["board unreachable: nothing restarted (outage noted)"])
        self.assertIsNotNone(outage.current())

    def test_dry_run_unreachable_says_outage_not_noted(self):
        self.h.set_available(False)
        out = []
        self.assertEqual(self.supervise(dry_run=True, say=out.append), 0)
        self.assertEqual(out, ["board unreachable: nothing restarted (dry run: outage not noted)"])
        self.assertIsNone(outage.current())

    def test_another_pass_running_exits_quietly(self):
        import fcntl
        path = command.lock_path()
        st.ensure_private_dir(self.cfg)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.supervise(), 0)
        self.assertIn("another supervise pass is running", " ".join(self.out))
        self.assertEqual(self.started, [])
        self.assertIsNone(self.agent("orig").ended_at)
        self.assertEqual(self.supervise(dry_run=True), 0)       # a dry run doesn't need the lock
        self.assertIn("would close", " ".join(self.out))

    def _another_stuck_agent(self, key="second"):
        self.hook("start", agent_id=key, session="sess-1")
        self.h.update_agent(key, harness="claude")
        enrol(self.cfg, key, cwd=self.work)
        self.h.backdate_agent(key, last_seen=31 * 60)
        m = json.loads((self.markers / "J.json").read_text())
        (self.markers / "J.json").write_text(json.dumps({**m, "cwd": str(self.work)}))
        return self.agent(key).name

    def test_job_cap_two_agents_one_restart(self):
        self.cfg["supervise"]["max_restarts_per_job"] = 1
        self._another_stuck_agent()
        self.supervise()
        self.supervise()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.restarts()), 1)

    def test_job_cap_enforced_by_the_board_even_with_a_stale_decision(self):
        from swarm.supervisor import budget
        self.cfg["supervise"]["max_restarts_per_job"] = 1
        self._another_stuck_agent()
        go = budget.Decision(True, "ok", attempt=1, minutes=30.0)
        with mock.patch.object(budget, "decide", return_value=go):   # every decision says go
            self.supervise()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(len(self.restarts()), 1)
        self.assertIn("a cap was reached meanwhile", st.log_path().read_text())

    def test_decision_recomputed_with_this_pass_launches(self):
        self.cfg["supervise"]["max_concurrent_replacements"] = 1
        self._another_stuck_agent()
        self.supervise()
        self.assertEqual(len(self.started), 1)

    def test_daily_minutes_reserved_by_this_pass_launch(self):
        self.cfg["supervise"].update(daily_restart_minutes=60, max_minutes=60)
        self._another_stuck_agent()
        self.supervise()
        self.assertEqual(len(self.started), 1)          # the first launch holds all 60 minutes
        self.assertTrue(any("today's restart minutes" in p for p in self.posts()), self.posts())

    # ---- per-host caps across OS users, from the board's restart rows

    def _other_users_row(self, job="K", cap=60.0, old="theirs"):
        with self.board() as b, mock.patch("getpass.getuser", return_value="codex-user"):
            return b.record_restart(job, old, old, "stuck:dead", "codex", cap)

    def test_other_os_users_running_replacements_count_toward_the_host_cap(self):
        self._other_users_row("K1", old="t1")
        self._other_users_row("K2", old="t2")
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertIn(f"not restarting {self.name} on J (for now): 2/2 replacements already running",
                      st.log_path().read_text())
        self.out.clear()
        self.supervise(dry_run=True)
        self.assertIn("running 2/2", "\n".join(self.out))

    def test_an_open_row_whose_runner_died_still_counts(self):
        """A session that outlived its runner (its row still open) holds its slot."""
        from swarm.board.autoinit import store_key
        self.cfg["supervise"]["max_concurrent_replacements"] = 1
        with self.board() as b:
            r = b.record_restart("K", "mine", "mine", "stuck:dead", "claude", 60.0)
        st.ensure_private_dir(self.cfg)
        runner.run_dir(self.cfg).mkdir(mode=0o700)
        runner.run_file(r.id, self.cfg).write_text(json.dumps({"board": store_key(self.cfg)}))   # no live runner
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertIn("1/1 replacements already running", st.log_path().read_text())

    def test_daily_cap_counts_a_run_across_midnight(self):
        """A replacement started before local midnight and still running holds its cap today."""
        import datetime as dt
        self.cfg["supervise"].update(daily_restart_minutes=100, max_minutes=60)
        self._other_users_row(cap=60.0)
        with self.board() as b:
            midnight = b.now() + dt.timedelta(minutes=1)      # the row began "yesterday"
        with mock.patch.object(st, "today_start", return_value=midnight):
            self.supervise()
        [run] = self.started
        self.assertEqual(run["limit_seconds"], 40 * 60)

    def test_job_minutes_are_checked_atomically_with_the_insert(self):
        """Another host's timer reserves job minutes between this pass's
        decision and its insert; the insert must refuse."""
        self.cfg["supervise"].update(max_restart_minutes=100, max_minutes=60)
        real, calls = command._invalid, []

        def other_host_meanwhile(board, job, key, cfg=None):
            calls.append(key)
            if len(calls) == 2:                                   # right before record_restart
                with mock.patch("getpass.getuser", return_value="codex-user"):
                    board.record_restart("J", "x", "other-old", "stuck:dead", "codex", 60.0)
            return real(board, job, key, cfg)
        with mock.patch.object(command, "_invalid", side_effect=other_host_meanwhile):
            self.supervise()
        self.assertEqual(self.started, [])
        self.assertEqual([r.old_agent_key for r in self.restarts()], ["other-old"])

    def test_concurrency_cap_is_logged_not_posted_and_shown_in_dry_run(self):
        self.cfg["supervise"]["max_concurrent_replacements"] = 0
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertIn(f"not restarting {self.name} on J (for now): 0/0 replacements already running",
                      st.log_path().read_text())
        self.assertFalse(any(p.startswith(f"not restarting {self.name}") for p in self.posts()))
        self.out.clear()
        self.supervise(dry_run=True)
        self.assertIn(f"not restarting {self.name} on J: 0/0 replacements already running", "\n".join(self.out))

    def test_job_closed_between_selection_and_action_is_skipped(self):
        from swarm.supervisor import brief
        real = brief.build_brief

        def close_then_build(b, *a, **k):
            with self.board() as other:
                other.close_job("J", "cancelled", None)
            return real(b, *a, **k)
        with mock.patch.object(brief, "build_brief", side_effect=close_then_build):
            self.supervise()
        self.assertEqual((self.started, self.restarts()), ([], []))
        self.assertIn(f"not restarting {self.name} on J: the job is closed", st.log_path().read_text())

    def test_verdict_or_supervise_off_or_name_back_between_selection_and_action(self):
        from swarm.supervisor import brief
        real = brief.build_brief
        for change, why in ((lambda b: self.h.update_job("J", verdict="met"), "the job has a verdict"),
                            (lambda b: b.set_job_supervise("J", False), "supervise is off for the job")):
            with self.subTest(why=why):
                def meddle(b, *a, **k):
                    with self.board() as other:
                        change(other)
                    return real(b, *a, **k)
                with mock.patch.object(brief, "build_brief", side_effect=meddle):
                    self.supervise()
                self.assertEqual((self.started, self.restarts()), ([], []))
                self.assertIn(why, st.log_path().read_text())
                self.h.update_job("J", verdict=None)
                with self.board() as b:
                    b.set_job_supervise("J", True)

    def test_name_active_again_between_selection_and_action(self):
        from swarm.supervisor import brief
        real = brief.build_brief

        def revive(b, *a, **k):
            with self.board() as other:
                other.claim_resume("someone-new", "orig", "J")
            return real(b, *a, **k)
        with mock.patch.object(brief, "build_brief", side_effect=revive):
            self.supervise()
        self.assertEqual((self.started, self.restarts()), ([], []))
        self.assertIn("its name is active again", st.log_path().read_text())

    def test_marker_removed_even_when_finish_restart_raises(self):
        from swarm.board import backend_class
        def boom(cfg, run):
            raise OSError("fork failed")
        with mock.patch.object(backend_class(self.cfg), "finish_restart", side_effect=RuntimeError("db")):
            self.assertEqual(self.supervise(start_runner=boom), 0)
        self.assertFalse(any(self.markers.glob("*--resume-r*.json")))
        self.supervise(start_runner=boom)                     # next pass finishes the row
        self.assertEqual(self.restarts()[0].outcome, "failed")

    def test_marker_removal_failure_is_retried_next_pass(self):
        from swarm.supervisor import markers
        def boom(cfg, run):
            raise OSError("fork failed")
        with mock.patch.object(markers, "remove_resume_marker", return_value=False):
            self.supervise(start_runner=boom)
        self.assertEqual(len(list(self.markers.glob("*--resume-r*.json"))), 1)
        self.assertIn("retried next pass", st.log_path().read_text())
        self.supervise()
        self.assertFalse(any(self.markers.glob("*--resume-r*.json")))
        self.assertEqual(self.started, [])

    def test_leftover_marker_of_unstarted_restart_is_cleaned_and_row_failed(self):
        from swarm.supervisor import markers
        with self.board() as b:
            r = b.record_restart("J", "x", "x", "stuck:dead", "claude", 5.0)
        p = markers.write_resume_marker(self.cfg, "J", r.id, resume_of="x", name="X", harness="claude")
        self.supervise()
        self.assertFalse(p.exists())
        self.assertEqual(next(x for x in self.restarts() if x.id == r.id).outcome, "failed")

    def test_a_fifo_resume_marker_blocks_no_pass(self):
        """A sandboxed agent plants a FIFO named like a leftover resume marker."""
        from test_supervise_markers import _within
        self.markers.mkdir(parents=True, exist_ok=True)
        fifo = self.markers / "J--resume-r77.json"
        os.mkfifo(fifo)
        self.fifos = [fifo]
        with self.board() as b:
            self.assertEqual(_within(self, lambda: command.clean_resume_markers(b, self.cfg), 5), 1)
        self.assertFalse(os.path.lexists(fifo))
        os.mkfifo(fifo)
        self.assertEqual(_within(self, self.supervise, 10), 0)

    def test_marker_of_a_live_run_is_left_alone(self):
        from swarm.supervisor import markers
        with self.board() as b:
            r = b.record_restart("J", "x", "x", "stuck:dead", "claude", 5.0)
        p = markers.write_resume_marker(self.cfg, "J", r.id, resume_of="x", name="X", harness="claude")
        runner.write_run({"restart_id": r.id, "job": "J", "name": "X", "marker": str(p), "board": store_key(self.cfg)})
        fd = runner.own(r.id, self.cfg)
        self.addCleanup(os.close, fd)
        self.supervise()
        self.assertTrue(p.exists())
        self.assertIsNone(next(x for x in self.restarts() if x.id == r.id).outcome)

    def test_partial_snapshot_labelled_in_the_brief(self):
        from swarm.supervisor import lost
        self.hook("start", agent_id="snap", session="sess-1")
        self.h.update_agent("snap", harness="claude")
        enrol(self.cfg, "snap", cwd=self.work)
        name = self.agent("snap").name
        self.seed("J", "snap", name, claude_transcript(str(self.work)), final=False)
        with self.board() as b:
            b.close_agent("snap", "stuck:dead")
        with mock.patch.object(lost, "capture_final", return_value=False):
            self.supervise()
        run = next(r for r in self.started if r["resume_of"] == "snap")
        self.assertIn("transcript tail from a non-final snapshot; may be incomplete", run["stdin"])
        self.assertIn("Do the thing.", run["stdin"])

    def test_restarted_is_posted_before_the_runner_starts(self):
        """A replacement that ends at once must not have its end posted before its start."""
        from swarm.supervisor.stuck import SUPERVISOR_NAME

        def ends_at_once(cfg, run):
            self.started.append(run)
            with self.board() as b:
                b.post("J", SUPERVISOR_NAME, f"{run['name']} (restart 1) ended: failed")
            return 1
        self.supervise(start_runner=ends_at_once)
        with self.board() as b:
            mine = [m.message for m in b.messages_after(0, job="J") if m.agent_name == SUPERVISOR_NAME]
        i = next(i for i, m in enumerate(mine) if m.startswith(f"restarted {self.name}"))
        j = next(i for i, m in enumerate(mine) if "ended: failed" in m)
        self.assertLess(i, j, mine)

    def test_a_launch_that_fails_is_posted_after_restarted(self):
        def boom(cfg, run):
            raise RuntimeError("no runner")
        self.supervise(start_runner=boom)
        posts = self.posts()
        self.assertTrue(any(p.startswith(f"restart of {self.name} failed to start") for p in posts), posts)

    def test_two_restarts_brief_two_carries_attempt_ones_work(self):
        """End to end: orig -> replacement 1 (works, then hangs) -> replacement 2, whose brief
        has the original task, replacement 1's work, and the right attempt numbers."""
        self.supervise()
        [run1] = self.started
        sid1 = run1["session_id"]
        with self.board() as b:
            self.assertEqual(b.claim_resume(sid1, "orig", "J"), self.name)
            b.post("J", self.name, "REP1: pushed the parser branch")
        lines = [{"type": "user", "cwd": str(self.work), "timestamp": "2026-09-27T11:00:00Z",
                  "message": {"role": "user", "content": run1["stdin"]}}]
        lines += [{"type": "assistant", "timestamp": "2026-09-27T11:01:00Z",
                   "message": {"role": "assistant", "content": f"REP1 step {i}"}} for i in range(60)]
        self.seed("J", sid1, self.name, "\n".join(json.dumps(x) for x in lines) + "\n")
        self.h.update_agent(sid1, harness="claude")
        enrol(self.cfg, sid1, cwd=self.work)
        with self.board() as b:
            b.close_agent(sid1, "stuck:silent")          # what the sweep does when it hangs
            b.finish_restart(b.restarts(job="J")[0].id, "stuck")
        self.supervise()
        self.assertEqual(len(self.started), 2)
        brief2 = self.started[1]["stdin"]
        self.assertIn("Do the thing.", brief2)                     # the original task
        self.assertIn("REP1 step 59", brief2)                      # attempt 1's work
        self.assertIn("REP1: pushed the parser branch", brief2)
        self.assertIn("restart 2 of at most 2", brief2)
        self.assertNotIn("restart 1 of at most 2", brief2)
        self.assertEqual(brief2.count("You are resuming"), 1)
        [r1, r2] = self.restarts()
        self.assertEqual((r1.attempt, r2.attempt, r2.old_agent_key, r2.agent_key), (1, 2, sid1, "orig"))
        self.assertTrue(any(p.startswith(f"restarted {self.name} (attempt 2/2): stuck:silent") for p in self.posts()))

    def test_another_boards_run_file_does_not_hold_this_boards_row(self):
        """Restart ids collide across boards; a run file of board B is not this row's run."""
        with self.board() as b:
            r = b.record_restart("K", "mine", "mine", "stuck:dead", "claude", 60.0)
        st.ensure_private_dir(self.cfg)
        runner.run_dir(self.cfg).mkdir(mode=0o700)
        runner.run_file(r.id, self.cfg).write_text(json.dumps({"restart_id": r.id, "board": "sqlite-another-board"}))
        self.supervise()
        with self.board() as b:
            self.assertEqual(next(x for x in b.restarts(job="K") if x.id == r.id).outcome, "failed")
        self.assertTrue(runner.run_file(r.id, self.cfg).exists())          # board B's file is B's business

    # ---- the supervisor's wait flag never outlives the restart it waits for

    def _flagged(self):
        from swarm.supervisor.stuck import WAITING_PREFIX
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            b.set_waiting("J", WAITING_PREFIX + self.name)

    def waiting(self):
        with self.board() as b:
            return b.job_status("J").waiting_on

    def test_wait_flag_cleared_when_the_job_gets_a_verdict(self):
        self._flagged()
        self.h.update_job("J", verdict="met")
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertIsNone(self.waiting())

    def test_wait_flag_cleared_when_the_name_is_taken_again(self):
        self._flagged()
        self.hook("start", agent_id="newcomer", session="sess-1")
        self.h.update_agent("newcomer", name=self.name)
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertIsNone(self.waiting())

    def test_wait_flag_cleared_when_supervise_is_off_for_the_job(self):
        self._flagged()
        with self.board() as b:
            b.set_job_supervise("J", False)
        self.supervise()
        self.assertIsNone(self.waiting())

    def test_someone_elses_wait_flag_is_left_alone(self):
        with self.board() as b:
            b.set_waiting("J", "the user's review")
        self.h.update_job("J", verdict="met")
        self.supervise()
        self.assertEqual(self.waiting(), "the user's review")

    # ---- kill switches rechecked right before the launch

    def test_off_file_created_during_the_pass_stops_the_launch(self):
        from swarm.supervisor import brief
        real = brief.build_brief

        def switch_off_then_build(*a, **k):     # a slow capture/brief: the user switches off meanwhile
            st.off_file().parent.mkdir(parents=True, exist_ok=True)
            st.off_file().touch()
            return real(*a, **k)
        with mock.patch.object(brief, "build_brief", side_effect=switch_off_then_build):
            self.supervise()
        self.assertEqual(self.started, [])
        self.assertEqual(self.restarts(), [])
        self.assertIn("switched off", st.log_path().read_text())

    def test_switched_off_after_the_row_is_recorded_cancels_it(self):
        from swarm.supervisor import markers
        real = markers.write_resume_marker

        def write_then_switch_off(*a, **k):
            p = real(*a, **k)
            (st.private_dir() / "supervise.off").touch()
            return p
        with mock.patch.object(markers, "write_resume_marker", side_effect=write_then_switch_off):
            self.supervise()
        self.assertEqual(self.started, [])
        [r] = self.restarts()
        self.assertEqual(r.outcome, "cancelled")
        self.assertFalse(any(self.markers.glob("*--resume-r*.json")))

    def test_job_supervise_turned_off_after_the_row_is_recorded_cancels_it(self):
        from swarm.supervisor import markers
        real = markers.write_resume_marker

        def write_then_no_supervise(*a, **k):
            p = real(*a, **k)
            with self.board() as b:
                b.set_job_supervise("J", False)
            return p
        with mock.patch.object(markers, "write_resume_marker", side_effect=write_then_no_supervise):
            self.supervise()
        self.assertEqual(self.started, [])
        self.assertEqual([r.outcome for r in self.restarts()], ["cancelled"])

    def test_codex_replacement_whose_workdir_holds_the_private_dir_is_refused(self):
        """A Codex replacement's work directory is sandbox-writable; one that contains the
        supervisor's private directory (e.g. $HOME, from a sandbox-writable orchestrator marker)
        would expose it."""
        from swarm import paths
        self.h.update_agent("orig", harness="codex")
        enrol(self.cfg, "orig", harness="codex", cwd=self.work)
        with mock.patch.object(command, "workdir_for", return_value=str(paths.home())):
            self.supervise()
        self.assertEqual(self.started, [])
        self.assertTrue(any(p.startswith(f"can't restart {self.name}") and "allowed_workdirs" in p
                            for p in self.posts()))
        self.h.update_agent("orig", harness="claude")

    def test_workdirs_are_allowed_only_under_allowed_workdirs(self):
        """The work directory comes from board or marker data an
        agent can forge, and a replacement may write it: an allowlist ([supervise]
        allowed_workdirs, default ~/src), no dot-directory below the root, a directory of this
        user, never home or above."""
        from swarm import paths
        from swarm.supervisor.settings import DEFAULTS
        self.assertEqual(DEFAULTS["allowed_workdirs"], ["~/src"])
        home = paths.home()
        cfg = {**self.cfg, "supervise": {"enabled": True}}          # the default allowlist
        (home / "src" / "proj").mkdir(parents=True)
        (home / "src" / "x" / ".git").mkdir(parents=True)
        (home / ".aws").mkdir()
        (home / ".ssh").mkdir()
        (home / "src" / "link").symlink_to(home / ".ssh")
        self.assertIsNone(command.workdir_problem(cfg, str(home / "src" / "proj")))
        for d in (home / ".aws", home / "src" / ".." / ".ssh", home / "src" / "link", home / "src" / "x" / ".git",
                  home, home.parent, self.work, home / "src" / "missing"):
            with self.subTest(d=d):
                why = command.workdir_problem(cfg, str(d))
                self.assertIsNotNone(why)
                self.assertIn("not under [supervise] allowed_workdirs", why)

    def test_a_workdir_of_another_owner_is_refused(self):
        from swarm import paths
        cfg = {**self.cfg, "supervise": {"enabled": True}}
        (paths.home() / "src" / "proj").mkdir(parents=True)
        real = os.stat

        def other_owner(p, *a, **k):
            s = real(p, *a, **k)
            if str(p).endswith("proj"):
                return os.stat_result((s.st_mode, s.st_ino, s.st_dev, s.st_nlink, s.st_uid + 1, s.st_gid,
                                       s.st_size, s.st_atime, s.st_mtime, s.st_ctime))
            return s
        with mock.patch("os.stat", side_effect=other_owner):
            self.assertIsNotNone(command.workdir_problem(cfg, str(paths.home() / "src" / "proj")))

    def test_the_launch_spec_carries_the_real_path(self):
        link = self.tmp / "worklink"
        link.symlink_to(self.work)
        with mock.patch.object(command, "workdir_for", return_value=str(link)):
            self.supervise()
        [run] = self.started
        self.assertEqual(run["cwd"], os.path.realpath(self.work))

    def test_a_disallowed_workdir_is_refused_with_a_clear_post(self):
        from swarm import paths
        (paths.home() / ".aws").mkdir()
        with mock.patch.object(command, "workdir_for", return_value=str(paths.home() / ".aws")):
            self.supervise()
        self.assertEqual(self.started, [])
        self.assertTrue(any(p.startswith(f"can't restart {self.name}: work dir ") and
                            "not under [supervise] allowed_workdirs" in p for p in self.posts()), self.posts())

    def test_allowed_workdirs_must_be_a_list_of_strings(self):
        for bad in ("~/src", [1], ["~/src", None]):
            with self.subTest(bad=bad):
                with self.assertRaises(st.SettingsError):
                    st.settings({"supervise": {"allowed_workdirs": bad}})

    def test_claude_replacement_in_a_disallowed_workdir_is_refused(self):
        from swarm import paths
        (paths.home() / ".claude").mkdir(exist_ok=True)
        with mock.patch.object(command, "workdir_for", return_value=str(paths.home() / ".claude")):
            self.supervise()
        self.assertEqual(self.started, [])
        self.assertTrue(any(p.startswith(f"can't restart {self.name}") and ".claude" in p for p in self.posts()))

    def test_an_unknown_recorded_harness_is_refused_not_a_crash(self):
        self.h.update_agent("orig", harness="evil")
        self.assertEqual(self.supervise(), 0)
        self.assertEqual(self.started, [])
        self.assertTrue(any(p.startswith(f"can't restart {self.name}") and "harness" in p for p in self.posts()))

    def test_a_recorded_model_that_is_not_a_model_name_is_ignored(self):
        self.h.update_agent("orig", model="--settings=/tmp/evil.json")
        self.supervise()
        [run] = self.started
        self.assertNotIn("--settings=/tmp/evil.json", run["argv"])

    def test_codex_replacement_joining_before_set_restart_agent(self):
        from swarm.supervisor import markers
        self.h.update_agent("orig", harness="codex")
        enrol(self.cfg, "orig", harness="codex", cwd=self.work)
        tid = "00000000-0000-4000-8000-0000000000d1"
        token = "k" * 32

        def fake_runner(cfg, run):
            self.started.append(run)
            self.assertIsNone(run["session_id"])                      # Codex: key unknown at launch
            self.assertTrue(markers.set_resume_token(Path(run["marker"]), token))
            with mock.patch.dict(os.environ, {markers.TOKEN_ENV: token}):   # its first tool call
                self.hook("turn", agent_id=None, session=tid, host="codex", turn_id="t1",
                          tool_name="Bash", tool_input={"command": "ls"})
            return 1
        self.supervise(start_runner=fake_runner)
        [run] = self.started
        self.assertEqual(run["argv"][:2], ["codex", "exec"])
        a = self.agent(tid)
        self.assertEqual((a.name, a.resume_of, a.harness), (self.name, "orig", "codex"))
        [r] = self.restarts()
        self.assertIsNone(r.new_agent_key)                             # joined before it was recorded
        run["agent_key"] = runner._marker_session(run["marker"])      # what the runner's loop reads
        self.assertEqual(run["agent_key"], tid)
        self.assertTrue(runner.finish(self.cfg, run, "completed"))
        [r] = self.restarts()
        self.assertEqual((r.new_agent_key, r.outcome), (tid, "completed"))
        self.assertFalse(Path(run["marker"]).exists())


if __name__ == "__main__":
    import unittest
    unittest.main()
