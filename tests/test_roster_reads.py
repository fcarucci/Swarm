"""Hooks end to end (SWARM_TEST_BACKEND, default memory): the roster at start and its periodic sync, catch-up reads
(history, paging, late spooled posts) and the board-communication guidance."""
from __future__ import annotations

import datetime as dt
import re

from test_hooks_cli import Env  # noqa: F401  (sets sys.path)

from swarm import spool  # noqa: E402

MIN = dt.timedelta(minutes=1)


class RosterHookTests(Env):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J", "--task", "Find the latency regression")

    def start(self, key: str, role: str | None = "worker") -> str:
        return self.context(self.hook("start", agent_id=key, agent_type=role))

    def turn(self, key: str, tool: str = "Bash") -> str:
        out = self.hook("turn", agent_id=key, tool_name=tool)
        return self.context(out) if out else ""

    def backdate(self, key: str, **minutes) -> None:
        """Set each timestamp field to that many minutes ago."""
        self.h.backdate_agent(key, **{field: m * MIN.total_seconds() for field, m in minutes.items()})

    def test_start_context_has_the_roster_of_the_other_agents(self):
        ctx = self.start("a", "Explore")
        self.assertIn("[swarm roster] you are the only agent on job \"J\" so far.", ctx)
        a = self.agent("a").name
        self.turn("a", "Grep")
        ctx = self.start("b", "general-purpose")
        b = self.agent("b").name
        roster = ctx[ctx.index("[swarm roster] other"):].split("\n\n")[0].splitlines()
        self.assertEqual(roster[0], '[swarm roster] other agents on job "J" (1 active; address them with --to \'<exact name>\'):')
        self.assertEqual(roster[1], f"- {a} (Explore): running, in Grep")
        self.assertNotIn(b, "\n".join(roster))  # not yourself
        with self.board() as board:
            self.assertIsNotNone(board.sync_state("b").roster_seen)
            self.assertIsNotNone(board.sync_state("b").roster_synced_at)

    def test_turn_reports_roster_changes_as_a_short_diff(self):
        self.start("a")
        self.assertNotIn("[swarm roster]", self.turn("a"))  # nothing changed
        self.start("b", "Explore")
        b = self.agent("b").name
        self.assertIn(f"[swarm roster] changes: joined: {b} (Explore)", self.turn("a"))
        self.assertNotIn("[swarm roster]", self.turn("a"))  # reported once
        self.turn("b")                                     # b's tool changes are not news
        self.assertNotIn("[swarm roster]", self.turn("a"))
        self.backdate("b", last_seen=6)
        self.h.update_agent("b", current_tool=None)
        self.assertIn(f"changes: idle: {b}", self.turn("a"))
        self.turn("b")
        self.assertIn(f"changes: back: {b}", self.turn("a"))
        self.hook("stop", agent_id="b")
        self.assertIn(f"changes: completed: {b}", self.turn("a"))
        self.assertNotIn("[swarm roster]", self.turn("a"))

    def test_full_roster_refresh_after_the_interval_even_without_changes(self):
        self.start("a")
        self.start("b", "Explore")
        b = self.agent("b").name
        self.turn("a")                                  # the join diff
        self.assertNotIn("[swarm roster]", self.turn("a"))
        self.backdate("a", roster_synced_at=11)         # roster_refresh_minutes default 10
        ctx = self.turn("a")
        self.assertIn('[swarm roster] other agents on job "J" (1 active; ', ctx)
        self.assertIn(f"- {b} (Explore): ", ctx)
        self.assertNotIn("[swarm roster]", self.turn("a"))  # timer restarted

    def test_roster_refresh_interval_is_configurable(self):
        text = self.config.read_text().replace("[board]\n", "[board]\nroster_refresh_minutes = 2\n")
        self.config.write_text(text)
        self.cfg = __import__("swarm.cli", fromlist=["load_config"]).load_config(self.config)
        self.start("a")
        self.backdate("a", roster_synced_at=3)
        self.assertIn("[swarm roster] you are the only agent", self.turn("a"))

    def test_departed_agents_are_listed_as_finished(self):
        self.start("a")
        self.start("b", "tester")
        b = self.agent("b").name
        self.hook("stop", agent_id="b")
        ctx = self.start("c")
        self.assertIn(f"finished: {b} (completed)", ctx)


