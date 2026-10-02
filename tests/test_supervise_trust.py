"""The supervisor's own state lives in a private directory no Codex sandbox can
write, and nothing it reads from a place a sandboxed agent can write (run files, resume markers)
is acted on unchecked. The attacks, reproduced: each must do nothing and log a
refusal."""
from __future__ import annotations

import unittest

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env
from support import tq
from test_supervise_runner import RunnerBase, fake
from test_supervise_setup import FakeRun

from swarm import cli as swarm
from swarm import codex_config, paths
from swarm.supervisor import command, lost, markers, outage, runner, settings as st


def _under(p: Path, root: Path) -> bool:
    p, root = Path(os.path.realpath(p)), Path(os.path.realpath(root))
    return p == root or root in p.parents


def supervise_log() -> str:
    try:
        return st.log_path().read_text()
    except OSError:
        return ""


class PrivateDirTests(Env):
    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        self.cfg = swarm.load_config(self.config)

    def test_every_supervisor_file_is_in_the_private_dir(self):
        d = st.private_dir()
        for p in (st.runs_dir(), st.state_path(), st.log_path(), outage.path(), lost.retry_state_path(),
                  command.lock_path()):
            self.assertTrue(_under(p, d), p)
        self.assertFalse(_under(d, paths.state_dir()))

    def test_the_private_dir_is_outside_every_codex_writable_root(self):
        default = swarm.load_config(Path("/nonexistent/swarm-config.toml"))
        for cfg in (self.cfg, default):
            roots = codex_config.required(cfg)[("sandbox_workspace_write", "writable_roots")]
            self.assertTrue(roots)
            for root in roots:
                self.assertFalse(_under(st.private_dir(), Path(root)), f"{st.private_dir()} under {root}")

    def test_created_0700_and_owned(self):
        d = st.ensure_private_dir(self.cfg)
        for p in (d, st.runs_dir()):
            s = os.lstat(p)
            self.assertTrue(stat.S_ISDIR(s.st_mode))
            self.assertEqual(stat.S_IMODE(s.st_mode), 0o700)
            self.assertEqual(s.st_uid, os.getuid())

    def test_a_symlinked_private_dir_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        st.private_dir().parent.mkdir(parents=True)
        st.private_dir().symlink_to(elsewhere)
        with self.assertRaises(st.PrivateDirError):
            st.ensure_private_dir(self.cfg)
        out = []
        with mock.patch("sys.stderr"):
            rc = command.run_pass(self.cfg, say=out.append, scope_available=lambda: True)
        self.assertEqual(rc, 1)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_symlinked_runs_dir_is_refused(self):
        d = st.ensure_private_dir(self.cfg)
        os.rmdir(st.runs_dir())
        (self.tmp / "elsewhere").mkdir()
        st.runs_dir().symlink_to(self.tmp / "elsewhere")
        with self.assertRaises(st.PrivateDirError):
            st.ensure_private_dir(self.cfg)
        self.assertTrue(d.is_dir())

    def test_a_loose_mode_is_refused(self):
        st.private_dir().mkdir(parents=True, mode=0o700)
        st.private_dir().chmod(0o755)
        with self.assertRaises(st.PrivateDirError):
            st.ensure_private_dir(self.cfg)

    def test_another_owner_is_refused(self):
        st.ensure_private_dir(self.cfg)
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            with self.assertRaises(st.PrivateDirError):
                st.ensure_private_dir(self.cfg)

    def test_a_private_dir_under_a_writable_root_is_refused(self):
        exposed = paths.share_dir()
        self.config.write_text(self.config.read_text().replace(f'spool_dir = {tq(self.spool_dir)}',
                                                               f'spool_dir = {tq(exposed)}'))
        cfg = swarm.load_config(self.config)
        with self.assertRaises(st.PrivateDirError) as cm:
            st.ensure_private_dir(cfg)
        self.assertIn("Codex writable root", str(cm.exception))

    def test_the_off_file_in_the_private_dir_counts_too(self):
        # a sandboxed agent can delete ~/.local/state/swarm/supervise.off; not this one
        st.ensure_private_dir(self.cfg)
        self.assertTrue(st.enabled(self.cfg))
        (st.private_dir() / "supervise.off").touch()
        self.assertFalse(st.enabled(self.cfg))
        out = []
        self.assertEqual(command.run_pass(self.cfg, say=out.append), 0)
        self.assertIn("supervisor off", out[0])

    def test_repro_in_the_old_location_does_nothing(self):
        """r9999.json dropped into ~/.local/state/swarm/replacements."""
        victim = self.tmp / "victim.txt"
        victim.write_text("keep me")
        old = paths.state_dir() / "replacements"
        old.mkdir(parents=True)
        (old / "r9999.json").write_text(json.dumps(
            {"restart_id": 9999, "job": "x", "name": "x", "marker": str(victim), "limit_seconds": 1,
             "exited": True, "board_done": True, "unit": "swarm-supervise.timer", "started_epoch": 0}))
        out = []
        self.assertEqual(command.run_pass(self.cfg, say=out.append, scope_available=lambda: True), 0)
        self.assertTrue(victim.exists())
        self.assertTrue((old / "r9999.json").exists())


