"""Several swarm jobs in one Claude session: each subagent joins the job named by the
`[swarm job: <job>]` tag in its spawn prompt.

What Claude Code (2.1.28x) gives the hooks, verified with a logging hook (see docs/REFERENCE.md):
SubagentStart carries session_id, agent_id, agent_type and the MAIN session's transcript_path,
but no prompt; the subagent's own transcript, <transcript_path minus .jsonl>/subagents/
agent-<agent_id>.jsonl, is written only after SubagentStart and holds the spawn prompt as its
first (user) line from the subagent's first PreToolUse on. These tests lay out that file."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

from swarm import hooks as swarm_hooks  # noqa: E402


class RoutingEnv(Env):
    def setUp(self):
        super().setUp()
        self.projects = self.tmp / "projects"
        self.route_log = Path(os.environ["HOME"]) / ".local/share/swarm/host/routing.log"   # host-only

    def activate(self, job: str, *extra, session: str | None = "sess-1"):
        args = ["activate", "--job", job, *extra] + (["--session", session] if session else [])
        rc, out, _ = self.cli(*args)
        self.assertEqual(rc, 0)
        return out

    def main_transcript(self, session: str = "sess-1") -> str:
        return str(self.projects / f"{session}.jsonl")

    def write_prompt(self, agent_id: str, prompt, session: str = "sess-1") -> Path:
        """The subagent transcript Claude Code writes after SubagentStart: first line = prompt."""
        path = self.projects / session / "subagents" / f"agent-{agent_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        first = {"parentUuid": None, "isSidechain": True, "agentId": agent_id, "type": "user",
                 "message": {"role": "user", "content": prompt}}
        later = {"type": "assistant", "message": {"role": "assistant", "content": "[swarm job: WRONG]"}}
        path.write_text(json.dumps(first) + "\n" + json.dumps(later) + "\n")
        return path

    def start(self, agent_id: str, session: str = "sess-1"):
        return self.hook("start", agent_id=agent_id, session=session, agent_type="general-purpose",
                         transcript_path=self.main_transcript(session))

    def turn(self, agent_id: str, session: str = "sess-1", tool: str = "Bash"):
        return self.hook("turn", agent_id=agent_id, session=session, tool_name=tool,
                         transcript_path=self.main_transcript(session))

    def spawn(self, agent_id: str, prompt, session: str = "sess-1"):
        """SubagentStart (no transcript yet), then the transcript appears, then the first tool call.
        Returns (start output, first turn output)."""
        out_start = self.start(agent_id, session)
        self.write_prompt(agent_id, prompt, session)
        return out_start, self.turn(agent_id, session)

    def member(self, key: str):
        with self.board() as b:
            for job in ("A", "B", "C", "J"):
                hit = next((a for a in b.agents(job) if a.agent_key == key and a.ended_at is None), None)
                if hit:
                    return hit
        return None

    def job_of(self, key: str) -> str | None:
        with self.board() as b:
            return next((job for job in ("A", "B", "C", "J")
                         for a in b.agents(job) if a.agent_key == key and a.ended_at is None), None)

    def log_lines(self) -> list[str]:
        return self.route_log.read_text().splitlines() if self.route_log.exists() else []


class ActivateTagTests(RoutingEnv):
    def test_custom_role_arrives_after_blind_start_and_survives_resume(self):
        self.activate("J")
        self.spawn("pm", "[swarm job: J]\n[swarm role: product_manager]\nDefine acceptance criteria.")
        self.assertEqual(self.member("pm").role, "product_manager")
        name = self.member("pm").name
        self.hook("stop", agent_id="pm")
        self.start("pm")
        self.assertEqual((self.member("pm").name, self.member("pm").role), (name, "product_manager"))
        rc, out, err = self.cli("status", "--job", "J", "--no-color")
        self.assertEqual(rc, 0, err)
        self.assertIn("product_manager", out)

    def test_custom_role_on_goal_job_cannot_verdict_but_can_write(self):
        self.activate("J", "--goal", "Ship the requested product")
        self.spawn("qa", "[swarm job: J]\n[swarm role: qa]\nWrite acceptance tests.")
        self.assertEqual(self.member("qa").role, "qa")
        out = self.turn("qa", tool="Write")
        self.assertNotEqual((out or {}).get("hookSpecificOutput", {}).get("permissionDecision"), "deny")
        rc, _, _ = self.cli("verdict", "--job", "J", "--as", self.member("qa").name, "met", "tests pass")
        self.assertNotEqual(rc, 0)

    def test_custom_role_routes_to_correct_job_when_several_are_active(self):
        self.activate("A")
        self.activate("B")
        self.spawn("lead", "[swarm job: B]\n[swarm role: engineering_lead]\nPlan the work.")
        self.assertEqual((self.job_of("lead"), self.member("lead").role), ("B", "engineering_lead"))

    def test_activate_prints_the_tag_line(self):
        out = self.activate("A")
        self.assertIn("\n[swarm job: A]\n", out)
        self.assertIn("put this line in every subagent prompt", out)

    def test_activate_binds_to_the_calling_claude_session(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env"}):
            self.activate("A", session=None)
        self.assertEqual(json.loads((self.markers / "A.json").read_text())["session_id"], "sess-env")
        with self.board() as b:
            self.assertEqual(b.job_status("A").session_id, "sess-env")

    def test_explicit_session_wins_over_the_environment(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-env"}):
            self.activate("A", session="given")
        self.assertEqual(json.loads((self.markers / "A.json").read_text())["session_id"], "given")


class TwoJobsOneSessionTests(RoutingEnv):
    def setUp(self):
        super().setUp()
        self.activate("A")
        self.activate("B")

    def test_tagged_subagents_join_their_own_job(self):
        s1, t1 = self.spawn("a1", "[swarm job: A]\nYou own the database.")
        s2, t2 = self.spawn("b1", "You own the network.\n[swarm job: B]\n")
        s3, t3 = self.spawn("a2", "[swarm job: A]\nYou test.")
        self.assertIsNone(s1)  # the prompt is not readable at SubagentStart: routed on the first call
        self.assertIn('working on job "A"', self.context(t1))
        self.assertIn('working on job "B"', self.context(t2))
        self.assertIn('working on job "A"', self.context(t3))
        self.assertEqual([self.job_of(k) for k in ("a1", "b1", "a2")], ["A", "B", "A"])
        a1 = self.member("a1")
        self.assertEqual((a1.status, a1.current_tool, a1.tool_calls), ("running", "Bash", 1))
        # the boards stay apart: a1 sees A's posts only, b1 B's only
        self.cli("post", "--job", "B", "--as", "Someone", "network news")
        self.cli("post", "--job", "A", "--as", "Someone", "database news")
        ctx_a = self.context(self.turn("a1"))
        self.assertIn("database news", ctx_a)
        self.assertNotIn("network news", ctx_a)
        ctx_b = self.context(self.turn("b1"))
        self.assertIn("network news", ctx_b)
        self.assertNotIn("database news", ctx_b)
        self.assertIn(self.member("a2").name, ctx_a)  # roster of its own job
        self.assertNotIn(self.member("b1").name, ctx_a)
        self.hook("done", agent_id="b1")
        self.hook("stop", agent_id="b1")
        self.assertIsNone(self.job_of("b1"))

    def test_untagged_subagent_joins_neither_and_it_is_logged_once(self):
        self.assertEqual(self.spawn("x", "Just look around."), (None, None))
        self.assertIsNone(self.turn("x"))
        self.assertIsNone(self.job_of("x"))
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("x: not joining: 2 swarm jobs are active in this session (A, B) and the "
                      "prompt has no [swarm job: <job>] tag", lines[0])

    def test_tag_naming_an_inactive_job_joins_nothing_and_says_so(self):
        _, t = self.spawn("x", "[swarm job: C]\nwork")
        self.assertIn('tagged [swarm job: C], but "C" is not an active swarm job of this Claude '
                      'session', self.context(t))
        self.assertIsNone(self.turn("x"))  # said once
        self.assertIsNone(self.job_of("x"))
        self.assertEqual(len(self.log_lines()), 1)

    def test_tag_naming_another_sessions_job_joins_nothing(self):
        self.activate("C", session="sess-other")
        _, t = self.spawn("x", "[swarm job: C]\nwork")
        self.assertIn('"C" is not an active swarm job of this Claude session', self.context(t))
        self.assertIsNone(self.job_of("x"))
        with self.board() as b:
            self.assertEqual(b.agents("C"), [])

    def test_the_transcript_is_read_once(self):
        with mock.patch.object(swarm_hooks, "_spawn_prompt", wraps=swarm_hooks._spawn_prompt) as read:
            self.spawn("a1", "[swarm job: A]\nwork")
            self.spawn("x", "no tag here")
            for _ in range(3):
                self.turn("a1")
                self.turn("x")
                self.hook("done", agent_id="a1")
        # SubagentStart looks (the file is not there yet), the first call reads: per agent
        self.assertEqual(read.call_count, 4)
        self.assertEqual(self.job_of("a1"), "A")

    def test_parallel_first_calls_decide_once(self):
        # Two tool calls in one message fire two PreToolUse hooks at once: both may read the
        # route while it is still pending. Only one may act on it.
        from swarm.board import Route, backend_class
        self.start("x")
        self.start("a1")
        self.write_prompt("x", "no tag")
        self.write_prompt("a1", "[swarm job: A]\n")
        stale = {"x": Route("pending", None, "sess-1", None), "a1": Route("pending", None, "sess-1", None)}
        with mock.patch.object(backend_class(self.cfg), "route", lambda self, key: stale[key]):
            outs = [self.turn("x"), self.turn("x"), self.turn("a1"), self.turn("a1")]
        self.assertEqual(len(self.log_lines()), 1)
        self.assertEqual(outs[:2], [None, None])
        self.assertIn("You are **", self.context(outs[2]))
        self.assertIsNone(outs[3])  # the loser says nothing; the next call is a normal member turn
        self.assertEqual(self.job_of("a1"), "A")

    def test_resumed_member_gets_back_into_its_own_job(self):
        self.spawn("b1", "[swarm job: B]\nwork")
        name = self.member("b1").name
        self.hook("stop", agent_id="b1")
        self.cli("post", "--job", "B", "--as", "Someone", "while you were away")
        out = self.start("b1")  # resumed through SendMessage: the transcript exists already
        self.assertIn(f'You are **{name}**, a member of the swarm working on job "B"', self.context(out))
        self.assertIn("while you were away", self.context(out))

    def test_resumed_member_rejoins_on_a_tool_call_too(self):
        self.spawn("b1", "[swarm job: B]\nwork")
        self.hook("stop", agent_id="b1")
        out = self.turn("b1")
        self.assertIn('working on job "B"', self.context(out))
        self.assertEqual(self.job_of("b1"), "B")

    def test_other_sessions_are_ignored(self):
        self.assertIsNone(self.start("y", session="sess-2"))
        self.write_prompt("y", "[swarm job: A]\n", session="sess-2")
        self.assertIsNone(self.turn("y", session="sess-2"))
        self.assertIsNone(self.job_of("y"))

    def test_tag_as_text_blocks(self):
        _, t = self.spawn("a1", [{"type": "text", "text": "intro"},
                                 {"type": "text", "text": "[swarm job: B]\ndo it"}])
        self.assertIn('working on job "B"', self.context(t))

    def test_prompt_in_the_payload_routes_at_start(self):
        # Not sent by Claude Code 2.1.28x; taken if a later version adds it.
        out = self.hook("start", agent_id="p1", prompt="[swarm job: B]\nwork",
                        transcript_path=self.main_transcript())
        self.assertIn('working on job "B"', self.context(out))

    def test_adopt_running_with_two_jobs_follows_the_tag(self):
        self.activate("C", "--adopt-running")
        self.write_prompt("old", "[swarm job: C]\nlong-running work")
        out = self.turn("old")
        self.assertIn('working on job "C"', self.context(out))
        self.write_prompt("old2", "[swarm job: A]\nlong-running work")
        self.assertIsNone(self.turn("old2"))  # A was not activated with --adopt-running
        self.assertIsNone(self.job_of("old2"))


class OneJobTests(RoutingEnv):
    def test_untagged_subagent_joins_the_only_job_at_start(self):
        self.activate("J")
        out = self.start("u1")
        self.assertIn('working on job "J"', self.context(out))
        self.write_prompt("u1", "no tag")
        self.assertIsNone(self.turn("u1"))
        self.assertIsNone(self.turn("u1"))
        self.assertEqual(self.job_of("u1"), "J")

    def test_matching_tag_keeps_it(self):
        self.activate("J")
        self.start("t1")
        self.write_prompt("t1", "[swarm job: J]\n")
        self.turn("t1")
        self.assertEqual(self.job_of("t1"), "J")

    def test_bad_tag_takes_it_off_the_board_at_its_first_call(self):
        self.activate("J")
        self.assertIn('working on job "J"', self.context(self.start("t1")))
        self.write_prompt("t1", "[swarm job: typo]\n")
        out = self.turn("t1")
        self.assertIn('tagged [swarm job: typo], but "typo" is not an active swarm job', self.context(out))
        self.assertIsNone(self.job_of("t1"))
        self.assertIsNone(self.turn("t1"))

    def test_unbound_marker_is_claimed_only_on_a_start(self):
        self.activate("J", session=None)
        self.write_prompt("elsewhere", "[swarm job: J]\n", session="sess-other")
        self.assertIsNone(self.turn("elsewhere", session="sess-other"))
        self.assertIsNone(json.loads((self.markers / "J.json").read_text())["session_id"])
        self.assertIn('working on job "J"', self.context(self.start("s1")))
        self.assertEqual(json.loads((self.markers / "J.json").read_text())["session_id"], "sess-1")

    def test_second_job_activated_later_gets_its_tagged_spawns(self):
        self.activate("A")
        self.spawn("a1", "[swarm job: A]\n")
        self.activate("B")
        s, t = self.spawn("b1", "[swarm job: B]\n")
        self.assertIsNone(s)
        self.assertIn('working on job "B"', self.context(t))
        self.assertEqual(self.job_of("a1"), "A")

    def test_second_job_unbound_is_claimed_by_a_tagged_spawn_of_this_session(self):
        # Activated outside Claude Code (no session id): claimed by the session whose
        # subagent names it, as long as that subagent started after activation.
        self.activate("A")
        self.spawn("a1", "[swarm job: A]\n")
        self.activate("B", session=None)
        _, t = self.spawn("b1", "[swarm job: B]\n")
        self.assertIn('working on job "B"', self.context(t))
        self.assertEqual(json.loads((self.markers / "B.json").read_text())["session_id"], "sess-1")
