"""`swarm post --to @role` resolves to the seat's current holders; unknown recipients and authors
outside the job are rejected, never stored. Runs on the backend under test (SWARM_TEST_BACKEND)."""
from __future__ import annotations

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

from swarm import addressing  # noqa: E402


class SeatsEnv(Env):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.names = {}
        for key, role in (("el", "engineering_lead"), ("pm", "product_manager"), ("qa", "qa"),
                          ("be", "build_engineer"), ("e1", "engineer"), ("e2", "engineer")):
            self.names[key] = self.cli("join", "--job", "J", "--key", key, "--role", role)[1].strip()

    def messages(self):
        with self.board() as b:
            return [(m.agent_name, m.to_agent, m.message) for m in b.recent_messages(50, job="J")]


class AddressingTests(SeatsEnv):
    def test_role_alias_resolves_to_the_holder(self):
        for alias, key in (("@EL", "el"), ("@PM", "pm"), ("@QA", "qa"), ("@build_engineer", "be")):
            with self.subTest(alias=alias):
                rc, out, err = self.cli("post", "--job", "J", "--as", self.names["e1"], "--to", alias, "hi")
                self.assertEqual((rc, err), (0, ""))
                self.assertIn(f"to {alias} ({self.names[key]})", out)
                self.assertIn((self.names["e1"], self.names[key], "hi"), self.messages())

    def test_a_seat_with_several_holders_gets_one_message_each(self):
        rc, out, _ = self.cli("post", "--job", "J", "--as", self.names["el"], "--to", "@engineer", "go")
        self.assertEqual(rc, 0)
        got = {to for _, to, msg in self.messages() if msg == "go"}
        self.assertEqual(got, {self.names["e1"], self.names["e2"]})

    def test_the_judge_is_addressable(self):
        judge = self.cli("join", "--job", "J", "--key", "jj", "--role", "judge", "--judge")[1].strip()
        rc, _, _ = self.cli("post", "--job", "J", "--as", self.names["el"], "--to", "@judge", "proof")
        self.assertEqual(rc, 0)
        self.assertIn((self.names["el"], judge, "proof"), self.messages())

    def test_a_seat_nobody_holds_is_rejected_and_nothing_is_stored(self):
        before = self.messages()
        rc, _, err = self.cli("post", "--job", "J", "--as", self.names["el"], "--to", "@verifier", "x")
        self.assertEqual(rc, 1)
        self.assertIn("nobody holds @verifier", err)
        self.assertEqual(self.messages(), before)

    def test_an_unknown_name_is_rejected(self):
        before = self.messages()
        rc, _, err = self.cli("post", "--job", "J", "--as", self.names["el"], "--to", "Nobody Here", "x")
        self.assertEqual(rc, 1)
        self.assertIn("no agent named 'Nobody Here'", err)
        self.assertEqual(self.messages(), before)

    def test_a_plain_agent_name_still_works_and_no_to_is_a_broadcast(self):
        self.assertEqual(self.cli("post", "--job", "J", "--as", self.names["el"], "--to", self.names["qa"], "a")[0], 0)
        self.assertEqual(self.cli("post", "--job", "J", "--as", self.names["el"], "b")[0], 0)
        self.assertEqual([m for m in self.messages() if m[2] in ("a", "b")],
                         [(self.names["el"], self.names["qa"], "a"), (self.names["el"], None, "b")])

    def test_the_author_must_be_an_agent_of_the_job(self):
        before = self.messages()
        rc, _, err = self.cli("post", "--job", "J", "--as", "Made Up", "x")
        self.assertEqual(rc, 1)
        self.assertIn("is not an agent of job J", err)
        self.assertEqual(self.messages(), before)

    def test_an_agent_of_another_job_is_sent_there_not_stored_on_this_one(self):
        self.assertEqual(self.cli("activate", "--job", "K")[0], 0)
        other = self.cli("join", "--job", "K", "--key", "k1", "--role", "qa")[1].strip()
        before = self.messages()
        rc, _, err = self.cli("post", "--job", "J", "--as", other, "intruder")
        self.assertEqual(rc, 0)
        self.assertIn("is on K now", err)   # the existing moved-agent redirect
        self.assertEqual(self.messages(), before)

    def test_the_key_names_the_author_and_must_match(self):
        rc, _, _ = self.cli("post", "--job", "J", "--key", "el", "--to", "@QA", "via key")
        self.assertEqual(rc, 0)
        self.assertIn((self.names["el"], self.names["qa"], "via key"), self.messages())
        rc, _, err = self.cli("post", "--job", "J", "--as", self.names["qa"], "--key", "el", "forged")
        self.assertEqual(rc, 1)
        self.assertIn("--key does not belong", err)
        rc, _, _ = self.cli("post", "--job", "J", "--to", "@QA", "no author")
        self.assertEqual(rc, 2)

    def test_a_departed_holder_no_longer_holds_the_seat(self):
        with self.board() as b:
            b.agent_stopped("qa")
        rc, _, err = self.cli("post", "--job", "J", "--as", self.names["el"], "--to", "@QA", "x")
        self.assertEqual(rc, 1)
        self.assertIn("nobody holds @QA", err)

    def test_the_read_shows_the_resolved_recipient(self):
        self.cli("post", "--job", "J", "--as", self.names["el"], "--to", "@QA", "look")
        rc, out, _ = self.cli("read", "--key", "qa", "--job", "J", "--peek")
        self.assertIn("look", out)


class ResolveTests(SeatsEnv):
    def test_pm_falls_back_to_the_orchestrating_seats(self):
        with self.board() as b:
            b.agent_stopped("pm")
            b.allocate_name("pj", "J", "project_manager")
            self.assertEqual(addressing.resolve(b, "J", "@PM"), [b.active_agent_name("pj")])

    def test_case_is_ignored_and_junk_is_refused(self):
        with self.board() as b:
            self.assertEqual(addressing.resolve(b, "J", "@el"), [self.names["el"]])
            for bad in ("@", "@bad role"):
                with self.assertRaises(addressing.AddressError):
                    addressing.resolve(b, "J", bad)
