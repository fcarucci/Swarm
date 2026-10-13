# tests/test_memory_tags.py
"""A job's memory subject tags: activation, `swarm job tags`, and what `swarm remember` stamps on a memory.

Subjects only: the job name, an agent name, a session, a run, a date or any id goes into the metadata, never
into the tags (no retain ever sends an id-like tag)."""
from __future__ import annotations

import os
from unittest import mock

from test_hindsight import HindsightEnv  # noqa: E402

from swarm import learnings  # noqa: E402

SESSION = "3f2a9c1e-0b7d-4c55-9a7e-2d1f8b6c4e10"


class MemoryTagsTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.fake.banks["proj"] = []
        rc, _, err = self.cli("activate", "--job", "J", "--project", "proj", "--tags", "swarm,astro-loom")
        self.assertEqual(rc, 0, err)

    def retained(self) -> list[dict]:
        return [r["body"]["items"][0] for r in self.fake.calls("POST", "/v1/default/banks/proj/memories")]

    def remember(self, *extra, fact="a fact") -> dict:
        rc, out, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", *extra, fact)
        self.assertEqual(rc, 0, err)
        return self.retained()[-1]

    def job_tags(self, *args) -> tuple[int, str, str]:
        return self.cli("job", "tags", "--job", "J", *args)

    def notices(self) -> list[str]:
        with self.board() as b:
            return [m.message for m in b.recent_messages(50, job="J") if m.agent_name == "swarm"]

    # --- activation and what remember stamps

    def test_the_job_subject_tags_are_stamped_on_remember(self):
        self.assertEqual(self.remember()["tags"], ["swarm", "astro-loom"])

    def test_extra_tags_from_the_agent_are_added_once(self):
        item = self.remember("--tags", "etcd,astro-loom", "--tags", "etcd")
        self.assertEqual(item["tags"], ["swarm", "astro-loom", "etcd"])

    def test_metadata_carries_job_agent_and_session(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            item = self.remember()
        meta = item["metadata"]
        self.assertEqual((meta["job"], meta["agent"], meta["session_id"]), ("J", "Homer Simpson", SESSION))
        self.assertNotIn("job:J", item["tags"])
        self.assertNotIn(SESSION, item["tags"])

    def test_no_subject_tags_falls_back_to_the_project_name(self):
        self.job_tags("--set", "")
        self.assertEqual(self.remember()["tags"], ["proj"])

    def test_the_job_name_and_agent_name_are_never_tags(self):
        item = self.remember("--tags", "j,astro-loom")   # the job J's name, from the agent's --tags
        self.assertEqual(item["tags"], ["swarm", "astro-loom"])
        rc, _, err = self.cli("remember", "--job", "J", "--as", "Homer", "--tags", "homer", "a fact")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.retained()[-1]["tags"], ["swarm", "astro-loom"])

    # --- swarm job tags: remember reads the tags at each call

    def test_list_shows_the_stored_tags(self):
        rc, out, err = self.job_tags()
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("swarm, astro-loom", out)

    def test_added_tag_is_used_by_the_next_remember(self):
        self.remember()
        rc, out, err = self.job_tags("--add", "etcd")
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(self.remember()["tags"], ["swarm", "astro-loom", "etcd"])

    def test_removed_tag_is_no_longer_used_by_remember(self):
        rc, _, err = self.job_tags("--remove", "swarm")
        self.assertEqual((rc, err), (0, ""), err)
        self.assertEqual(self.remember()["tags"], ["astro-loom"])

    def test_set_replaces_the_tags(self):
        self.job_tags("--set", "etcd,hermes")
        self.assertEqual(self.remember()["tags"], ["etcd", "hermes"])

    def test_each_change_posts_a_board_notice(self):
        self.job_tags("--add", "etcd")
        self.job_tags("--add", "etcd")          # no change: no second notice
        self.job_tags("--remove", "swarm")
        changed = [n for n in self.notices() if "subject tags of J changed" in n]
        self.assertEqual(len(changed), 2, changed)
        self.assertIn("swarm, astro-loom, etcd", changed[0])

    def test_tags_can_change_when_the_job_is_closed(self):
        with self.board() as b:
            self.assertTrue(b.close_job("J", "completed", "tag test"))   # a person's `deactivate` is not needed here
        rc, _, err = self.job_tags("--add", "etcd")
        self.assertEqual((rc, err), (0, ""), err)
        self.assertIn("etcd", self.job_tags()[1])

    def test_an_unknown_job_is_refused(self):
        rc, _, err = self.cli("job", "tags", "--job", "nope", "--add", "etcd")
        self.assertEqual(rc, 1)
        self.assertIn("no such job", err)

    # --- no id-like tag, ever

    ID_CANDIDATES = ["job:J", "agent:homer", "session:abc", "run:7", "2026-10-13", "20261013",
                     SESSION, "parent:x", "deadbeefcafe"]

    def test_id_like_tags_are_refused_by_remember_and_job_tags(self):
        for bad in self.ID_CANDIDATES:
            rc, _, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "--tags", bad, "a fact")
            self.assertEqual(rc, 2, bad)
            rc, _, err = self.job_tags("--add", bad)
            self.assertEqual(rc, 2, bad)
            self.assertIn("tag", err, bad)
        self.assertEqual(self.retained(), [])
        self.assertEqual(self.job_tags()[1].strip(), "J: swarm, astro-loom")

    def test_no_retain_ever_sends_an_id_like_tag(self):
        self.remember("--tags", "etcd")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION}):
            self.remember()
        self.job_tags("--set", "hermes,astro-loom")
        self.remember()
        self.assertTrue(self.retained())
        for item in self.retained():
            for tag in item["tags"]:
                self.assertIsNone(learnings.ID_LIKE.fullmatch(tag), tag)
                self.assertNotIn(":", tag)
                self.assertNotIn(SESSION, tag)
