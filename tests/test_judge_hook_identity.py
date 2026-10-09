"""A hook-registered judge records its verdict under its OWN hook identity: no `join`, no `--as`.

Root cause of the field failure (Claude Code 2.1.28x): the judge's prompt said `[swarm title: Judge]`,
which no hook read, so it was seated as a plain worker; a worker holds a fast-path lease and
bin/swarm-hook then exits in shell before Python runs, so `swarm join --judge --key judge` was never
rewritten to the agent's own key. It joined a second identity, and `--as <that name>` is
impersonation. Now the title seats the judge, join/verdict calls always take the Python path, and
`swarm verdict` is rewritten to run as the caller."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_goals import GoalEnv  # noqa: E402  (sets sys.path)
from support import ROOT  # noqa: E402

from swarm import hooks, roles  # noqa: E402

JOIN = "/home/u/.claude/plugins/cache/swarm/swarm/0.2.1/bin/swarm join --job J --judge --key judge"
VERDICT = '/x/bin/swarm verdict --job J met "all checks pass"'


class JudgeIdentityTests(GoalEnv):
    def setUp(self):
        super().setUp()
        self.activate_goal()

    def bash(self, agent, command):
        out = self.hook("turn", agent_id=agent, session="sess-1", tool_name="Bash",
                        transcript_path=self.main_transcript(), tool_input={"command": command})
        return ((out or {}).get("hookSpecificOutput", {}).get("updatedInput") or {}).get("command")

    def spawn_titled_judge(self, agent="judge-1"):
        self.spawn(agent, "[swarm job: J]\n[swarm title: Judge]\nDecide whether the goal is met.")

    def run_rewritten(self, command):
        """What the host then runs: the rewritten swarm call, through the real CLI."""
        words = shlex.split(command)
        self.assertEqual(words[1], "verdict")
        return self.cli(*words[1:])

    # -- the judge needs no join and no --as ------------------------------------------------
    def test_title_judge_records_a_verdict_with_no_join_and_no_as(self):
        self.spawn_titled_judge()
        judge = self.member("judge-1")
        self.assertEqual(judge.role, "judge")
        self.assertEqual(self.job().judge, judge.name)
        command = self.bash("judge-1", VERDICT)
        self.assertIn(f"--as {shlex.quote(judge.name)}", command)
        rc, out, err = self.run_rewritten(command)
        self.assertEqual(rc, 0, err)
        s = self.job()
        self.assertEqual((s.verdict, s.verdict_by), ("met", judge.name))

    def test_role_tag_judge_records_a_verdict_with_no_join_and_no_as(self):
        self.spawn_judge()
        rc, _, err = self.run_rewritten(self.bash("judge-1", VERDICT))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.job().verdict, "met")

    def test_not_met_without_as_and_a_wrong_as_is_replaced(self):
        self.spawn_titled_judge()
        name = self.member("judge-1").name
        cmd = self.bash("judge-1", '/x/bin/swarm verdict --job J --as "Birch Barlow" not_met '
                                   '--reason "x" --next "fix y"')
        self.assertIn(shlex.quote(name), cmd)
        self.assertNotIn("Birch Barlow", cmd)
        rc, _, err = self.run_rewritten(cmd)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.job().verdict, "not_met")

    def test_own_name_and_other_job_and_non_verdicts_are_left_alone(self):
        self.spawn_titled_judge()
        name = self.member("judge-1").name
        for command in (f"/x/bin/swarm verdict --job J --as {shlex.quote(name)} met ok",
                        "/x/bin/swarm verdict --job OTHER met ok",
                        "echo swarm verdict --job J met ok",
                        "swarm verdict --job J --as $WHO met ok",
                        "swarm status --job J"):
            with self.subTest(command):
                self.assertIsNone(self.bash("judge-1", command))

    def test_a_worker_verdict_is_still_refused(self):
        self.spawn_titled_judge()
        self.spawn_worker("w1")
        rc, _, err = self.run_rewritten(self.bash("w1", VERDICT))
        self.assertEqual(rc, 1)
        self.assertIn("not the judge", err)
        self.assertIsNone(self.job().verdict)

    # -- the title seats the judge, with the handover rules -----------------------------------
    def test_title_judge_does_not_displace_a_live_judge(self):
        self.spawn_judge("judge-1")
        first = self.member("judge-1").name
        self.spawn_titled_judge("judge-2")
        self.assertEqual(self.job().judge, first)
        self.assertNotEqual(self.member("judge-2").role, "judge")
        rc, _, err = self.run_rewritten(self.bash("judge-2", VERDICT))
        self.assertEqual(rc, 1)
        self.assertIn("not the judge", err)

    def test_title_judge_takes_over_from_a_dead_judge(self):
        self.spawn_judge("judge-1")
        self.h.backdate_agent("judge-1", joined_at=3 * 3600, last_seen=2 * 3600)
        self.spawn_titled_judge("judge-2")
        second = self.member("judge-2")
        self.assertEqual(self.job().judge, second.name)
        rc, _, err = self.run_rewritten(self.bash("judge-2", VERDICT))
        self.assertEqual(rc, 0, err)

    def test_title_tag_parsing(self):
        self.assertEqual(roles.from_prompt("[swarm job: J]\n[swarm title: Judge]"), "judge")
        self.assertEqual(roles.from_prompt("[swarm title:  judge ]"), "judge")
        self.assertIsNone(roles.from_prompt("[swarm title: Judge assistant]"))
        self.assertEqual(roles.from_prompt("[swarm title: Judge]\n[swarm role: engineer]"), "engineer")

    # -- the observed join argv is rewritten ---------------------------------------------------
    def test_observed_join_argv_is_rewritten_for_a_judge_and_a_worker(self):
        self.spawn_titled_judge()
        self.spawn_worker("w1")
        for agent in ("judge-1", "w1"):
            with self.subTest(agent):
                self.assertEqual(self.bash(agent, JOIN), JOIN.replace("--key judge", f"--key {agent}"))

    def test_the_shell_fast_path_never_skips_join_or_verdict(self):
        """bin/swarm-hook exits before Python on a live lease; a swarm join/verdict must not."""
        if not sys.platform.startswith("linux"):
            self.skipTest("the shell fast path uses Linux /proc/uptime")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td); hd = home / ".local/share/swarm/host"; hd.mkdir(parents=True, mode=0o700)
            fp = hd / "fastpath"; fp.mkdir(mode=0o700)
            cfg = home / "config.toml"; cfg.write_text("[hook]\nhook_min_interval_s=15\n")
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            cache = hd / "hook-config-claude"
            cache.write_text(f"{cfg}\n{ROOT}\n{fp}\n{home}/active\n1\nuptime-v1\n{boot}\n")
            os.utime(cache, ns=(cfg.stat().st_atime_ns, cfg.stat().st_mtime_ns))
            (fp / "live").write_text("9999999999\n")
            (fp / "board-J").write_text("generation-1\n")
            (fp / "agent-a").write_text("9999999999\nJ\ngeneration-1\n")
            env = dict(os.environ, HOME=td, SWARM_CONFIG=str(cfg), PATH="/nonexistent")

            def run(command):
                payload = json.dumps({"session_id": "s", "agent_id": "a", "tool_name": "Bash",
                                      "tool_input": {"command": command}})
                return subprocess.run([str(ROOT / "bin/swarm-hook"), "--host", "claude", "turn"],
                                      input=payload, text=True, capture_output=True, env=env)
            quiet = run("ls -l")
            self.assertEqual((quiet.returncode, quiet.stdout, quiet.stderr), (0, "", ""))   # the lease skips
            for command in (JOIN, VERDICT, "swarm   join --key=x --job J"):
                with self.subTest(command):
                    self.assertNotEqual(run(command).stderr, "", "the shell fast path skipped the hook")

    # -- humans ---------------------------------------------------------------------------------
    def test_cli_without_hook_context_still_needs_an_identity(self):
        self.spawn_titled_judge()
        rc, _, err = self.cli("verdict", "--job", "J", "met", "ok")
        self.assertEqual(rc, 2)
        self.assertIn("--as NAME", err)
        self.assertIsNone(self.job().verdict)

    def test_join_without_key_points_judges_at_the_no_join_path(self):
        rc, _, err = self.cli("join", "--job", "J", "--judge")
        self.assertEqual(rc, 2)
        self.assertIn("--key is required", err)
        self.assertIn("swarm verdict --job J met", err)
        self.assertIn("does NOT join", err)

    def test_judge_brief_shows_a_verdict_command_without_as(self):
        out_start, turn = self.spawn_judge("judge-3")
        text = json.dumps(turn)
        self.assertIn("verdict --job 'J' --artifact REF met", text)
        self.assertNotIn("verdict --job 'J' --as", text)

    def test_judge_brief_offers_details_only_with_the_cli_it_names(self):
        # The brief names this plugin's own bin/swarm (hooks._bin), so it may offer --details
        # exactly when that same tree's CLI has it. A 0.2.2 hook shows a 0.2.2 brief (no
        # --details) and a 0.2.2 bin/swarm; a judge told otherwise was told by its spawn prompt.
        from swarm import paths
        brief = hooks._judge_instructions("Lisa Simpson", "J", "the goal", self.cfg, 200)
        self.assertIn(shlex.quote(str(paths.agent_bin())), brief)
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            __import__("swarm.cli").cli.main(["verdict", "--help"])
        out = out.getvalue()
        self.assertEqual("--details" in brief, "--details" in out)
        self.assertIn("--details", brief)


if __name__ == "__main__":
    unittest.main()