class RunFileTrustTests(RunnerBase):
    def setUp(self):
        super().setUp()
        self.victim = self.tmp / "victim.txt"
        self.victim.write_text("keep me")

    def plant(self, run: dict, name: str | None = None) -> Path:
        """A run file written straight into the runs dir (what an attacker with write access, or
        a corrupted file, would leave)."""
        st.ensure_private_dir(self.cfg)
        runner.run_dir(self.cfg).mkdir(mode=0o700, exist_ok=True)
        p = runner.run_dir(self.cfg) / (name or f"r{run.get('restart_id')}.json")
        p.write_text(json.dumps(run))
        return p

    def exited(self, **changes) -> dict:
        run = self.run_dict(["true"])
        run.pop("stdin")
        run.update(exited=True, board_done=True, outcome="completed", **changes)
        return run

    def reap(self) -> int:
        with self.board() as b:
            return runner.reap(self.cfg, b)

    def assert_refused(self, p: Path, what: str):
        self.assertEqual(self.reap(), 0)
        self.assertTrue(p.exists(), "a refused run file is left for a person to look at")
        self.assertIn("refusing run file", supervise_log())
        self.assertIn(what, supervise_log())
        self.assertIsNone(self.restart().outcome)

    def test_a_foreign_marker_path_is_refused(self):
        p = self.plant(self.exited(marker=str(self.victim)))
        self.assert_refused(p, "marker")
        self.assertTrue(self.victim.exists())

    def test_another_restarts_marker_is_refused(self):
        other = markers.resume_marker_path(self.cfg, "J", self.r.id + 1)
        other.write_text("{}")
        p = self.plant(self.exited(marker=str(other)))
        self.assert_refused(p, "marker")
        self.assertTrue(other.exists())

    def test_a_marker_reached_through_dotdot_is_refused(self):
        name = markers.resume_marker_path(self.cfg, "J", self.r.id).name
        sneaky = self.markers / "x" / ".." / ".." / "elsewhere" / name
        p = self.plant(self.exited(marker=str(sneaky)))
        self.assert_refused(p, "marker")

    def test_a_foreign_unit_is_refused(self):
        run = self.run_dict(["true"])
        run.pop("stdin")
        run.update(unit="swarm-supervise.timer", started_epoch=0, launch_epoch=0)
        p = self.plant(run)
        self.assert_refused(p, "unit")
        self.assertFalse([c for c in self.calls() if "swarm-supervise.timer" in c])

    def test_another_restarts_unit_is_refused(self):
        run = self.run_dict(["true"])
        run.pop("stdin")
        run.update(unit=f"swarm-r{self.r.id + 1}-abcdef01.scope", started_epoch=0, launch_epoch=0)
        p = self.plant(run)
        self.assert_refused(p, "unit")
        self.assertFalse([c for c in self.calls() if "kill" in c or "stop" in c])

    def test_non_integer_fields_are_refused(self):
        for bad in ({"child_pgid": "1"}, {"child_pid": True}, {"limit_seconds": "1"},
                    {"started_epoch": [0]}, {"restart_id": str(self.r.id)}):
            with self.subTest(bad=bad):
                run = self.run_dict(["true"])
                run.pop("stdin")
                run.update(bad)
                p = self.plant(run, name=f"r{self.r.id}.json")
                self.assert_refused(p, "run file")
                p.unlink()

    def test_a_restart_id_other_than_its_file_name_is_refused(self):
        p = self.plant(self.exited(), name=f"r{self.r.id + 7}.json")
        self.assertEqual(self.reap(), 0)
        self.assertIn("refusing run file", supervise_log())
        self.assertIsNone(self.restart().outcome)
        self.assertTrue(p.exists())

    def test_a_run_naming_another_jobs_restart_is_refused(self):
        self.cli("activate", "--job", "K", "--session", "sess-2")
        run = self.exited(job="K", marker=str(markers.resume_marker_path(self.cfg, "K", self.r.id)))
        p = self.plant(run)
        self.assert_refused(p, "restart row")

    def test_a_run_naming_another_os_users_restart_is_refused(self):
        self.hook("start", agent_id="orig2", session="sess-1")
        with self.board() as b:
            b.close_agent("orig2", "stuck:dead")
            with mock.patch("getpass.getuser", return_value="someone-else"):
                r2 = b.record_restart("J", "orig2", "orig2", "stuck:dead", "claude", 1.0)
        run = self.exited(restart_id=r2.id, resume_of="orig2",
                          marker=str(markers.resume_marker_path(self.cfg, "J", r2.id)))
        p = self.plant(run)
        self.assertEqual(self.reap(), 0)
        self.assertIn("restart row", supervise_log())
        with self.board() as b:
            self.assertIsNone(next(x for x in b.restarts(job="J") if x.id == r2.id).outcome)
        self.assertTrue(p.exists())

    def test_a_run_of_another_board_is_left_alone(self):
        """Two boards used by one OS user; restart ids collide. Not refused (it is valid for
        its own board), not finished on this one."""
        p = self.plant(self.exited(board="sqlite-some-other-board"))
        self.assertEqual(self.reap(), 0)
        self.assertTrue(p.exists())
        self.assertIsNone(self.restart().outcome)
        self.assertNotIn("refusing", supervise_log())

    def test_a_run_file_without_a_board_is_refused(self):
        run = self.exited()
        run.pop("board")
        p = self.plant(run)
        self.assert_refused(p, "board")

    def test_a_symlinked_run_file_is_refused(self):
        target = self.tmp / "target.json"
        target.write_text(json.dumps(self.exited()))
        st.ensure_private_dir(self.cfg)
        runner.run_dir(self.cfg).mkdir(mode=0o700, exist_ok=True)
        (runner.run_dir(self.cfg) / f"r{self.r.id}.json").symlink_to(target)
        self.assertEqual(self.reap(), 0)
        self.assertIsNone(self.restart().outcome)
        self.assertTrue(target.exists())

    def test_a_valid_run_file_is_still_finished(self):
        run = self.exited()
        run.pop("board_done")
        self.plant(run)
        self.assertEqual(self.reap(), 1)
        self.assertEqual(self.restart().outcome, "completed")
        self.assertNotIn("refusing", supervise_log())

    # ---- kill switches: the runner checks them right before it launches

    def test_runner_launches_nothing_when_switched_off(self):
        argv = [fake(self.bin / "claude", 'touch "$0.ran"\n')]
        for off in (lambda: st.off_file(), lambda: st.private_dir() / "supervise.off"):
            with self.subTest(off=off()):
                p = off()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
                self.assertEqual(self.execute(self.run_dict(argv)), "cancelled")
                self.assertFalse((self.bin / "claude.ran").exists())
                p.unlink()
                with self.board() as b:
                    self.hook("start", agent_id=f"o-{len(str(p))}", session="sess-1")
                    self.r = b.record_restart("J", "orig", f"o-{len(str(p))}", "stuck:dead", "claude", 1.0)
                    b.close_agent(f"o-{len(str(p))}", "stuck:dead")

    def test_runner_launches_nothing_when_disabled_or_the_job_is_unsupervised(self):
        argv = [fake(self.bin / "claude", 'touch "$0.ran"\n')]
        self.cfg["supervise"]["enabled"] = False
        self.assertEqual(self.execute(self.run_dict(argv)), "cancelled")
        self.cfg["supervise"]["enabled"] = True
        with self.board() as b:
            b.set_job_supervise("J", False)
            self.hook("start", agent_id="o2", session="sess-1")
            b.close_agent("o2", "stuck:dead")
            self.r = b.record_restart("J", "o2", "o2", "stuck:dead", "claude", 1.0)
        run = dict(self.run_dict(argv), resume_of="o2")
        self.assertEqual(self.execute(run), "cancelled")
        self.assertFalse((self.bin / "claude.ran").exists())

    # ---- kill switches: the hook enrols no replacement when switched off

    def _replacement_turn(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        markers.write_resume_marker(self.cfg, "J", self.r.id, resume_of="orig", name=self.name,
                                    harness="claude", session_id=sid)
        return sid, self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})

    def test_hook_refuses_a_replacement_when_the_off_file_exists(self):
        st.off_file().parent.mkdir(parents=True, exist_ok=True)
        st.off_file().touch()
        sid, out = self._replacement_turn()
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("switched off", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(self.agent(sid))

    def test_hook_refuses_a_replacement_when_disabled(self):
        self.cfg["supervise"]["enabled"] = False
        sid, out = self._replacement_turn()
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.agent(sid))

    def test_hook_refuses_a_replacement_when_the_job_is_unsupervised(self):
        with self.board() as b:
            b.set_job_supervise("J", False)
        sid, out = self._replacement_turn()
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.agent(sid))

    # ---- the swapped command: the runner never reads its run from a file

    def test_the_run_file_never_holds_the_brief(self):
        p = runner.write_run(self.run_dict(["true"]))
        self.assertNotIn("stdin", json.loads(p.read_text()))
        self.assertNotIn("BRIEF", p.read_text())

    def test_start_hands_the_run_over_a_pipe_not_a_path(self):
        seen, fed = {}, bytearray()

        class Pipe:
            def write(self, data):
                fed.extend(data)

            def close(self):
                pass

        class P:
            pid = 4242
            stdin = Pipe()

        def popen(argv, **kw):
            seen.update(argv=argv, **kw)
            return P()
        run = self.run_dict(["true"])
        runner.start(self.cfg, run, popen=popen)
        self.assertNotIn(str(runner.run_file(self.r.id, self.cfg)), seen["argv"])
        self.assertFalse(any(a.endswith(".json") for a in seen["argv"]))
        self.assertEqual(json.loads(bytes(fed))["stdin"], "BRIEF")

    def test_a_swapped_run_file_changes_nothing_the_runner_runs(self):
        fed = bytearray()

        class Pipe:
            def write(self, data):
                fed.extend(data)

            def close(self):
                pass

        class P:
            pid = 4242
            stdin = Pipe()
        run = self.run_dict(["true"])
        runner.start(self.cfg, run, popen=lambda argv, **kw: P())
        pwned = self.tmp / "pwned"
        evil = dict(run, argv=["/bin/sh", "-c", f"touch {pwned}"], cwd="/", stdin="evil")
        evil.pop("stdin")
        runner.run_file(self.r.id, self.cfg).write_text(json.dumps(evil))   # the swap, before the runner reads
        import io
        with mock.patch.object(runner, "execute", return_value="completed") as ex, \
                mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(bytes(fed)))):
            self.assertEqual(runner.main([]), 0)
        got = ex.call_args[0][1]
        self.assertEqual((got["argv"], got["cwd"], got["stdin"]), (["true"], str(self.workdir), "BRIEF"))
        self.assertFalse(pwned.exists())

    # ---- the resume marker is sandbox-writable: what the runner reads from it is checked

    def test_a_marker_bound_to_a_foreign_agent_is_not_adopted(self):
        self.hook("start", agent_id="victim", session="sess-1")        # an active agent of J
        argv = [fake(self.bin / "codex", "cat >/dev/null; sleep 30\n")]
        run = self.run_dict(argv, harness="codex", enrol=1.0)
        m = Path(run["marker"])
        data = json.loads(m.read_text())
        data["session_id"] = "victim"                                   # what a sandboxed agent writes
        m.write_text(json.dumps(data))
        self.assertEqual(self.execute(run), "not_enrolled")
        v = self.agent("victim")
        self.assertIsNone(v.ended_at, "the victim is not closed as the replacement")
        self.assertIsNone(self.restart().new_agent_key)
        self.assertIn("not a replacement of", supervise_log())

    def test_finish_never_closes_an_agent_that_is_not_the_replacement(self):
        self.hook("start", agent_id="victim", session="sess-1")
        run = self.run_dict(["true"])
        run.update(agent_key="victim", exited=True)
        self.assertTrue(runner.finish(self.cfg, run, "timeout"))
        self.assertIsNone(self.agent("victim").ended_at)
        self.assertIsNone(self.restart().new_agent_key)

    def test_a_crafted_resume_marker_enrols_nobody(self):
        """A sandboxed agent writes <job>--resume-r77.json naming a departed agent: the hook of its
        session must not make it that agent (no restart row backs it)."""
        sid = "99999999-2222-4333-8444-555555555555"
        p = markers.resume_marker_path(self.cfg, "J", 77)
        p.write_text(json.dumps({"job": "J", "session_id": sid, "adopt_running": False,
                                 "resume": {"agent_key": sid, "resume_of": "orig", "name": self.name,
                                            "harness": "claude", "restart_id": 77}}))
        out = self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.agent(sid))

    def test_a_resume_marker_naming_another_agent_than_its_row_enrols_nobody(self):
        self.hook("start", agent_id="other", session="sess-1")
        with self.board() as b:
            b.close_agent("other", "stuck:dead")
        sid = "99999999-2222-4333-8444-555555555556"
        p = markers.resume_marker_path(self.cfg, "J", self.r.id)    # the real row replaces "orig"
        p.write_text(json.dumps({"job": "J", "session_id": sid, "adopt_running": False,
                                 "resume": {"agent_key": sid, "resume_of": "other", "name": "x",
                                            "harness": "claude", "restart_id": self.r.id}}))
        out = self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.agent(sid))


