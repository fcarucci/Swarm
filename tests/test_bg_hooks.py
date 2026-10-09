"""swarm-orphans through the hooks and the CLI: a member's CI wait is refused with the exact
`swarm ci wait` to run instead; a member's background Bash call is rewritten to run under
`swarm bg` and recorded; a finished agent's commands get a detached reap; `bg list`, `bg reap`,
`status --job`, `deactivate`, the auto-close sweep, the supervisor pass and `doctor` all see
orphans."""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from test_goals import GoalEnv  # noqa: E402  (sets sys.path)
from support import posix_only, wait_until  # noqa: E402

from swarm import bg, cli, shellguard  # noqa: E402

LINUX = sys.platform.startswith("linux") and hasattr(os, "pidfd_open")
SHA = "0123456789abcdef0123456789abcdef01234567"


def fake_repo(root: Path, url: str = "git@github.com:acme/rocket.git") -> Path:
    (root / ".git/refs/heads").mkdir(parents=True)
    (root / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git/refs/heads/main").write_text(SHA + "\n")
    (root / ".git/config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')
    return root


class BgHookEnv(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate("J")
        self.spawn_worker("w1")
        self.name = self.member("w1").name

    def bash(self, agent, command, background=False, cwd=None):
        ti = {"command": command, "description": "x"}
        if background:
            ti["run_in_background"] = True
        payload = dict(agent_id=agent, session="sess-1", tool_name="Bash", tool_input=ti,
                       transcript_path=self.main_transcript())
        if cwd:
            payload["cwd"] = str(cwd)
        return self.hook("turn", **payload) or {}

    @staticmethod
    def rewritten(out):
        return (out.get("hookSpecificOutput", {}).get("updatedInput") or {}).get("command")

    @staticmethod
    def denied(out):
        o = out.get("hookSpecificOutput", {})
        return o.get("permissionDecisionReason") if o.get("permissionDecision") == "deny" else None


# --------------------------------------------------------------------------- A: CI waits

class CiWaitGuardTests(BgHookEnv):
    def test_gh_run_watch_is_refused_and_says_the_plugin_is_missing(self):
        why = self.denied(self.bash("w1", "gh run watch 123 --interval 60"))
        self.assertIsNotNone(why)
        self.assertIn("`gh run watch`", why)
        self.assertIn("plugin", why)
        self.assertIn("not installed or is disabled", why)
        self.assertNotIn(" ci wait --repo", why)   # never a command that doesn't exist here

    def test_one_shot_checks_and_other_commands_pass(self):
        for cmd in ("gh run view 123 --json conclusion", "gh pr checks 7", "git commit -m 'no gh run watch'",
                    "gh api repos/o/r/pulls", "ls"):
            self.assertIsNone(self.denied(self.bash("w1", cmd)), cmd)

    def test_poll_loops_and_watch_flags_are_refused(self):
        for cmd in ("while true; do gh run view 1 --json status; sleep 60; done",
                    'until [ "$(gh run list -L1 --json status -q .[0].status)" = completed ]; do sleep 30; done',
                    "watch -n 60 gh run list", "gh pr checks 7 --watch",
                    "for i in 1 2 3; do gh api repos/o/r/actions/runs; sleep 9; done"):
            self.assertIsNotNone(self.denied(self.bash("w1", cmd)), cmd)

    def test_the_orchestrator_is_not_gated(self):
        out = self.hook("turn", agent_id=None, session="sess-1", tool_name="Bash",
                        tool_input={"command": "gh run watch 1"}, transcript_path=self.main_transcript())
        self.assertIsNone(self.denied(out or {}))

    def test_judge_may_verify_once_but_not_watch(self):
        self.activate_goal("G")
        self.spawn_judge("judge-1", job="G")
        self.assertIsNone(self.denied(self.bash("judge-1", "gh run view 99 --json conclusion")))
        self.assertIsNotNone(self.denied(self.bash("judge-1", "gh run watch 99")))


class CiWaitWithPluginTests(BgHookEnv):
    plugins_disabled = ("engineering-team", "ask-answer")   # the ci plugin is loaded

    def test_refusal_gives_the_exact_replacement_from_the_work_dir(self):
        repo = fake_repo(self.tmp / "repo")
        why = self.denied(self.bash("w1", "gh run watch 123", cwd=repo))
        self.assertIn(f"ci wait --repo acme/rocket --sha {SHA}", why)
        self.assertIn(f"ci status --repo acme/rocket --sha {SHA}", why)
        self.assertIn("gh run view <run-id> --json conclusion` is still allowed", why)

    def test_repo_flag_wins_and_unknowns_are_placeholders(self):
        why = self.denied(self.bash("w1", "gh run watch 5 -R other/thing", cwd=self.tmp))
        self.assertIn("ci wait --repo other/thing --sha '<exact head SHA>'", why)


class CiWaitShellguardTests(BgHookEnv):
    def test_quoted_data_is_not_code_but_substitutions_are(self):
        self.assertIsNone(shellguard.ci_wait("echo 'gh run watch 1'"))
        self.assertIsNone(shellguard.ci_wait('git commit -m "gh run watch is banned"'))
        self.assertIsNotNone(shellguard.ci_wait('while [ "$(gh run view 1 --json status)" ]; do sleep 1; done'))
        self.assertIsNotNone(shellguard.ci_wait("timeout 600 /usr/bin/gh run watch 5"))
        self.assertIsNotNone(shellguard.ci_wait("gh -R o/r run watch 5"))
        self.assertIsNotNone(shellguard.ci_wait(
            "while sleep 60; do curl -s https://gitea.x/api/v1/repos/o/r/commits/abc/status; done"))
        self.assertIsNone(shellguard.ci_wait("curl -s https://gitea.x/api/v1/repos/o/r/commits/abc/status"))
        self.assertEqual(shellguard.ci_repo("cd x && gh run watch 1 --repo o/r"), "o/r")

    def test_loop_conditions_are_polls(self):
        for cmd in ("while gh run view 1 --json status | grep -q in_progress; do sleep 30; done",
                    "until gh run view 1 --exit-status; do sleep 30; done",
                    "while ! gh run view 1 --json conclusion -q .conclusion | grep -q success; do sleep 9; done",
                    "until ! gh api repos/o/r/actions/runs/5 >/dev/null; do :; done"):
            self.assertIsNotNone(shellguard.ci_wait(cmd), cmd)
            self.assertIsNotNone(self.denied(self.bash("w1", cmd)), cmd)

    def test_a_loop_elsewhere_a_piped_list_and_heredoc_bodies_are_not_waits(self):
        for cmd in ("for i in 1 2; do echo $i; done; gh run view 1 --json conclusion",
                    "while read f; do echo $f; done < files.txt && gh run view 7 --json conclusion",
                    "gh run list --json databaseId -q .[].databaseId | while read r; do echo $r; done",
                    "cat <<EOF > notes.md\ngh run watch 1\nEOF",
                    "cat <<-'END' | wc -l\n\tgh run watch 2\n\tEND",
                    'cat > x.md <<"E"\nwhile true; do gh run view 1; done\nE\nls'):
            self.assertIsNone(shellguard.ci_wait(cmd), cmd)
            self.assertIsNone(self.denied(self.bash("w1", cmd)), cmd)
        # bash expands $(...) and backticks in an UNQUOTED heredoc body: those are code
        for cmd in ("cat <<EOF\n$(gh run watch 1)\nEOF", "cat <<EOF > x\nnote `gh run watch 2` done\nEOF"):
            self.assertIsNotNone(shellguard.ci_wait(cmd), cmd)
        for cmd in ("cat <<'EOF'\n$(gh run watch 1)\nEOF", 'cat <<"EOF"\n`gh run watch 1`\nEOF',
                    "cat <<\\EOF\n$(gh run watch 1)\nEOF"):
            self.assertIsNone(shellguard.ci_wait(cmd), cmd)
        # what follows a heredoc is still looked at
        self.assertIsNotNone(shellguard.ci_wait("cat <<EOF\nx\nEOF\ngh run watch 3"))


# --------------------------------------------------------------------------- B: background calls

class BackgroundRewriteTests(BgHookEnv):
    @posix_only("the wrapper runs the command with bash -c / sh -c")
    def test_background_call_is_wrapped_and_runs_recorded(self):
        out = self.bash("w1", "exit 7", background=True)
        cmd = self.rewritten(out)
        words = shlex.split(cmd)
        self.assertEqual(words[words.index("bg"):], ["bg", "--job", "J", "--key", "w1", "--as", self.name, "--", "exit 7"])
        self.assertIn("runs under `swarm bg`", self.context(out))
        self.assertTrue(out["hookSpecificOutput"]["updatedInput"]["run_in_background"])
        rc, _, err = self.cli(*words[1:])                       # what the host then runs
        self.assertEqual(rc, 7, err)
        with self.board() as b:
            (r,) = b.bg_commands("J")
        self.assertEqual((r.agent_key, r.agent_name, r.command, r.outcome, r.exit_code),
                         ("w1", self.name, "exit 7", "exited", 7))

    def test_foreground_orchestrator_and_already_wrapped_are_left_alone(self):
        self.assertIsNone(self.rewritten(self.bash("w1", "sleep 1")))
        out = self.hook("turn", agent_id=None, session="sess-1", tool_name="Bash",
                        tool_input={"command": "sleep 1", "run_in_background": True},
                        transcript_path=self.main_transcript()) or {}
        self.assertIsNone(self.rewritten(out))


    def test_only_our_own_rewrite_is_not_wrapped_again(self):
        cmd = self.rewritten(self.bash("w1", "sleep 3600; swarm bg list", background=True))
        self.assertIsNotNone(cmd)                                  # merely containing `swarm bg`: wrapped
        self.assertEqual(shlex.split(cmd)[-1], "sleep 3600; swarm bg list")
        self.assertIsNone(self.rewritten(self.bash("w1", cmd, background=True)))   # our rewrite: as is
        other = bg.wrap_command("sleep 1", "J", "someone-else", self.name, self.cfg)
        self.assertIsNotNone(self.rewritten(self.bash("w1", other, background=True)))

    def test_notice_says_aliases_and_functions_are_not_available(self):
        self.assertIn("aliases and functions are not available", self.context(self.bash("w1", "ll", background=True)))

    def test_join_rewrite_and_wrap_compose(self):
        cmd = self.rewritten(self.bash("w1", "/x/bin/swarm join --job J --key other", background=True))
        inner = shlex.split(cmd)[-1]
        self.assertEqual(inner, "/x/bin/swarm join --job J --key w1")

    def test_ci_refusal_beats_the_wrap(self):
        out = self.bash("w1", "gh run watch 3", background=True)
        self.assertIsNotNone(self.denied(out))
        self.assertIsNone(self.rewritten(out))

    def test_stop_spawns_a_delayed_reap_for_its_own_running_commands(self):
        with mock.patch.object(bg, "spawn_reaper") as spawn:
            self.hook("stop", agent_id="w1")
            spawn.assert_not_called()                           # nothing running: nothing spawned
        self.spawn_worker("w2")
        with self.board() as b:
            b.bg_start("J", "w2", self.member("w2").name, "sleep 60", host=bg.this_host(), boot=bg.boot_key(),
                       pid=1, pgid=1, proc_start=1, tag="t")
            b.bg_start("J", "w2", self.member("w2").name, "sleep 60", host="elsewhere", boot="x/1",
                       pid=1, pgid=1, proc_start=1, tag="t")
        with mock.patch.object(bg, "spawn_reaper") as spawn:
            self.hook("stop", agent_id="w2")
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1:], ("J", "w2", bg.AGENT_GRACE))

    def test_spawn_reaper_argv(self):
        with mock.patch("subprocess.Popen") as popen:
            bg.spawn_reaper(self.cfg, "J", "w2", 30)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("bg"):], ["bg", "reap", "--job", "J", "--agent", "w2", "--delay", "30"])
        self.assertIn(str(self.config), argv)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


