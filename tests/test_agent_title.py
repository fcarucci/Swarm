"""The optional agent title: a short free-text seat label (`EL`, `PM`, `Eng: board view`) kept apart
from the role, set by a `[swarm title: ...]` spawn tag, `swarm join --title` or `swarm title`, stored
by schema 20 on every backend, and shown by `who`, `status` and `watch` (never on board messages).

The board contract and the schema 19 -> 20 migration run on the memory, file and sqlite backends
(Postgres with SWARM_TEST_CONFIG); the hook and CLI tests on SWARM_TEST_BACKEND."""
from __future__ import annotations

import os
import types
import unittest
from pathlib import Path

from support import FileHarness, MemoryHarness, PostgresHarness, SqliteHarness, SMALL_POOL  # noqa: F401
from test_routing import RoutingEnv  # noqa: E402  (sets sys.path)
from test_team_plugin import TeamEnv  # noqa: E402

from swarm import cli as swarm  # noqa: E402
from swarm.board import SCHEMA_VERSION, setup_board  # noqa: E402


class CleanTitleTests(unittest.TestCase):
    def test_whitespace_is_collapsed_and_the_text_capped_at_60(self):
        from swarm.board.base import TITLE_MAX, clean_title
        self.assertEqual(TITLE_MAX, 60)
        self.assertEqual(clean_title("  Eng:\t board \n view  "), "Eng: board view")
        self.assertEqual(len(clean_title("x" * 200)), 60)
        self.assertEqual(clean_title("a " * 50), ("a " * 30).strip())   # cut, then trimmed again
        self.assertEqual(clean_title("lead\x1b[2J\x07"), "lead[2J")      # control characters never stored

    def test_nothing_left_means_no_title(self):
        from swarm.board.base import clean_title
        for empty in (None, "", "   ", "\n\t", "\x1b\x07"):
            self.assertIsNone(clean_title(empty))

    def test_the_tag_parser_reads_the_first_title_line_only(self):
        from swarm import titles
        self.assertEqual(titles.from_prompt("[swarm job: J]\n[swarm title:  QA ]\nwork"), "QA")
        self.assertEqual(titles.from_prompt("[swarm title: EL]\n[swarm title: PM]"), "EL")
        self.assertEqual(titles.from_prompt("[swarm title: " + "y" * 90 + "]"), "y" * 60)
        for none in ("", "no tag", "[swarm title: ]", "[swarm title:]", "text [swarm title: EL] inline"):
            self.assertIsNone(titles.from_prompt(none), none)