class PrivateFileDisciplineTests(RunnerBase):
    """The private dir may still be writable by a sandbox (a
    user-added writable root, a Codex session rooted at $HOME), so no file in it is ever followed
    through a planted symlink or hard link, created over an existing path, or truncated."""

    SECRET = "TOPSECRET-do-not-copy-0123456789"

    def setUp(self):
        super().setUp()
        self.victim = self.tmp / "victim.txt"
        self.victim.write_text(self.SECRET)
        st.ensure_private_dir(self.cfg)
        runner.run_dir(self.cfg).mkdir(mode=0o700, exist_ok=True)

    def untouched(self):
        self.assertEqual(self.victim.read_text(), self.SECRET)

    def plant(self, path: Path, how="symlink"):
        path.parent.mkdir(parents=True, exist_ok=True)
        if how == "symlink":
            path.symlink_to(self.victim)
        else:
            os.link(self.victim, path)

    def test_planted_err_and_out_symlinks_are_never_followed(self):
        for name in ("err", "out"):
            for how in ("symlink", "hardlink"):
                with self.subTest(name=name, how=how):
                    p = runner.run_dir(self.cfg) / f"r{self.r.id}.{name}"
                    self.plant(p, how)
                    argv = [fake(self.bin / "claude", "echo noise; echo noise >&2; exit 3\n")]
                    run = self.run_dict(argv)
                    runner.execute(self.cfg, run, poll=0.05, board_every=0.1)
                    self.untouched()
                    tail = runner.run_dir(self.cfg) / f"r{self.r.id}.tail.txt"
                    self.assertNotIn(self.SECRET, tail.read_text() if tail.exists() else "")
                    for q in runner.run_dir(self.cfg).glob(f"r{self.r.id}.*"):
                        if q.is_symlink() or q.name.endswith((".err", ".out")) or ".stale-" in q.name:
                            q.unlink()
                    with self.board() as b:     # a fresh restart for the next round
                        self.hook("start", agent_id=f"o-{name}-{how}", session="sess-1")
                        b.close_agent(f"o-{name}-{how}", "stuck:dead")
                        self.r = b.record_restart("J", "orig", f"o-{name}-{how}", "stuck:dead", "claude", 1.0)

    def test_the_output_tail_never_copies_a_planted_file(self):
        self.plant(runner.run_dir(self.cfg) / f"r{self.r.id}.out")
        runner._keep_output_tail(self.r.id, self.cfg)
        tail = runner.run_dir(self.cfg) / f"r{self.r.id}.tail.txt"
        self.assertNotIn(self.SECRET, tail.read_text() if tail.exists() else "")
        self.untouched()

    def test_a_planted_run_file_is_never_written_through(self):
        for how in ("symlink", "hardlink"):
            with self.subTest(how=how):
                p = runner.run_file(self.r.id, self.cfg)
                self.plant(p, how)
                runner.write_run(self.run_dict(["true"]))
                self.untouched()
                p.unlink()

    def test_a_planted_tail_file_is_never_written_through(self):
        (runner.run_dir(self.cfg) / f"r{self.r.id}.err").write_text("some stderr\n")
        self.plant(runner.run_dir(self.cfg) / f"r{self.r.id}.tail.txt", "hardlink")
        runner._keep_output_tail(self.r.id, self.cfg)
        self.untouched()

    def test_a_planted_outage_file_is_neither_followed_nor_written(self):
        from swarm.supervisor import outage
        self.victim.write_text(json.dumps({"started": "2026-09-27T10:00:00+00:00", "recovered": None}))
        before = self.victim.read_text()
        self.plant(outage.path())
        self.assertIsNone(outage.current())                   # its content is not read
        outage.note_unreachable()
        self.assertEqual(self.victim.read_text(), before)

    def test_planted_state_log_and_retry_files(self):
        for path, act in ((st.state_path(), lambda: st.save_state({"x": 1})),
                          (st.log_path(), lambda: st.log("a line")),
                          (lost.retry_state_path(), lambda: lost._write_retries({"a": 1}))):
            for how in ("symlink", "hardlink"):
                with self.subTest(path=path.name, how=how):
                    if os.path.lexists(path):
                        path.unlink()
                    self.plant(path, how)
                    act()
                    self.untouched()
                    if path == st.state_path():
                        self.assertNotIn("TOPSECRET", json.dumps(st.load_state()))

    def test_a_planted_lock_file_is_refused(self):
        self.plant(runner.lock_file(self.r.id, self.cfg), "hardlink")
        self.assertIsNone(runner.own(self.r.id, self.cfg))
        self.untouched()

    def test_a_symlinked_parent_of_the_private_dir_is_refused(self):
        """~/.local/share/swarm swapped for a symlink (a sandbox with $HOME writable)."""
        import shutil
        other = self.tmp / "other"
        other.mkdir()
        shutil.rmtree(paths.share_dir())
        paths.share_dir().symlink_to(other)
        with self.assertRaises(st.PrivateDirError):
            st.ensure_private_dir(self.cfg)
        with self.assertRaises((st.PrivateDirError, OSError)):
            runner.write_run(self.run_dict(["true"]))
        self.assertEqual(list(other.iterdir()), [])


