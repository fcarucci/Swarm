"""A trusted Codex wrapper runs outside the local jail, with normal drive accounting."""
import json
import os
import shlex
import sys
from unittest import mock

from test_drive_exec import ExecEnv
from swarm import hooks
from swarm.supervisor import launch, launchhome, runner


class WrapperTests(ExecEnv):
    def wrapper_run(self, command_type, status):
        script = self.tmp / "wrapper.py"
        capture = self.tmp / "capture.json"
        script.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            f"Path({str(capture)!r}).write_text(json.dumps([sys.argv[1:], sys.stdin.read()]))\n"
            "print(json.dumps({'type': 'thread.started', 'thread_id': 'remote-thread'}), flush=True)\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': "
            "{'input_tokens': 500000, 'output_tokens': 250000}}), flush=True)\n"
            f"sys.exit({status})\n")
        command = [sys.executable, str(script), "--profile", "engineering"]
        self.cfg["harness"] = {"codex": {"command": shlex.join(command) if command_type == "string" else command}}
        self.cfg["drive"] = {"codex_usd_per_mtok": 2}
        self.get_launch(harness="codex", model="gpt-6-astra", role="judge")
        run = self.go(self.act(), prompt=None)
        brief = run["stdin"]
        self.assertIn('--key "${CODEX_SESSION_ID:-$CODEX_THREAD_ID}"', brief)
        joined = []
        set_agent = type(self.b).set_restart_agent

        def remote_join(board, rid, key):
            set_agent(board, rid, key)
            if key == "remote-thread" and not joined:
                rc, name, err = self.cli("join", "--job", "J", "--key", key,
                                         "--resume-of", run["resume_of"])
                joined.append((rc, name.strip(), err))
                self.b.post("J", name.strip(), "remote agent joined")
                self.assertTrue(self.b.record_verdict("J", name.strip(), "met", "wrapper judge works"))

        with mock.patch.object(runner, "scope_available", return_value=False), \
             mock.patch.object(launchhome, "launch", side_effect=launchhome.LaunchIsolationError("no jail")), \
             mock.patch.object(type(self.b), "set_restart_agent", remote_join):
            outcome = runner.execute(self.cfg, run, poll=0.01, board_every=0.01)
        self.assertEqual(outcome, "completed" if status == 0 else "failed")
        argv, received = json.loads(capture.read_text())
        self.assertEqual(received, brief)
        self.assertIn("brief.md", received)
        self.assertEqual(argv[:2], ["--profile", "engineering"])
        self.assertIn(("-m", "gpt-6-astra"), list(zip(argv, argv[1:])))
        self.assertNotIn("launch_home", run)
        self.assertEqual(self.row_of(run).outcome, outcome)
        self.assertEqual(self.row_of(run).cost_usd, 1.5)
        self.assertEqual(self.reload().intents[0].charged_usd, 1.5)
        agent = next(a for a in self.b.agents("J") if a.agent_key == "remote-thread")
        self.assertEqual(agent.resume_of, run["resume_of"])
        self.assertEqual(agent.role, "judge")
        self.assertEqual(joined, [(0, run["name"], "")])
        self.assertEqual(self.b.job_status("J").verdict, "met")

    def test_argv_wrapper_stdin_model_and_cost(self):
        self.wrapper_run("argv", 0)

    def test_string_wrapper_nonzero_exit_and_cost(self):
        self.wrapper_run("string", 7)

    def test_wrapper_exit_125_is_failure_not_jail_refusal(self):
        self.wrapper_run("argv", 125)

    def test_unconfigured_codex_still_requires_jail(self):
        run = {"harness": "codex", "launch_id": "L", "argv": ["codex", "exec"],
               "job": "J", "name": "a", "cwd": str(self.work)}
        with mock.patch.object(launchhome, "launch", side_effect=launchhome.LaunchIsolationError("no jail")):
            self.assertEqual(runner._launch_argv(self.cfg, run)[1], "no jail")

    def test_codex_wrapper_does_not_exempt_claude(self):
        self.cfg["harness"] = {"codex": {"command": [sys.executable]}}
        run = {"harness": "claude", "launch_id": "L", "argv": ["claude", "-p"],
               "job": "J", "name": "a", "cwd": str(self.work)}
        with mock.patch.object(launchhome, "launch", side_effect=launchhome.LaunchIsolationError("no jail")):
            self.assertEqual(runner._launch_argv(self.cfg, run)[1], "no jail")

    def test_no_wrapper_or_binary_refuses_before_claim(self):
        self.get_launch(harness="codex")
        with mock.patch.object(launch.shutil, "which", return_value=None):
            self.assertIsNone(self.go(self.act()))
        self.assertEqual(self.b.restarts(job="J"), [])
        self.assertEqual(self.led.intents, [])

    def test_remote_root_hooks_use_explicit_thread_join_without_driver_markers(self):
        key = "11111111-2222-3333-4444-555555555555"
        self.cfg["harness"] = {"codex": {"command": [sys.executable]}}
        self.get_launch(harness="codex", role="judge")
        run = self.go(self.act())
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": key, "SWARM_HOST": "codex"}), \
             mock.patch("os.getcwd", return_value=str(self.work)):
            rc, name, err = self.cli("join", "--job", "J", "--key", key, "--resume-of", run["resume_of"])
        self.assertEqual((rc, err), (0, ""))
        # The remote join may beat the driver's stdout pump; a later bind must retain its identity.
        self.b.set_restart_agent(run["restart_id"], key)
        self.assertEqual(next(a for a in self.b.agents("J") if a.agent_key == key).resume_of, run["resume_of"])
        self.assertEqual(self.b.job_status("J").judge, name.strip())
        peer = self.b.allocate_name("peer", "J", "engineer")
        self.b.post("J", peer, "remote board context")
        with mock.patch.object(hooks, "_resume_binding", return_value=None), \
             mock.patch.object(hooks, "_session_markers", return_value=({}, {})):
            out = self.hook("turn", agent_id=None, session=key, host="codex", cwd=str(self.work),
                            tool_name="Bash", tool_input={"command": "git status"})
            self.assertEqual(next(a for a in self.b.agents("J") if a.agent_key == key).tool_calls, 1)
            self.assertIn("remote board context", json.dumps(out))
            out = self.hook("session-stop", agent_id=None, session=key, host="codex", cwd=str(self.work))
            self.assertEqual(out["decision"], "block")
            self.assertIn("must record swarm verdict", out["reason"])
            self.hook("session-end", agent_id=None, session=key, host="codex", cwd=str(self.work))
            self.assertIsNotNone(next(a for a in self.b.agents("J") if a.agent_key == key).ended_at)
            self.assertIsNone(next(a for a in self.b.agents("J") if a.agent_key == "peer").ended_at)

    def test_wrapper_join_refuses_wrong_predecessor(self):
        rc, _, err = self.cli("join", "--job", "J", "--key", "other", "--resume-of", "missing")
        self.assertEqual(rc, 1)
        self.assertIn("no open Codex launch", err)
        self.assertEqual(self.b.agents("J"), [])

    def test_wrapper_join_cannot_claim_a_local_supervisor_restart(self):
        self.b.allocate_name("local", "J", "engineer")
        self.b.close_agent("local", "stuck:dead")
        self.b.record_restart("J", "local", "local", "stuck:dead", "codex", 1)
        rc, _, err = self.cli("join", "--job", "J", "--key", "other", "--resume-of", "local")
        self.assertEqual(rc, 1)
        self.assertIn("no open Codex launch", err)
        self.assertIsNone(self.b.active_agent_name("other"))
