"""End to end on one box, simulating two: agents work through the hooks, `swarm pause` freezes the
job, `swarm resume` (host launch faked) re-creates the agents from the transcripts stored on the
board, and each new session enrols under its old name through the hooks, as on a second host."""
from __future__ import annotations

import json
from unittest import mock

from test_hooks_cli import Env

from swarm import transcripts as T
from swarm.hosts import resume as hosts_resume

SECRET = "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wx-yz0123"
OLD_SID = "99999999-8888-4777-8666-555555555555"


def claude_transcript(who: str) -> str:
    base = {"sessionId": OLD_SID, "cwd": "/old/box/work", "isSidechain": False, "userType": "external"}
    rows = [{**base, "type": "user", "uuid": "u1", "parentUuid": None,
             "message": {"role": "user", "content": f"{who}: build it, key {SECRET}"}},
            {**base, "type": "assistant", "uuid": "a1", "parentUuid": "u1",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]}}]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


class PauseResumeE2E(Env):
    def setUp(self):
        super().setUp()
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.assertEqual(self.cli("activate", "--job", "J", "--session", "sess-1")[0], 0)
        for key in ("agent-1", "agent-2"):
            self.hook("start", agent_id=key, session="sess-1")
            self.hook("turn", agent_id=key, session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.names = {k: self.agent(k).name for k in ("agent-1", "agent-2")}
        with self.board() as b:
            b.post("J", self.names["agent-1"], "step one done")
            for key, name in self.names.items():
                b.save_transcript(T.make_row("J", key, name, "subagent", claude_transcript(name), harness="claude"))

    def test_pause_then_resume_on_another_host(self):
        rc, out, err = self.cli("pause", "--job", "J", "--reason", "moving to box b", "--wait", "0")
        self.assertEqual(rc, 0, err)
        self.assertIn("J is paused", out)
        self.assertIn("moving to box b", out)
        for name in self.names.values():
            self.assertIn(name, out)
        self.assertNotIn(SECRET, out + err)
        # status shows it; joins and posts are refused with the reason, nothing queued
        rc, out, _ = self.cli("status")
        self.assertIn("paused", out)
        rc, out, err = self.cli("join", "--job", "J", "--key", "late")
        self.assertEqual(rc, 1)
        self.assertIn("job J is paused", err)
        self.assertIn("swarm resume --job J", err)
        rc, out, err = self.cli("post", "--job", "J", "--as", self.names["agent-1"], "still here?")
        self.assertEqual(rc, 1)
        self.assertIn("paused", err)
        # hooks stay safe: a running agent is told to stop; a newcomer is refused without an error
        out = self.hook("turn", agent_id="agent-1", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn("paused", hso["permissionDecisionReason"])
        out = self.hook("start", agent_id="agent-9", session="sess-1")
        self.assertIn("paused", self.context(out))
        self.assertFalse(self.error_log.exists() and "Traceback" in self.error_log.read_text())
        self.hook("done", agent_id="agent-1", session="sess-1", tool_name="Bash")   # no-ops, no errors
        self.hook("stop", agent_id="agent-2", session="sess-1")
        # resume "on another box"
        started = []
        with mock.patch.object(hosts_resume, "start", lambda restored, env=None: started.append(restored)), \
                mock.patch("swarm.pause.ENROL_WAIT", 0):
            rc, out, err = self.cli("resume", "--job", "J", "--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(started), 2)
        for line in out.splitlines()[1:3]:
            self.assertIn("launched", line)
        self.assertNotIn(SECRET, out + err + "".join(r.stdin for r in started))
        with self.board() as b:
            self.assertEqual(b.job_status("J").status, "active")
        # each new session is the old agent again, through the hooks
        seen = {}
        for restored in started:
            sid = restored.session_id
            self.assertIsNotNone(sid)
            out = self.hook("turn", agent_id=None, session=sid, tool_name="Bash", tool_input={"command": "ls"})
            text = self.context(out)
            self.assertIn("paused and resumed", text)
            a = self.agent(sid)
            seen[a.resume_of] = (a.name, a.status)
        self.assertEqual({k: v[0] for k, v in seen.items()}, self.names)
        with self.board() as b:
            self.assertEqual({a.name for a in b.agents("J", include_departed=False)}, set(self.names.values()))
        # the old sessions (still alive on the old box) are told, correctly, to stop
        out = self.hook("turn", agent_id="agent-1", session="sess-1", tool_name="Bash", tool_input={"command": "ls"})
        self.assertIn("paused and resumed", out["hookSpecificOutput"]["permissionDecisionReason"])
        # a rerun finds nothing paused; `resume` on an open job is the old "stop waiting"
        rc, out, _ = self.cli("resume", "--job", "J")
        self.assertEqual((rc, out.strip()), (0, "J is no longer waiting"))

    def test_resume_of_an_unpaused_job_with_flags_says_so(self):
        rc, _, err = self.cli("resume", "--job", "J", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("is not paused", err)

    def test_dry_run_leaves_the_job_paused(self):
        self.cli("pause", "--job", "J", "--wait", "0")
        rc, out, err = self.cli("resume", "--job", "J", "--dry-run", "--workdir", str(self.work))
        self.assertEqual(rc, 0, err)
        self.assertIn("would resume", out)
        with self.board() as b:
            self.assertEqual(b.job_status("J").status, "paused")

    def test_pause_of_an_unknown_job_fails_cleanly(self):
        rc, _, err = self.cli("pause", "--job", "nope", "--wait", "0")
        self.assertEqual(rc, 1)
        self.assertIn("not an open job", err)


if __name__ == "__main__":
    import unittest
    unittest.main()