class CodexExposureTests(Env):
    """The real Codex config (read only) may make the private dir writable."""

    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        self.cfg = swarm.load_config(self.config)

    def codex_config(self, text: str, home: Path | None = None):
        d = home or (paths.home() / ".codex")
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.toml").write_text(text)

    def test_no_codex_config_no_exposure(self):
        self.assertEqual(st.codex_exposure(), [])

    def test_a_writable_root_covering_the_private_dir_is_reported(self):
        self.codex_config(f'[sandbox_workspace_write]\nwritable_roots = ["{paths.home()}"]\n')
        self.assertEqual([str(p) for p in st.codex_exposure()], [str(paths.home())])
        self.codex_config(f'[profiles.p.sandbox_workspace_write]\nwritable_roots = ["{paths.home() / ".local"}"]\n')
        self.assertEqual(len(st.codex_exposure()), 1)
        self.codex_config(f'[sandbox_workspace_write]\nwritable_roots = ["{paths.home() / "src"}"]\n')
        self.assertEqual(st.codex_exposure(), [])

    def test_codex_home_is_honoured(self):
        alt = self.tmp / "codexhome"
        self.codex_config(f'[sandbox_workspace_write]\nwritable_roots = ["{paths.home()}"]\n', alt)
        self.assertEqual(st.codex_exposure(), [])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(alt)}):
            self.assertEqual(len(st.codex_exposure()), 1)

    def test_pass_refuses_when_exposed_doctor_fails(self):
        """An exposed private tree is a refusal, not a warning: one
        log line, no pass (nothing closed, nothing launched), a non-zero exit."""
        from swarm import bootstrap
        self.codex_config(f'[sandbox_workspace_write]\nwritable_roots = ["{paths.home()}"]\n')
        before = (paths.home() / ".codex" / "config.toml").read_text()
        out, started = [], []
        with mock.patch("sys.stderr"):
            rc = command.run_pass(self.cfg, say=out.append, scope_available=lambda: True,
                                  start_runner=lambda cfg, run: started.append(run))
        self.assertNotEqual(rc, 0)
        self.assertEqual(started, [])
        self.assertIn("refusing", supervise_log())
        self.assertIn("writable", supervise_log())
        self.assertNotIn("last_run_at", st.load_state())          # no pass ran
        self.assertFalse(os.path.lexists(command.lock_path()))    # not even the pass lock
        with mock.patch("sys.stderr"):
            self.assertNotEqual(command.run_pass(self.cfg, dry_run=True, say=out.append,
                                                 scope_available=lambda: True), 0)
        with mock.patch("swarm.supervisor.runner.scope_available", return_value=True):
            checks = {c.name: c for c in bootstrap.supervisor_checks(self.cfg, "claude", run=FakeRun(),
                                                                      which=lambda b, path=None: "/x")}
        self.assertIs(checks["supervise private dir"].ok, False)
        self.assertEqual((paths.home() / ".codex" / "config.toml").read_text(), before)   # never written

    def test_exposure_covers_the_whole_share_tree(self):
        """~/.local/share/swarm holds the venv, host/ (hook logs, enrolment records) and
        supervisor/: a writable root on any of them, or above, is exposure."""
        for root in (paths.share_dir(), paths.share_dir() / "venv", paths.share_dir() / "host",
                     paths.share_dir() / "host" / "enrolled", paths.home() / ".local" / "share"):
            with self.subTest(root=root):
                self.codex_config(f'[sandbox_workspace_write]\nwritable_roots = ["{root}"]\n')
                self.assertEqual([str(p) for p in st.codex_exposure()], [str(root)])
        self.codex_config(f'[sandbox_workspace_write]\nwritable_roots = ["{paths.home() / ".local" / "share" / "swarm-other"}"]\n')
        self.assertEqual(st.codex_exposure(), [])

    def test_a_swarm_writable_root_inside_the_share_tree_is_refused(self):
        exposed = paths.share_dir() / "venv"
        self.config.write_text(self.config.read_text().replace(f'spool_dir = {tq(self.spool_dir)}',
                                                               f'spool_dir = {tq(exposed)}'))
        with self.assertRaises(st.PrivateDirError):
            st.ensure_private_dir(swarm.load_config(self.config))

    def test_doctor_ok_without_exposure(self):
        from swarm import bootstrap
        with mock.patch("swarm.supervisor.runner.scope_available", return_value=True):
            checks = {c.name: c for c in bootstrap.supervisor_checks(self.cfg, "claude", run=FakeRun(),
                                                                      which=lambda b, path=None: "/x")}
        self.assertIs(checks["supervise private dir"].ok, True)


