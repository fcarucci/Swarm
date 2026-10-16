"""PM-STALL: an orchestrator item unhandled for orchestrator_stall_minutes is announced on the board, once per
interval; the person is notified only when configured; status and watch say "waiting on ORCHESTRATOR"."""
from __future__ import annotations

import datetime as dt

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

from swarm import stall  # noqa: E402

MIN = dt.timedelta(minutes=1)


class StallEnv(Env):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.pm = self.cli("join", "--job", "J", "--key", "pm", "--role", "project_manager")[1].strip()
        self.eng = self.cli("join", "--job", "J", "--key", "e1", "--role", "engineer")[1].strip()
        self.board_ = self.h.board()
        self.addCleanup(self.board_.close)

    def post(self, who, text, to=None) -> int:
        return self.board_.post("J", who, text, to=to).id

    def age(self, msg_id, minutes):
        self.h.backdate_message(msg_id, minutes * 60)

    def stalls(self):
        return [m.message for m in self.board_.messages_after(0, "J") if m.message.startswith(stall.PREFIX)]

    def check(self, **kw):
        return stall.check(self.board_, "J", kw.pop("cfg", self.cfg), **kw)


class PostingTests(StallEnv):
    def test_nothing_before_n_minutes(self):
        self.age(self.post(self.eng, "land this?", to=self.pm), 4)
        self.assertEqual(self.check(), [])
        self.assertEqual(self.stalls(), [])

    def test_posted_after_n_minutes(self):
        mid = self.post(self.eng, "land this?", to=self.pm)
        self.age(mid, 6)
        posted = self.check()
        self.assertEqual(len(posted), 1)
        self.assertEqual(self.stalls(), posted)
        self.assertTrue(posted[0].startswith(f"PM-STALL msg#{mid} age=6 "), posted[0])

    def test_not_reposted_within_n_minutes_then_again_after(self):
        mid = self.post(self.eng, "land this?", to=self.pm)
        self.age(mid, 6)
        self.assertEqual(len(self.check()), 1)
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(now=self.board_.now() + 3 * MIN), [])
        self.assertEqual(len(self.check(now=self.board_.now() + 6 * MIN)), 1)
        self.assertEqual(len(self.stalls()), 2)

    def test_configured_minutes(self):
        self.cfg["job"]["orchestrator_stall_minutes"] = 2
        self.age(self.post(self.eng, "land this?", to=self.pm), 3)
        self.assertEqual(len(self.check()), 1)

    def test_zero_turns_it_off(self):
        self.cfg["job"]["orchestrator_stall_minutes"] = 0
        self.age(self.post(self.eng, "land this?", to=self.pm), 60)
        self.assertEqual(self.check(), [])

    def test_role_queued_message_counts(self):
        self.age(self.post(self.eng, "land this?", to="PM"), 6)
        self.assertEqual(len(self.check()), 1)

    def test_a_reply_to_the_poster_handles_it(self):
        mid = self.post(self.eng, "land this?", to=self.pm)
        self.age(mid, 6)
        self.post(self.pm, "landed", to=self.eng)
        self.assertEqual(self.check(), [])

    def test_a_reply_to_someone_else_does_not(self):
        mid = self.post(self.eng, "land this?", to=self.pm)
        self.age(mid, 6)
        self.post(self.pm, "unrelated", to="someone")
        self.assertEqual(len(self.check()), 1)

    def test_unaddressed_and_own_messages_are_not_items(self):
        self.age(self.post(self.eng, "broadcast"), 10)
        self.age(self.post(self.pm, "note", to=self.eng), 10)
        self.assertEqual(self.check(), [])

    def test_open_blocker_for_the_orchestrator_then_resolved(self):
        b = self.board_.open_blocker("J", "question", "@PM", "which sha?", created_by=self.eng)
        later = self.board_.now() + 6 * MIN
        posted = self.check(now=later)
        self.assertEqual(len(posted), 1)
        self.assertIn(f"blocker#{b.id}", posted[0])
        self.board_.resolve_blocker(b.id, "that one", actor=self.pm)
        self.assertEqual(self.check(now=later + 6 * MIN), [])

    def test_blocker_waiting_on_a_person_is_not_the_orchestrators(self):
        self.board_.open_blocker("J", "question", "human", "colour?", created_by=self.eng)
        self.assertEqual(self.check(now=self.board_.now() + 30 * MIN), [])


class NotifyTests(StallEnv):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.age(self.post(self.eng, "land this?", to=self.pm), 7)

    def test_not_sent_by_default(self):
        self.assertEqual(len(self.check(notifier=lambda cfg, text: self.sent.append(text))), 1)
        self.assertEqual(self.sent, [])

    def test_sent_when_configured(self):
        self.cfg["job"]["notify_on_stall"] = True
        self.check(notifier=lambda cfg, text: self.sent.append(text))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("the swarm is waiting on your PM: message #"), self.sent)
        self.assertTrue(self.sent[0].endswith(" age=7"), self.sent)
        self.check(notifier=lambda cfg, text: self.sent.append(text))   # not again within the interval
        self.assertEqual(len(self.sent), 1)

    def test_notify_send_without_channels_sends_nothing(self):
        from swarm import notify
        self.assertFalse(notify.send({}, "x"))
        self.assertFalse(notify.send({"notify": {"push_url": "file:///etc/passwd"}}, "x"))


class StatusTests(StallEnv):
    def test_status_says_orchestrator_not_person(self):
        self.assertNotIn("ORCHESTRATOR", self.cli("status", "--job", "J")[1])
        self.age(self.post(self.eng, "land this?", to=self.pm), 7)
        out = " ".join(self.cli("status", "--job", "J")[1].split())
        self.assertIn("waiting on ORCHESTRATOR since 7m", out)

    def test_watch_heading_says_it(self):
        from swarm import cli
        self.age(self.post(self.eng, "land this?", to=self.pm), 7)
        j = self.board_.job_status("J")
        self.assertIn("waiting on ORCHESTRATOR since 7m", cli._job_heading(self.board_, j, self.board_.now()))