class CatchUpReadTests(Env):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J")
        self.someone = self.peer()

    def post(self, text: str, who: str | None = None) -> None:
        self.cli("post", "--job", "J", "--as", who or self.someone, text)

    def set_board(self, **values) -> None:
        text = self.config.read_text().replace(
            "[board]\n", "[board]\n" + "".join(f"{k} = {v}\n" for k, v in values.items()))
        self.config.write_text(text)
        self.cfg = __import__("swarm.cli", fromlist=["load_config"]).load_config(self.config)

    def test_new_agent_sees_recent_history_at_start_capped(self):
        self.set_board(join_history=3)
        for i in range(5):
            self.post(f"before {i}")
        ctx = self.context(self.hook("start"))
        self.assertIn("[swarm board] recent messages on this job (before you joined):", ctx)
        self.assertEqual(re.findall(r"before \d", ctx), ["before 2", "before 3", "before 4"])
        self.assertIsNone(self.hook("turn", tool_name="Bash"))  # already delivered

    def test_truncated_read_says_how_many_are_pending_and_delivers_them_next(self):
        self.set_board(read_limit=2)
        self.hook("start")
        for i in range(5):
            self.post(f"m{i}")
        ctx = self.context(self.hook("turn", tool_name="Bash"))
        self.assertEqual(re.findall(r"m\d", ctx), ["m0", "m1"])
        self.assertIn("[swarm board] 3 more unread messages: they are shown before your next tool calls", ctx)
        ctx = self.context(self.hook("turn", tool_name="Bash"))
        self.assertEqual(re.findall(r"m\d", ctx), ["m2", "m3"])
        self.assertIn("1 more unread message:", ctx)
        ctx = self.context(self.hook("turn", tool_name="Bash"))
        self.assertEqual(re.findall(r"m\d", ctx), ["m4"])
        self.assertNotIn("more unread", ctx)

    def test_own_posts_interleaved_never_hide_others(self):
        self.set_board(read_limit=2)
        self.hook("start")
        me = self.agent("agent-1").name
        for i in range(3):
            self.post(f"o{i}")
            self.post(f"mine {i}", who=me)
        got = []
        for _ in range(3):
            out = self.hook("turn", tool_name="Bash")
            got += re.findall(r"o\d|mine \d", self.context(out)) if out else []
        self.assertEqual(got, ["o0", "o1", "o2"])

    def test_spooled_post_delivered_late_is_still_shown(self):
        self.hook("start")
        self.hook("turn", tool_name="Bash")
        spool.spool_post(self.cfg, "J", "Sandboxed", "written while offline", None)
        ctx = self.context(self.hook("turn", tool_name="Bash"))  # this hook flushes, then reads
        self.assertIn("Sandboxed: written while offline", ctx)

    def test_resumed_agent_gets_what_it_missed_when_it_rejoins(self):
        self.hook("start")
        self.hook("stop")
        self.post("while you were away")
        ctx = self.context(self.hook("turn", tool_name="Bash"))
        self.assertIn("[swarm board] messages since you left:", ctx)
        self.assertIn("while you were away", ctx)


class BoardGuidanceTests(Env):
    def test_instructions_ask_for_active_board_use(self):
        self.cli("activate", "--job", "J")
        ctx = self.context(self.hook("start"))
        for phrase in ("Broadcast (no --to) claims before you touch anything shared, findings, "
                       "warnings, blockers and results",
                       "--to '<exact name>' for questions, requests, handoffs and answers",
                       "Always reply to messages addressed to you and acknowledge requests",
                       "pick the recipient from the roster",
                       "ask its owner on the board instead of doing it yourself",
                       "status every few steps"):
            self.assertIn(phrase, ctx)

    def test_messages_addressed_to_the_agent_are_flagged(self):
        self.cli("activate", "--job", "J")
        self.hook("start")
        me = self.agent("agent-1").name
        someone = self.peer()
        self.cli("post", "--job", "J", "--as", someone, "--to", me, "can you check X?")
        self.cli("post", "--job", "J", "--as", someone, "broadcast")
        ctx = self.context(self.hook("turn", tool_name="Bash"))
        self.assertIn("[swarm board] 1 addressed to you: reply with `", ctx)
        self.assertIn(f"--to '{someone}'", ctx)


class CliReadTests(Env):
    def test_read_says_how_many_are_left(self):
        text = self.config.read_text().replace("[board]\n", "[board]\nread_limit = 2\njoin_history = 0\n")
        self.config.write_text(text)
        me = self.cli("join", "--job", "J", "--key", "k")[1].strip()
        other = self.peer()
        for i in range(3):
            self.cli("post", "--job", "J", "--as", other, f"m{i}")
        out = self.cli("read", "--as", me)[1]
        self.assertTrue(out.endswith("\n(1 more unread: run read again)\n"), out)
        self.assertNotIn("more unread", self.cli("read", "--as", me)[1])