class WorkdirRaceTests(RunnerBase):
    """The pass checks the work dir, then the runner launches
    later; a sandbox that can write under the allowed root swaps the checked directory for a
    symlink to ~/.aws in between. The runner re-walks it with O_NOFOLLOW and starts the session
    in the verified directory by descriptor: never in the symlink's target."""

    def setUp(self):
        super().setUp()
        self.src = self.tmp / "src"
        self.proj = self.src / "proj"
        self.proj.mkdir(parents=True)
        self.aws = paths.home() / ".aws"
        self.aws.mkdir()
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.src)]
        self.argv = [fake(self.bin / "claude", 'pwd -P > "$0.cwd"; touch landed; exit 0\n')]

    def run_in(self, cwd):
        return dict(self.run_dict(self.argv), cwd=str(cwd))

    def test_the_session_runs_in_the_checked_directory(self):
        out = self.execute(self.run_in(self.proj))
        self.assertNotEqual(out, "refused")
        self.assertEqual((self.bin / "claude.cwd").read_text().strip(), str(self.proj))
        self.assertTrue((self.proj / "landed").exists())

    def test_a_directory_swapped_for_a_symlink_is_refused(self):
        run = self.run_in(self.proj)                         # what the pass checked and recorded
        self.proj.rename(self.src / "proj.orig")             # the swap, before the runner launches
        self.proj.symlink_to(self.aws)
        self.assertEqual(self.execute(run), "refused")
        self.assertFalse((self.bin / "claude.cwd").exists())
        self.assertFalse((self.aws / "landed").exists())
        self.assertIn("work dir", supervise_log())

    def test_a_parent_swapped_for_a_symlink_is_refused(self):
        run = self.run_in(self.proj)
        self.src.rename(self.tmp / "src.orig")
        (self.tmp / "evil").mkdir()
        (self.tmp / "evil" / "proj").symlink_to(self.aws)
        self.src.symlink_to(self.tmp / "evil")
        self.assertEqual(self.execute(run), "refused")
        self.assertFalse((self.aws / "landed").exists())

    def test_a_workdir_outside_the_allowlist_is_refused_by_the_runner(self):
        self.assertEqual(self.execute(self.run_in(self.aws)), "refused")
        self.assertFalse((self.aws / "landed").exists())

    def test_a_dot_directory_below_the_root_is_refused_by_the_runner(self):
        (self.src / "x" / ".git").mkdir(parents=True)
        self.assertEqual(self.execute(self.run_in(self.src / "x" / ".git")), "refused")

    def test_a_swap_after_the_open_still_lands_in_the_verified_inode(self):
        """The directory is opened first, then swapped: the session still starts in the opened
        directory (by descriptor), never in the symlink target."""
        real = runner.open_workdir

        def open_then_swap(cfg, path, **kw):
            fd, why = real(cfg, path, **kw)
            self.proj.rename(self.src / "proj.orig")
            self.proj.symlink_to(self.aws)
            return fd, why
        with mock.patch.object(runner, "open_workdir", side_effect=open_then_swap):
            self.execute(self.run_in(self.proj))
        self.assertFalse((self.aws / "landed").exists())
        self.assertTrue((self.src / "proj.orig" / "landed").exists())

    def test_codex_gets_no_cd_flag(self):
        from swarm.supervisor import launch
        spec = launch.codex_spec(self.cfg, prompt="p", workdir=str(self.proj), model=None, minutes=5)
        self.assertNotIn("--cd", spec.argv)
        self.assertNotIn("-C", spec.argv)
        self.assertEqual(spec.cwd, str(self.proj))