class TitleContract:
    harness_factory = None

    @classmethod
    def setUpClass(cls):
        cls.h = cls.harness_factory()

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def setUp(self):
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def agent(self, key, job="j"):
        return next(a for a in self.b.agents(job) if a.agent_key == key)

    def test_set_replace_and_clear(self):
        self.b.allocate_name("k", "j", "engineer")
        self.assertIsNone(self.agent("k").title)
        self.assertTrue(self.b.set_agent_title("k", "EL"))
        self.assertEqual(self.agent("k").title, "EL")
        self.assertTrue(self.b.set_agent_title("k", "  Eng:\n board   view "))
        self.assertEqual(self.agent("k").title, "Eng: board view")
        self.assertTrue(self.b.set_agent_title("k", ""))
        self.assertIsNone(self.agent("k").title)
        self.assertTrue(self.b.set_agent_title("k", "QA"))
        self.assertTrue(self.b.set_agent_title("k", None))
        self.assertIsNone(self.agent("k").title)
        self.assertEqual(self.agent("k").role, "engineer")   # the role is another thing

    def test_the_cap_is_applied_by_the_board(self):
        self.b.allocate_name("k", "j")
        self.b.set_agent_title("k", "z" * 100)
        self.assertEqual(self.agent("k").title, "z" * 60)

    def test_only_an_active_agent_has_its_title_changed(self):
        self.b.allocate_name("k", "j")
        self.b.set_agent_title("k", "QA")
        self.b.agent_stopped("k")
        self.assertFalse(self.b.set_agent_title("k", "other"))
        self.assertFalse(self.b.set_agent_title("nobody", "x"))
        self.assertEqual(self.agent("k").title, "QA")

    def test_roster_and_status_rows_keep_working_with_titles(self):
        self.b.allocate_name("k", "j", "qa")
        self.b.set_agent_title("k", "QA")
        self.assertEqual([r.role for r in self.b.roster("j")], ["qa"])
        self.assertEqual([a.title for a in self.b.agents("j", include_departed=False)], ["QA"])

    def test_a_supervisor_replacement_inherits_the_title(self):
        name = self.b.allocate_name("old", "j", "engineer")
        self.b.set_agent_title("old", "Eng: api")
        self.assertTrue(self.b.close_agent("old", "stuck:dead"))
        self.assertEqual(self.b.claim_resume("new", "old", "j"), name)
        self.assertEqual(self.agent("new").title, "Eng: api")
        self.assertEqual(self.agent("old").title, "Eng: api")

    def test_a_replacement_of_an_untitled_agent_has_none(self):
        self.b.allocate_name("old", "j")
        self.b.close_agent("old", "stuck:dead")
        self.b.claim_resume("new", "old", "j")
        self.assertIsNone(self.agent("new").title)

    def test_a_revived_agent_keeps_its_title(self):
        self.b.allocate_name("k", "j")
        self.b.set_agent_title("k", "PM")
        self.b.agent_stopped("k")
        self.b.allocate_name("k", "j")     # the same key comes back (resumed): same incarnation
        self.assertEqual(self.agent("k").title, "PM")

    def test_a_pause_resume_keeps_the_title(self):
        name = self.b.allocate_name("k", "j", "engineer")
        self.b.set_agent_title("k", "EL")
        self.assertIsNotNone(self.b.pause_job("j", "me", "hold"))
        self.assertEqual(self.b.claim_resume("k2", "k", "j"), name)
        self.assertEqual(self.agent("k2").title, "EL")

    def test_titles_are_a_write_of_the_board(self):
        from swarm.board.base import WRITE_METHODS
        self.assertIn("set_agent_title", WRITE_METHODS)


class MemoryTitleContract(TitleContract, unittest.TestCase):
    harness_factory = MemoryHarness


class SqliteTitleContract(TitleContract, unittest.TestCase):
    harness_factory = SqliteHarness