# --------------------------------------------------------------------------- C/D: list, reap, status, doctor

class OrphanCliTests(BgHookEnv):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(bg, "spawn_reaper")   # the stop hook's detached reap: not here
        self.spawned = patch.start()
        self.addCleanup(patch.stop)

    def add(self, key="w1", host=None, **kw):
        with self.board() as b:
            return b.bg_start("J", key, self.member(key).name if self.member(key) else self.name, "make test",
                              host=host or bg.this_host(), boot=kw.pop("boot", bg.boot_key()),
                              pid=kw.pop("pid", 1), pgid=kw.pop("pgid", 1), proc_start=kw.pop("proc_start", 1),
                              tag=kw.pop("tag", "t"))

    def test_list_status_and_watch_show_running_and_orphaned(self):
        self.add()
        rc, out, _ = self.cli("bg", "list")
        self.assertEqual(rc, 0)
        self.assertIn("running", out)
        self.assertIn("make test", out)
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("background 1 running, 0 orphaned\n", out)
        self.hook("stop", agent_id="w1")
        _, out, _ = self.cli("bg", "list", "--orphans")
        self.assertIn("orphaned", out)
        _, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("background 1 running, 1 orphaned (swarm bg list --orphans; swarm bg reap)", out)
        with self.board() as b:
            frame = cli._watch_frame(b, "J", 10, False, False, {"recent_minutes": 60, "offset": 0, "wrap": False})
        self.assertTrue(any(line.startswith("background 1 running, 1 orphaned") for line in frame))

    def test_reap_dry_run_and_foreign_rows(self):
        self.add(host="another-box")
        self.hook("stop", agent_id="w1")
        rc, out, _ = self.cli("bg", "reap", "--job", "J", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("no orphaned background commands on this host", out)
        with self.board() as b:
            self.assertEqual(len(b.bg_commands("J", running=True)), 1)

    def test_doctor_warns_about_this_hosts_orphans(self):
        from swarm import bootstrap
        self.assertEqual([c.ok for c in bootstrap._bg_orphans_check(self.cfg)], [True])
        self.add()
        self.hook("stop", agent_id="w1")
        [check] = bootstrap._bg_orphans_check(self.cfg)
        self.assertIsNone(check.ok)
        self.assertIn("1 still running", check.detail)
        self.assertIn("swarm bg reap", check.fix)

    def test_auto_close_sweep_spawns_a_reap_for_closed_jobs_with_local_commands(self):
        from swarm.board import AutoClosed
        self.add()
        with self.board() as b, mock.patch.object(bg, "spawn_reaper") as spawn, \
                mock.patch.object(type(b), "sweep_auto_close", return_value=[AutoClosed("J", "done")]):
            cli.sweep_jobs(b, self.cfg)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1:], ("J",))