from test_supervise_command import SuperviseEnv as _SuperviseBase, claude_transcript, enrol  # noqa: E402


class ForgedRowTests(_SuperviseBase):
    """Board rows are written by agents (sandboxed or not) and, through
    the shared database role, by the other OS user. Ownership, work dir and harness of a
    replacement come only from this host user's private enrolment records."""

    def forge(self, key, *, harness="claude", cwd=None, job="J"):
        """A stuck-closed row claiming this host and OS user, with a transcript whose cwd is `cwd`
        (what a forger writes), and no enrolment record."""
        self.hook("start", agent_id=key, session="sess-1")
        self.h.update_agent(key, harness=harness)
        name = self.agent(key).name
        if cwd is not None:
            self.seed(job, key, name, claude_transcript(str(cwd)))
        with self.board() as b:
            b.close_agent(key, "stuck:dead")
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        enrolment.remove(store_key(self.cfg), key)   # whatever a hook wrote: this row is not enrolled
        return name

    def test_forged_os_user_not_replaced(self):
        import getpass
        name = self.forge("forged", cwd=self.work)
        a = self.agent("forged")
        self.assertEqual((a.host, a.os_user), (os.uname().nodename, getpass.getuser()))
        with self.board() as b:
            keys = [c.agent.agent_key for c in command.candidates(b, self.cfg, st.settings(self.cfg))]
        self.assertNotIn("forged", keys)
        self.supervise()
        self.assertFalse([r for r in self.started if r["name"] == name])
        self.assertFalse([r for r in self.restarts() if r.old_agent_key == "forged"])
        self.supervise(dry_run=True)
        self.assertIn(f"not restarting {name} on J: no local enrolment record", "\n".join(self.out))

    def test_a_record_of_another_job_is_not_ownership(self):
        name = self.forge("forged", cwd=self.work)
        enrol(self.cfg, "forged", job="other-job", cwd=self.work)
        self.supervise()
        self.assertFalse([r for r in self.started if r["name"] == name])

    def test_workdir_from_enrolment_only(self):
        other, marker_dir = self.tmp / "board-cwd", self.tmp / "marker-cwd"
        other.mkdir()
        marker_dir.mkdir()
        self.seed("J", "orig", self.name, claude_transcript(str(other)))       # the board's copy
        m = json.loads((self.markers / "J.json").read_text())
        (self.markers / "J.json").write_text(json.dumps({**m, "cwd": str(marker_dir)}))
        self.supervise()
        [run] = self.started
        self.assertEqual(run["cwd"], str(self.work))

    def test_a_gone_enrolment_cwd_has_no_fallback(self):
        other = self.tmp / "board-cwd"
        other.mkdir()
        self.seed("J", "orig", self.name, claude_transcript(str(other)))
        m = json.loads((self.markers / "J.json").read_text())
        (self.markers / "J.json").write_text(json.dumps({**m, "cwd": str(other)}))
        enrol(self.cfg, "orig", cwd=self.tmp / "gone")
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertTrue(any("work directory" in p for p in self.posts()))

    def test_harness_mismatch_refused(self):
        self.h.update_agent("orig", harness="codex")      # the row says codex; it was enrolled as claude
        self.supervise()
        self.assertEqual(self.started, [])
        self.assertTrue(any(p.startswith(f"can't restart {self.name}") and "harness" in p for p in self.posts()))
        [r] = self.restarts()
        self.assertEqual(r.outcome, "refused")

    def test_the_harness_comes_from_the_record(self):
        enrol(self.cfg, "orig", harness="codex", cwd=self.work)
        self.h.update_agent("orig", harness="codex")
        self.supervise()
        [run] = self.started
        self.assertEqual((run["harness"], run["argv"][:2]), ("codex", ["codex", "exec"]))

    def test_record_removed_between_selection_and_action(self):
        from swarm import enrolment
        from swarm.board.autoinit import store_key
        with self.board() as b:
            b.close_agent("orig", "stuck:dead")
            [c] = command.candidates(b, self.cfg, st.settings(self.cfg))
            enrolment.remove(store_key(self.cfg), "orig")
            fresh, why = command._recheck(b, self.cfg, st.settings(self.cfg), c, None)
        self.assertIsNone(fresh)
        self.assertIn("enrolment", why)