class FileTitleContract(TitleContract, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresTitleContract(TitleContract, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


# ---- schema 20 ---------------------------------------------------------------------------------

OLD_SQLITE_AGENT_COLS = ("agent_key, name, job, role, host, joined_at, last_seen, left_at, state, tool_calls, "
                         "current_tool, tool_started_at, last_post_at, judge, verifier, harness, model, os_user, "
                         "left_reason, resume_of")
OLD_PG_AGENT_STATUS_COLS = ("job, name, role, status, current_tool, tool_calls, messages, joined_at, "
                            "last_contact_at, last_post_at, ended_at, host, agent_key, harness, model, os_user, "
                            "left_reason, resume_of")


class Schema20Migration:
    """A board at schema 19 (no title anywhere) upgrades in place, keeps its agents, and is still
    readable the way the previous release reads it."""
    harness_factory = None

    def setUp(self):
        self.h = self.harness_factory()
        self.addCleanup(self.h.close)
        self.h.reset()

    def test_the_schema_is_at_least_20(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 20)
        with self.h.board() as b:
            name = b.allocate_name("k", "j")
            self.assertTrue(b.set_agent_title("k", "EL"))   # the title column exists
            self.assertEqual(b.agents("j")[0].title, "EL")

    def test_a_schema_19_board_upgrades_and_keeps_its_agents(self):
        with self.h.board() as b:
            cls = type(b)
            name = b.allocate_name("k", "j", "engineer")
            b.post("j", name, "hello")
        self.downgrade_to_19()
        self.assertEqual(cls.schema_version(self.h.cfg), 19)
        for _ in range(2):   # the migration, then an idempotent re-run
            setup_board(self.h.cfg, SMALL_POOL)
            self.assertGreaterEqual(cls.schema_version(self.h.cfg), 20)
            with self.h.board() as b:
                (a,) = b.agents("j")
                self.assertEqual((a.name, a.role, a.title), (name, "engineer", None))
                self.assertTrue(b.set_agent_title("k", "EL"))
                self.assertEqual(b.agents("j")[0].title, "EL")
                self.assertTrue(b.set_agent_title("k", ""))
        self.assertEqual(self.old_client_read(), [name])

    def test_old_clients_still_read_a_board_that_has_titles(self):
        with self.h.board() as b:
            name = b.allocate_name("k", "j")
            b.set_agent_title("k", "QA")
        self.assertEqual(self.old_client_read(), [name])


class MemorySchema20(Schema20Migration, unittest.TestCase):
    harness_factory = staticmethod(lambda: MemoryHarness("schema20"))

    def downgrade_to_19(self):
        with self.h.store.lock:
            self.h.store.schema_version = 19
            for row in self.h.store.agents.values():
                row.pop("title", None)

    def old_client_read(self):
        # the previous release's reads index the row dict by its old keys only
        with self.h.store.lock:
            return [a["name"] for a in self.h.store.agents.values()
                    if all(k in a for k in ("role", "harness", "model", "resume_of"))]


class FileSchema20(MemorySchema20):
    harness_factory = FileHarness

    def downgrade_to_19(self):
        super().downgrade_to_19()
        (Path(self.h.cfg["file"]["path"]) / "schema_version").write_text("19\n")


class SqliteSchema20(Schema20Migration, unittest.TestCase):
    harness_factory = SqliteHarness

    def downgrade_to_19(self):
        c = self.h._db()
        c.execute("ALTER TABLE agents DROP COLUMN title")
        c.execute("PRAGMA user_version = 19")

    def old_client_read(self):
        c = self.h._db()
        return [r[1] for r in c.execute(f"SELECT {OLD_SQLITE_AGENT_COLS} FROM agents WHERE job = 'j'")]


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresSchema20(Schema20Migration, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))

    def downgrade_to_19(self):
        c = self.h.conn
        c.execute("DROP VIEW IF EXISTS job_status")
        c.execute("DROP VIEW IF EXISTS agent_status")
        c.execute("ALTER TABLE agents DROP COLUMN title")
        c.execute("UPDATE board_meta SET value = '19' WHERE key = 'schema_version'")

    def old_client_read(self):
        return [r[1] for r in self.h.conn.execute(
            f"SELECT {OLD_PG_AGENT_STATUS_COLS} FROM agent_status WHERE job = 'j'").fetchall()]


# ---- hooks and CLI -----------------------------------------------------------------------------

class TitleCliTests(RoutingEnv):
    def title_of(self, key, job="J"):
        a = self.agent(key, job)
        return a.title if a else None

    def test_a_spawn_tag_sets_the_title(self):
        self.activate("J")
        self.spawn("e1", "[swarm job: J]\n[swarm role: engineer]\n[swarm title: Eng: board view]\nBuild it.")
        self.assertEqual(self.member("e1").title, "Eng: board view")
        self.assertEqual(self.member("e1").role, "engineer")   # separate from the role

    def test_a_spawn_without_the_tag_has_no_title(self):
        self.activate("J")
        self.spawn("e1", "[swarm job: J]\n[swarm role: engineer]\nBuild it.")
        self.assertIsNone(self.member("e1").title)

    def test_the_tag_is_capped_and_cleaned(self):
        self.activate("J")
        self.spawn("e1", "[swarm job: J]\n[swarm title:   " + "word  " * 30 + "]\nBuild it.")
        t = self.member("e1").title
        self.assertTrue(55 <= len(t) <= 60, t)
        self.assertNotIn("  ", t)

    def test_the_tag_works_without_a_role_and_on_a_job_picked_by_the_tag(self):
        self.activate("A")
        self.activate("B")
        self.spawn("q", "[swarm job: B]\n[swarm title: QA]\nTest.")
        self.assertEqual(self.member("q").title, "QA")
        self.assertEqual(self.job_of("q"), "B")

    def test_a_resumed_agent_keeps_its_title(self):
        self.activate("J")
        self.spawn("e1", "[swarm job: J]\n[swarm title: PM]\nGo.")
        self.hook("stop", agent_id="e1")
        self.start("e1")
        self.assertEqual(self.member("e1").title, "PM")

    def test_join_title(self):
        rc, name, err = self.cli("join", "--job", "J", "--key", "k1", "--title", "  Eng:  api ")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.title_of("k1"), "Eng: api")
        self.assertEqual(name.strip(), self.agent("k1").name)   # stdout stays the name only
        self.cli("join", "--job", "J", "--key", "k1")           # a plain re-join keeps it
        self.assertEqual(self.title_of("k1"), "Eng: api")

    def test_title_set_replace_and_clear(self):
        name = self.peer("J", "k1")
        self.assertEqual(self.cli("title", "--job", "J", "--as", name, "EL")[0], 0)
        self.assertEqual(self.title_of("k1"), "EL")
        rc, out, _ = self.cli("title", "--job", "J", "--key", "k1", "Eng:", "scope")
        self.assertEqual(rc, 0)
        self.assertEqual(self.title_of("k1"), "Eng: scope")
        self.assertIn("Eng: scope", out)
        rc, out, _ = self.cli("title", "--job", "J", "--as", name, "")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.title_of("k1"))
        self.assertIn("cleared", out)

    def test_title_needs_exactly_one_of_as_and_key_and_an_active_member(self):
        name = self.peer("J", "k1")
        rc, _, err = self.cli("title", "--job", "J", "EL")
        self.assertEqual(rc, 2)
        rc, _, err = self.cli("title", "--job", "J", "--as", name, "--key", "k1", "EL")
        self.assertEqual(rc, 2)
        rc, _, err = self.cli("title", "--job", "J", "--as", "Nobody Here", "EL")
        self.assertEqual(rc, 1)
        self.assertIn("no active agent", err)
        self.assertIsNone(self.title_of("k1"))

    def test_title_of_an_agent_of_another_job_is_refused(self):
        self.cli("job", "K", "--description", "d")
        self.peer("J", "k1")
        rc, _, err = self.cli("title", "--job", "K", "--key", "k1", "EL")
        self.assertEqual(rc, 1)
        self.assertIsNone(self.title_of("k1"))

    def test_who_shows_the_title_field_blank_without_one(self):
        a = self.peer("J", "ka")
        b = self.peer("J", "kb")
        self.cli("title", "--job", "J", "--as", b, "EL")
        rows = {r[0]: r for r in (l.split("\t") for l in self.cli("who", "--job", "J")[1].splitlines())}
        # name, harness, role, title, status, last contact, tool
        self.assertEqual(len(rows[a]), 7)
        self.assertEqual(rows[a][3], "")
        self.assertEqual(rows[b][3], "EL")
        self.assertEqual(rows[b][4], "started")

    def test_status_job_table_has_a_title_column_only_when_some_agent_has_one(self):
        a = self.peer("J", "ka")
        out = self.cli("status", "--job", "J", "--no-color")[1]
        self.assertNotIn("TITLE", out)
        self.cli("title", "--job", "J", "--as", a, "Eng: ui")
        out = self.cli("status", "--job", "J", "--no-color")[1]
        header = next(l for l in out.splitlines() if l.startswith("AGENT"))
        self.assertRegex(header, r"^AGENT\s+TITLE\s+ROLE")
        row = next(l for l in out.splitlines() if l.startswith(a))
        self.assertRegex(row, r"^" + a + r"\s+Eng: ui\s")

    def test_watch_shows_the_title_in_the_table_and_the_compact_line(self):
        a = self.peer("J", "ka", role="engineer")
        self.peer("J", "kb")
        self.cli("title", "--job", "J", "--as", a, "EL")
        with self.board() as b:
            rows = [x for x in b.agents("J") if x.name == a]
            wide = swarm._compact_agent_line(rows[0], 100, False)
            narrow = swarm._compact_agent_line(rows[0], 24, False)
            plain = swarm._compact_agent_line([x for x in b.agents("J") if x.name != a][0], 100, False)
            table = swarm.agents_table(b, "J", False, b.now())
        self.assertIn(f"{a} (EL) ", wide)
        self.assertLessEqual(len(narrow), 24)
        self.assertNotIn("()", plain)
        self.assertNotIn("(", plain)
        self.assertIn("TITLE", table)

    def test_a_title_never_appears_on_board_messages(self):
        a = self.peer("J", "ka")
        self.cli("title", "--job", "J", "--as", a, "EL")
        self.cli("post", "--job", "J", "--as", a, "hello")
        out = self.cli("read", "--key", "ka", "--peek")[1] + self.cli("read", "--job", "J", "--key", "ka")[1]
        self.assertNotIn("EL", out.replace("hello", ""))

    def test_hostile_titles_are_made_safe_for_the_terminal(self):
        a = self.peer("J", "ka")
        with self.board() as b:
            b.set_agent_title("ka", "x\x1b[31mred")
        self.assertNotIn("\x1b", self.cli("who", "--job", "J")[1])
        self.assertNotIn("\x1b", self.cli("status", "--job", "J", "--no-color")[1])