class ReplyOwedTests(Env):
    def setUp(self):
        super().setUp()
        self.cli("activate", "--job", "J")
        self.hook("start", agent_id="a")
        self.hook("start", agent_id="b")
        self.a, self.b = self.agent("a").name, self.agent("b").name

    def turn(self, key: str = "a") -> str:
        out = self.hook("turn", agent_id=key, tool_name="Bash")
        return self.context(out) if out else ""

    def test_reminds_once_then_clears_after_a_reply_to_that_sender(self):
        self.cli("post", "--job", "J", "--as", self.b, "--to", self.a, "can you check the VIP?")
        first = self.turn()
        self.assertIn("1 addressed to you", first)      # delivered, flagged
        self.assertNotIn("asked you something", first)   # no reminder in the same breath
        ctx = self.turn()
        self.assertRegex(ctx, rf"\[swarm\] {self.b} asked you something at \d\d:\d\d: reply with "
                              rf"`.* post --job 'J' --as '{self.a}' --to '{self.b}' \"<message>\"`")
        self.assertNotIn("asked you something", self.turn())   # once
        self.cli("post", "--job", "J", "--as", self.b, "--to", self.a, "and the DNS?")
        self.turn()
        self.cli("post", "--job", "J", "--as", self.a, "--to", self.b, "VIP ok, DNS next")
        self.assertNotIn("asked you something", self.turn())   # answered: cleared

    def test_no_reminder_for_broadcasts(self):
        self.cli("post", "--job", "J", "--as", self.b, "everyone: db-1 is leader")
        self.turn()
        self.assertNotIn("asked you something", self.turn())


class SilenceNudgeTests(Env):
    def setUp(self):
        super().setUp()
        text = self.config.read_text().replace(
            "[board]\n", "[board]\nsilence_nudge_calls = 3\nsilence_nudge_minutes = 60\n")
        self.config.write_text(text)
        self.cfg = __import__("swarm.cli", fromlist=["load_config"]).load_config(self.config)
        self.cli("activate", "--job", "J")
        self.hook("start")
        self.me = self.agent("agent-1").name

    def turn(self) -> str:
        out = self.hook("turn", tool_name="Bash")
        return self.context(out) if out else ""

    def test_nudges_once_after_n_calls_and_resets_on_post(self):
        self.assertNotIn("[swarm] status?", self.turn())
        self.assertNotIn("[swarm] status?", self.turn())
        ctx = self.turn()  # 3rd call without posting
        self.assertIn("[swarm] status? You have not posted for 3 tool calls", ctx)
        self.assertIn(f"post --job 'J' --as '{self.me}'", ctx)
        for _ in range(4):
            self.assertNotIn("[swarm] status?", self.turn())  # once per quiet window
        self.cli("post", "--job", "J", "--as", self.me, "status: halfway")
        self.assertNotIn("[swarm] status?", self.turn())
        self.assertNotIn("[swarm] status?", self.turn())
        self.assertIn("[swarm] status?", self.turn())  # a new window after the post

    def test_nudges_after_t_minutes(self):
        self.h.backdate_agent("agent-1", joined_at=61 * 60)
        self.assertIn("[swarm] status? You have not posted for 61 minutes", self.turn())


class AddressingTests(Env):
    def test_who_and_roster_give_exact_names_role_status_tool(self):
        self.cli("activate", "--job", "J")
        self.hook("start", agent_id="a", agent_type="Explore")
        self.hook("turn", agent_id="a", tool_name="Grep")
        a = self.agent("a").name
        out = self.cli("who", "--job", "J")[1]
        fields = out.splitlines()[0].split("\t")
        self.assertEqual(fields[:4], [a, "claude", "Explore", "running"])
        self.assertEqual(fields[5], "in Grep")
        ctx = self.context(self.hook("start", agent_id="b"))
        self.assertIn("address them with --to '<exact name>'", ctx)
        self.assertIn(f"- {a} (Explore): running, in Grep", ctx)

    def test_who_puts_harness_in_its_own_field_after_the_exact_name(self):
        self.cli("activate", "--job", "J")
        self.hook("start", agent_id="a", agent_type="Explore")
        a = self.agent("a").name
        with self.board() as board:
            board.set_agent_runtime("a", "codex", "gpt-x")
        out = self.cli("who", "--job", "J")[1]
        fields = out.splitlines()[0].split("\t")
        self.assertEqual(fields[0], a)          # exact name, unchanged, for --to
        self.assertEqual(fields[1], "codex")    # harness in its own field
