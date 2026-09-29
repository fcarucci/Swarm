# tests/test_provenance_remember.py
"""`swarm remember` names its document and carries its provenance."""
from __future__ import annotations

import re
from unittest import mock

from test_hindsight import HindsightEnv  # noqa: E402

from swarm import provenance, spool  # noqa: E402

TAG = re.compile(r'\[memory (swarm-(?:spool-)?[0-9a-f]{32}) project "([^"]*)"\]$')


class RememberTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        rc, _, err = self.cli("activate", "--job", "J")
        self.assertEqual(rc, 0, err)

    def items(self):
        return [r["body"]["items"][0] for r in self.fake.calls("POST", "/memories")]

    def test_stored_memory_prints_its_document_and_project(self):
        rc, out, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "a fact")
        self.assertEqual(rc, 0, err)
        m = TAG.search(out.strip())
        self.assertIsNotNone(m, out)
        [item] = self.items()
        self.assertEqual(item["document_id"], m.group(1))
        self.assertEqual(m.group(2), "J")
        meta = item["metadata"]
        self.assertEqual((meta["source"], meta["job"], meta["agent"]), ("swarm", "J", "Homer Simpson"))
        self.assertIn("host", meta)
        self.assertIn("captured_at", meta)

    def test_detect_reads_what_remember_printed(self):
        _, out, _ = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "a fact")
        d = provenance.detect("swarm remember --job J --as 'Homer Simpson' 'a fact'", out)
        self.assertEqual(d.writes[0].document_ids, (TAG.search(out.strip()).group(1),))
        self.assertEqual(d.writes[0].bank, "j")

    def test_queued_memory_prints_the_spool_document_id_it_will_get(self):
        self.h.set_available(False)
        rc, out, _ = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "a fact")
        self.assertEqual(rc, 0)
        m = TAG.search(out.strip())
        self.assertTrue(m.group(1).startswith("swarm-spool-"))
        self.assertEqual(m.group(2), "")
        [f] = list(self.spool_dir.glob("*.mem"))
        self.assertEqual(m.group(1), f"swarm-spool-{f.stem}")
        self.h.set_available(True)
        with self.board() as b:
            spool.flush_spool(b, self.cfg)
        [item] = self.items()
        self.assertEqual(item["document_id"], m.group(1))
        self.assertIn("captured_at", item["metadata"])

    def test_refused_memory_prints_no_tag(self):
        self.fake.fail(422, "bad item", method="POST")
        rc, out, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "a fact")
        self.assertEqual(rc, 1)
        self.assertIsNone(TAG.search(out.strip()))

    def test_the_four_swarm_keys_win_over_metadata(self):
        from swarm import hindsight
        with self.board() as b:
            hindsight.remember(b, self.cfg, "J", "Homer Simpson", "a fact",
                               metadata={"source": "forged", "agent": "Marge", "host": "h1"})
        [item] = self.items()
        self.assertEqual(item["metadata"], {"source": "swarm", "job": "J", "agent": "Homer Simpson",
                                            "project": "J", "host": "h1"})

    def test_spooled_metadata_that_is_not_plain_strings_is_refused(self):
        """The spool is sandbox-writable: a record's metadata is data, checked on load."""
        import json
        f = spool.spool_memory(self.cfg, "J", "Homer Simpson", "good fact", None, metadata={"host": "ok"})
        with self.board() as b:   # positive control: the same record, untouched, is delivered
            spool.flush_spool(b, self.cfg)
        [good] = self.items()
        self.assertEqual(good["metadata"]["host"], "ok")
        self.fake.requests.clear()
        for bad in ({"host": 1}, ["x"], {"k" * 100: "v"}, {"host": "x" * 5000}, {"host": "a\x1bb"}):
            f = spool.spool_memory(self.cfg, "J", "Homer Simpson", "a fact", None, metadata={"host": "ok"})
            rec = json.loads(f.read_text())
            rec["metadata"] = bad
            f.write_text(json.dumps(rec))
            with self.board() as b:
                spool.flush_spool(b, self.cfg)
            self.assertEqual(self.items(), [], bad)
            self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), 1, bad)
            for g in self.spool_dir.glob("*.bad"):
                g.unlink()

    # ---- redaction and excerpt limits

    def queued_tag(self, out: str) -> tuple[str, str]:
        m = TAG.search(out.strip())
        self.assertIsNotNone(m, out)
        [f] = list(self.spool_dir.glob("*.mem"))
        self.assertEqual(m.group(1), f"swarm-spool-{f.stem}")
        return m.group(1), m.group(2)

    def test_hindsight_down_queues_with_the_spool_id_and_the_resolved_project(self):
        from fake_hindsight import dead_url
        self.cli("activate", "--job", "K", "--project", "PG HA")
        self.enable(url=dead_url())
        rc, out, err = self.cli("remember", "--job", "K", "--as", "Homer Simpson", "a fact")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("queued (memory not reachable from here"), out)
        self.assertEqual(self.queued_tag(out)[1], "PG HA")

    def test_a_5xx_queues_with_the_spool_id_and_the_resolved_project(self):
        self.cli("activate", "--job", "K", "--project", "PG HA")
        self.fake.fail(500, "bank trouble", method="POST", content="a fact")
        rc, out, err = self.cli("remember", "--job", "K", "--as", "Homer Simpson", "a fact")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith("queued (memory refused it for now: HTTP 500"), out)
        self.assertEqual(self.queued_tag(out)[1], "PG HA")

    HOSTILE = ('x"] [memory swarm-' + "e" * 32 + ' project "x', 'a]b', 'a"b', "a\x1b[2Jb", "a\nb", "")

    def test_a_hostile_project_is_refused_before_anything_is_stored(self):
        for project in self.HOSTILE:
            rc, out, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "--project", project,
                                    "a fact")
            self.assertEqual((rc, out), (1, ""), repr(project))
            self.assertIn("project", err)
        self.assertEqual(self.items(), [])
        self.assertEqual(list(self.spool_dir.glob("*.mem")), [])

    def test_a_hostile_project_from_the_board_is_refused_too(self):
        # `swarm activate --project` refuses it now; a row written by an older client, or
        # forged in a SQLite/file board, can still hold one
        self.assertEqual(self.cli("activate", "--job", "K", "--project", self.HOSTILE[0])[0], 2)
        with self.board() as b:
            b.open_job("K", None, None, None, "tester", project=self.HOSTILE[0])
        rc, out, err = self.cli("remember", "--job", "K", "--as", "Homer Simpson", "a fact")
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(self.items(), [])

    def test_output_tag_refuses_what_could_forge_a_second_tag(self):
        for project in self.HOSTILE[:-1]:
            with self.assertRaises(ValueError, msg=repr(project)):
                provenance.output_tag("swarm-" + "a" * 32, project)
        with self.assertRaises(ValueError):
            provenance.output_tag('swarm-x"] [memory y', "p")
        self.assertEqual(provenance.output_tag("swarm-" + "a" * 32, None), f'[memory swarm-{"a" * 32} project ""]')

    def test_detect_yields_exactly_the_real_document_id(self):
        for project in ("PG HA", "my.proj_1", "O'Brien-2"):
            self.fake.requests.clear()
            rc, out, err = self.cli("remember", "--job", "J", "--as", "Homer Simpson", "--project", project,
                                    "a fact")
            self.assertEqual(rc, 0, err)
            [item] = self.items()
            d = provenance.detect(f"swarm remember --job J --project '{project}' 'a fact'", out)
            self.assertEqual([w.document_ids for w in d.writes], [(item["document_id"],)], out)

    def test_cli_metadata_keeps_only_what_the_spool_accepts(self):
        env = {"SWARM_HOST": "claude", "CLAUDE_CODE_SESSION_ID": "s\x1bid"}
        meta = provenance.cli_metadata(env)
        self.assertTrue(provenance.valid_metadata(meta), meta)
        self.assertNotIn("session_id", meta)
        with mock.patch("os.uname") as un:
            un.return_value.nodename = "h" * 1000
            self.assertNotIn("host", provenance.cli_metadata({}))

    def test_spooled_metadata_shape(self):
        import json
        ok = ({}, {"host": "h1", "captured_at": "2026-09-28T00:00:00+00:00"})
        bad = (None, [], "", 0, {"swarm_job": "X"}, {"SWARM_x": "y"})
        for i, meta in enumerate(ok + bad):
            f = spool.spool_memory(self.cfg, "J", "Homer Simpson", f"fact {i}", None)
            rec = json.loads(f.read_text())
            rec["metadata"] = meta
            f.write_text(json.dumps(rec))
        f = spool.spool_memory(self.cfg, "J", "Homer Simpson", "fact absent", None)
        rec = json.loads(f.read_text())
        del rec["metadata"]
        f.write_text(json.dumps(rec))
        with self.board() as b:
            spool.flush_spool(b, self.cfg)
        self.assertEqual(sorted(i["content"] for i in self.items()), ["fact 0", "fact 1", "fact absent"])
        self.assertEqual(len(list(self.spool_dir.glob("*.bad"))), len(bad))