class PipelineSeatTitleTests(unittest.TestCase):
    def action(self, role):
        job = types.SimpleNamespace(job="J", goal="g", task="t", description="d")
        handoff = types.SimpleNamespace(id=1, artifact="b@" + "a" * 40, agent_name="X", summary="s")
        return types.SimpleNamespace(job=job, handoff=handoff, role=role, verdict=None)

    def test_a_recipe_title_is_tagged_in_the_seat_prompt(self):
        from swarm.supervisor import pipeline
        text = pipeline.prompt_for(self.action("judge"), {"titles": {"judge": "Judge"}})
        self.assertIn("\n[swarm title: Judge]\n", text)
        self.assertNotIn("[swarm title:", pipeline.prompt_for(self.action("judge"), {}))


# ---- engineering-team --------------------------------------------------------------------------

class TeamTitleTests(TeamEnv):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)

    def test_seat_titles_are_short_labels(self):
        import importlib.util
        from swarm import paths
        spec = importlib.util.spec_from_file_location(
            "team_plugin_t", paths.PLUGIN_ROOT / "skills" / "engineering-team" / "swarm_plugin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.SEAT_TITLES, {
            "project_manager": "PM", "engineering_lead": "EL", "product_manager": "Product", "qa": "QA",
            "judge": "Judge", "build_engineer": "Build", "reviewer": "Reviewer", "verifier": "Verifier",
            "engineer": "Eng"})
        self.assertTrue(all(len(t) <= 8 for t in mod.SEAT_TITLES.values()))

    def test_team_show_lists_the_titles_of_the_effective_team(self):
        rc, out, err = self.team("--show")
        self.assertEqual(rc, 0, err)
        line = next(l for l in out.splitlines() if l.startswith("titles"))
        for pair in ("engineering_lead=EL", "qa=QA", "engineer=Eng", "judge=Judge", "product_manager=Product"):
            self.assertIn(pair, line)
        self.assertNotIn("build_engineer", line)    # not in this team
        self.team("--add", "build_engineer")
        self.assertIn("build_engineer=Build", self.team("--show")[1])

    def test_the_skill_text_tags_every_seat_and_tells_el_to_keep_titles_current(self):
        from swarm import paths
        skill = paths.PLUGIN_ROOT / "skills" / "engineering-team"
        hosts = (skill / "references" / "hosts.md").read_text()
        for tag in ("[swarm title: EL]", "[swarm title: Eng: board view]"):
            self.assertIn(tag, hosts)
        text = (skill / "SKILL.md").read_text()
        self.assertIn("swarm title", text)
        self.assertIn("titles", (skill / "references" / "team-roles.md").read_text())

    def test_the_coding_recipe_titles_the_pipeline_seats(self):
        import importlib.util
        from swarm import paths
        spec = importlib.util.spec_from_file_location(
            "team_plugin_t2", paths.PLUGIN_ROOT / "skills" / "engineering-team" / "swarm_plugin.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.PIPELINE_TITLES["judge"], "Judge")
        self.assertTrue(mod.PIPELINE_TITLES["integrator"].startswith("Eng"))
        self.assertTrue(mod.PIPELINE_TITLES["worker"].startswith("Eng"))


if __name__ == "__main__":
    unittest.main()