@__import__("unittest").skipUnless(LINUX, "needs Linux /proc and pidfd")
class DeactivateReapTests(BgHookEnv):
    def test_deactivate_stops_the_jobs_running_commands(self):
        tag = "deact-tag"
        p = subprocess.Popen(["sleep", "60"], env={**os.environ, bg.TAG_ENV: tag}, process_group=0)
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait(5)))
        wait_until(lambda: bg.proc_start(p.pid) is not None)
        with self.board() as b:
            bid = b.bg_start("J", "w1", self.name, "sleep 60", host=bg.this_host(), boot=bg.boot_key(),
                             pid=p.pid, pgid=p.pid, proc_start=bg.proc_start(p.pid), tag=tag)
        rc, out, err = self.cli("deactivate", "--job", "J", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"background command reaped #{bid}", out)
        self.assertEqual(p.wait(5), -signal.SIGTERM)
        with self.board() as b:
            self.assertEqual(b.bg_commands("J")[0].outcome, "reaped")


try:
    from test_supervise_command import SuperviseEnv  # noqa: E402
except __import__("unittest").SkipTest:   # Windows: no supervisor
    SuperviseEnv = __import__("unittest").TestCase


@__import__("unittest").skipIf(sys.platform == "win32", "swarm supervise is POSIX only")
class SupervisorPassTests(SuperviseEnv):
    def test_the_pass_reaps_this_hosts_orphans_and_the_dry_run_only_says_so(self):
        calls = []
        with mock.patch.object(bg, "reap_orphans", side_effect=lambda *a, **k: calls.append(k) or []):
            self.assertEqual(self.supervise(), 0)
            self.assertEqual(self.supervise(dry_run=True), 0)
        self.assertEqual([c.get("dry_run", False) for c in calls], [False, True])
        self.assertEqual([c["job"] for c in calls], [None, None])
