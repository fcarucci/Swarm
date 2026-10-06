"""Memory refs follow their memory: `swarm purge` drops a ref once Hindsight says its document is
gone, never on doubt, and `swarm memory refs --check` says where each one stands."""
from __future__ import annotations

import datetime as dt
import time
from unittest import mock

from support import assert_finishes
from test_hindsight import HindsightEnv  # noqa: E402
from fake_hindsight import dead_url  # noqa: E402

from swarm import hindsight, provenance  # noqa: E402
from swarm.board import MemoryRef  # noqa: E402


def mref(doc, bank="notes", **kw):
    return MemoryRef(**{**dict(document_id=doc, bank=bank, job="J", agent_key="a1", agent_name="Homer Simpson",
                               harness="claude", host="h", session_id="s", tool_call_id="t", writer="note-tool"),
                        **kw})


class PurgeTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.set_provenance("grace_days = 0")
        hindsight.Client(self.cfg).retain("notes", "kept fact", [], {}, document_id="alive", create_bank=True)
        with self.board() as b:
            for doc in ("alive", "gone"):
                b.save_memory_ref(mref(doc))
        self.fake.requests.clear()

    def set_provenance(self, *lines):
        """Replace the [provenance] block, kept just before [hindsight] (HindsightEnv.enable
        rewrites everything from [hindsight] on, and keeps what is before it)."""
        head, _, rest = self.config.read_text().partition("[hindsight]")
        head = head.split("[provenance]")[0]
        block = "[provenance]\n" + "\n".join(lines) + "\n"
        self.config.write_text(head + block + ("[hindsight]" + rest if rest else ""))
        from swarm import cli as swarm
        self.cfg = swarm.load_config(self.config)

    def no_url(self):
        """[hindsight] with url = "" (enable() would fall back to the fake's url)."""
        head = self.config.read_text().split("[hindsight]")[0]
        self.config.write_text(head + '[hindsight]\nurl = ""\n')
        from swarm import cli as swarm
        self.cfg = swarm.load_config(self.config)

    def docs(self):
        with self.board() as b:
            return {r.document_id: r for r in b.memory_refs()}

    # ---- swarm purge

    def test_purge_drops_refs_whose_document_is_gone(self):
        rc, out, err = self.cli("purge")
        self.assertEqual(rc, 0)
        self.assertEqual(out, "purged\n")
        refs = self.docs()
        self.assertEqual(set(refs), {"alive"})
        self.assertIsNotNone(refs["alive"].checked_at)
        self.assertIn("1 dropped", err)

    def test_purge_keeps_refs_within_grace(self):
        self.set_provenance("grace_days = 7")
        self.cli("purge")
        self.assertEqual(set(self.docs()), {"alive", "gone"})
        self.assertEqual(self.fake.calls("GET", "/documents/gone"), [])

    def test_purge_keeps_refs_when_hindsight_down(self):
        self.enable(url=dead_url())
        rc, _, err = self.cli("purge")
        self.assertEqual(rc, 0)
        self.assertEqual(set(self.docs()), {"alive", "gone"})
        self.assertIn("1 kept without a clear answer (Hindsight down or unsure), 1 left for a later purge", err)

    def test_purge_keeps_refs_on_a_server_error(self):
        self.fake.fail(500, "db down", method="GET")
        self.cli("purge")
        self.assertEqual(set(self.docs()), {"alive", "gone"})

    def test_purge_keeps_refs_on_a_404_that_is_not_hindsights_document_answer(self):
        """A 404 from a proxy, a wrong url path or another server is no proof the memory is gone."""
        self.fake.fail(404, "Not Found", method="GET", suffix="/documents/gone")
        rc, _, err = self.cli("purge")
        self.assertEqual(rc, 0)
        self.assertEqual(set(self.docs()), {"alive", "gone"})
        self.assertIn("1 kept without a clear answer", err)

    def test_purge_keeps_refs_when_the_url_is_not_hindsight(self):
        self.enable(url=self.fake.url + "/not-hindsight")
        self.cli("purge")
        self.assertEqual(set(self.docs()), {"alive", "gone"})

    def test_purge_keeps_refs_whose_bank_is_missing(self):
        """Hindsight answers "Document not found" in a bank that doesn't exist too; a missing bank
        can be a wrong or fresh server, so its refs stay (and aren't asked about one by one)."""
        with self.board() as b:
            b.save_memory_ref(mref("elsewhere", bank="no-such-bank"))
        self.cli("purge")
        self.assertEqual(set(self.docs()), {"alive", "elsewhere"})
        self.assertEqual(self.fake.calls("GET", "/documents/elsewhere"), [])

    def test_purge_without_hindsight_url_keeps_everything_and_says_so(self):
        self.no_url()
        rc, _, err = self.cli("purge")
        self.assertEqual(rc, 0)
        self.assertEqual(set(self.docs()), {"alive", "gone"})
        self.assertIn("[hindsight] url is empty", err)

    def test_recently_checked_refs_are_not_asked_again(self):
        self.cli("purge")
        self.fake.requests.clear()
        self.cli("purge")
        self.assertEqual(self.fake.calls("GET", "/documents/alive"), [])

    def test_purge_refreshes_the_capability_cache(self):
        self.cli("purge")
        self.assertTrue(self.fake.calls("GET", "/openapi.json"))

    def test_purge_lists_what_it_dropped_term_safe(self):
        """The dropped refs are listed with their job and agent, which are board data (a forged
        row's job can carry control characters): they come out as visible notation."""
        with self.board() as b:
            b.save_memory_ref(mref("gone", job="J\x1b[2J\x9b31m"))
        _, _, err = self.cli("purge")
        self.assertIn("dropped memory ref gone (job J\\x1b[2J\\x9b31m, agent Homer Simpson, bank notes)", err)
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\x9b", err)

    def test_purge_keeps_a_ref_re_recorded_while_it_ran(self):
        """The row read as gone is re-recorded (a new save under the same id) before the delete:
        the new row stays."""
        real = hindsight.Client.document

        def document(client, bank, doc):
            answer = real(client, bank, doc)
            if doc == "gone":
                with self.board() as b:
                    b.save_memory_ref(mref("gone", created_at=b.now() + dt.timedelta(seconds=1)))
            return answer
        with mock.patch.object(hindsight.Client, "document", document):
            _, _, err = self.cli("purge")
        self.assertEqual(set(self.docs()), {"alive", "gone"})
        self.assertIn("0 dropped", err)

    def test_prune_client_keeps_the_deadline(self):
        """Every question shares prune's deadline: a slow Hindsight can't hold purge longer."""
        self.enable(timeout_seconds=5)
        import threading
        entered, release = threading.Event(), threading.Event()
        self.fake.gates[("GET", "/documents/alive")] = (entered, release)
        self.addCleanup(release.set)
        with self.board() as b:
            res = assert_finishes(self, lambda: provenance.prune(b, self.cfg, deadline=time.monotonic() + 0.5))
        self.assertTrue(entered.wait(30))
        self.assertFalse(release.is_set())
        self.assertEqual(set(self.docs()), {"alive", "gone"})
        self.assertEqual(res.dropped, ())
        self.assertEqual(res.checked, 0)
        self.assertGreater(res.unknown + res.skipped, 0)

    def test_statuses_client_keeps_the_deadline(self):
        self.enable(timeout_seconds=5)
        import threading
        entered, release = threading.Event(), threading.Event()
        self.fake.gates[("GET", "/documents/alive")] = (entered, release)
        self.addCleanup(release.set)
        with self.board() as b:
            refs = b.memory_refs()
        out = assert_finishes(self, lambda: provenance.statuses(self.cfg, refs, deadline=time.monotonic() + 0.5))
        self.assertTrue(entered.wait(30))
        self.assertFalse(release.is_set())
        self.assertTrue(out["alive"].startswith("unknown"), out)

    # ---- memory refs --check

    def test_memory_refs_check_column(self):
        rc, out, _ = self.cli("memory", "refs", "--check")
        self.assertEqual(rc, 0)
        self.assertIn("HINDSIGHT", out)
        self.assertRegex(out, r"alive .*present")
        self.assertRegex(out, r"gone .*missing")
        self.assertEqual(set(self.docs()), {"alive", "gone"})   # --check never drops anything

    def test_memory_refs_check_without_url(self):
        self.no_url()
        rc, out, _ = self.cli("memory", "refs", "--check")
        self.assertEqual(rc, 0)
        self.assertIn("not checked ([hindsight] url is empty)", out)

    def test_memory_refs_check_with_hindsight_down(self):
        self.enable(url=dead_url())
        rc, out, _ = self.cli("memory", "refs", "--check")
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"gone .*unknown")
        self.assertNotRegex(out, r"missing")

    def test_memory_refs_check_on_a_foreign_404_is_unknown_not_missing(self):
        self.fake.fail(404, "Not Found", method="GET", suffix="/documents/gone")
        _, out, _ = self.cli("memory", "refs", "--check")
        self.assertRegex(out, r"gone .*unknown")

    # ---- status: doubtful claims

    def test_status_flags_a_document_written_before_the_reference(self):
        self.fake.set_document_time("notes", "alive", "2020-01-01T00:00:00+00:00")
        _, out, _ = self.cli("transcript", "show", "--memory", "alive")
        self.assertIn("before this reference: the claim may be wrong", out)

    def test_status_flags_a_document_first_written_well_after_the_reference(self):
        """Squatting: a forged claim on a predictable id, before anyone wrote that document."""
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        self.fake.set_document_time("notes", "alive", later)
        _, out, _ = self.cli("transcript", "show", "--memory", "alive")
        self.assertIn(f"first written {later}, after this reference: the claim may be wrong", out)

    def test_status_flags_a_document_rewritten_well_after_the_reference(self):
        later = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)).isoformat()
        self.fake.documents["notes"]["alive"]["updated_at"] = later
        _, out, _ = self.cli("transcript", "show", "--memory", "alive")
        self.assertIn(f"last written {later}, after this reference: the claim may be wrong", out)

    def test_status_of_a_fresh_write_is_plain_present(self):
        _, out, _ = self.cli("transcript", "show", "--memory", "alive")
        self.assertRegex(out, r"in Hindsight: present \(last written [^)]*\)\n")


class PruneUnitTests(HindsightEnv):
    """provenance.prune against a mock client: what counts as proof, deadlines, forged rows."""

    def setUp(self):
        super().setUp()
        self.enable()
        self.cfg = {**self.cfg, "provenance": {**self.cfg.get("provenance", {}), "grace_days": 0}}
        with self.board() as b:
            for doc in ("d1", "d2", "d3"):
                b.save_memory_ref(mref(doc))

    def client(self, answers):
        c = mock.Mock()
        c.bank_exists.return_value = True

        def document(bank, doc):
            a = answers[doc]
            if isinstance(a, BaseException):
                raise a
            return a
        c.document.side_effect = document
        return c

    def prune(self, client, **kw):
        with self.board() as b:
            res = provenance.prune(b, self.cfg, client=client, **kw)
            return res, {r.document_id for r in b.memory_refs()}

    def test_unavailable_stops_asking_and_drops_nothing(self):
        c = self.client({"d1": None, "d2": hindsight.HindsightUnavailable("down"), "d3": None})
        res, left = self.prune(c)
        self.assertEqual(left, {"d2", "d3"})   # d1: a definite 404 before the outage
        self.assertEqual(res.dropped, ("d1",))
        self.assertEqual((res.unknown, res.skipped), (1, 1))   # d2 asked, d3 left for later
        self.assertEqual([call.args[1] for call in c.document.call_args_list], ["d1", "d2"])

    def test_a_non_document_answer_is_unknown(self):
        c = self.client({"d1": ["not", "a", "document"], "d2": ValueError("not json"), "d3": {"id": "d3"}})
        res, left = self.prune(c)
        self.assertEqual(left, {"d1", "d2", "d3"})
        self.assertEqual((res.checked, res.dropped, res.unknown), (1, (), 2))

    def test_past_deadline_asks_nothing(self):
        c = self.client({"d1": None, "d2": None, "d3": None})
        res, left = self.prune(c, deadline=time.monotonic() - 1)
        self.assertEqual(left, {"d1", "d2", "d3"})
        c.document.assert_not_called()
        self.assertEqual((res.unknown, res.skipped), (0, 3))

    def test_check_max_caps_the_questions_bank_profiles_included(self):
        self.cfg["provenance"]["check_max"] = 2
        c = self.client({"d1": None, "d2": None, "d3": None})
        res, left = self.prune(c)
        self.assertEqual((c.bank_exists.call_count, c.document.call_count), (1, 1))
        self.assertEqual(left, {"d2", "d3"})
        self.assertEqual((res.checked, res.unknown, res.skipped), (1, 0, 2))

    def test_refs_of_a_missing_bank_are_marked_checked_and_kept(self):
        with self.board() as b:
            b.save_memory_ref(mref("m1", bank="gone-bank"))
        c = self.client({"d1": {"id": "d1"}, "d2": {"id": "d2"}, "d3": {"id": "d3"}})
        c.bank_exists.side_effect = lambda bank: bank != "gone-bank"
        res, left = self.prune(c)
        self.assertIn("m1", left)
        with self.board() as b:
            self.assertIsNotNone(b.memory_refs(document_id="m1")[0].checked_at)
        self.assertEqual((res.checked, res.kept_missing_bank, res.unknown), (4, 1, 0))

    def test_refs_of_a_missing_bank_do_not_starve_the_others(self):
        """They are the oldest, and asked about first; marked checked, the next run reaches the
        rest instead of asking about them again."""
        self.cfg["provenance"]["check_max"] = 2
        with self.board() as b:
            b.delete_memory_refs(["d1", "d2", "d3"])
            old = b.now() - dt.timedelta(days=30)
            for i in range(3):
                b.save_memory_ref(mref(f"m{i}", bank="gone-bank", created_at=old))
            b.save_memory_ref(mref("d1"))
        c = self.client({"d1": None})
        c.bank_exists.side_effect = lambda bank: bank != "gone-bank"
        res, left = self.prune(c)
        self.assertEqual(c.document.call_count, 0)   # profile gone-bank, profile notes: the cap
        self.assertEqual(res.skipped, 1)
        res, left = self.prune(c)
        self.assertEqual(left, {"m0", "m1", "m2"})
        self.assertEqual(res.dropped, ("d1",))

    def test_forged_row_ids_are_never_asked_or_dropped(self):
        with self.board() as b:
            b.save_memory_ref(mref("ok-id", bank="../x"))
            b.save_memory_ref(mref("../../etc", bank="notes"))
        c = self.client({"d1": None, "d2": None, "d3": None})
        res, left = self.prune(c)
        self.assertEqual(left, {"ok-id", "../../etc"})
        self.assertEqual(sorted(call.args[1] for call in c.document.call_args_list), ["d1", "d2", "d3"])

    def test_a_bank_profile_error_drops_nothing_in_that_bank(self):
        c = self.client({"d1": None, "d2": None, "d3": None})
        c.bank_exists.side_effect = hindsight.HindsightError("HTTP 500", 500)
        res, left = self.prune(c)
        self.assertEqual(left, {"d1", "d2", "d3"})
        c.document.assert_not_called()

    def test_statuses_stop_at_the_deadline(self):
        with self.board() as b:
            refs = b.memory_refs()
        out = provenance.statuses(self.cfg, refs, deadline=time.monotonic() - 1)
        self.assertEqual(set(out), {"d1", "d2", "d3"})
        self.assertTrue(all(v.startswith("unknown") for v in out.values()))


class DocumentAnswerTests(HindsightEnv):
    """Client.document: None only on Hindsight's own "Document not found" 404."""

    def setUp(self):
        super().setUp()
        self.enable()
        self.c = hindsight.Client(self.cfg)
        self.c.retain("notes", "a fact", [], {}, document_id="here", create_bank=True)

    def test_document_answers(self):
        self.assertEqual(self.c.document("notes", "here")["id"], "here")
        self.assertIsNone(self.c.document("notes", "not-here"))
        self.assertIsNone(self.c.document("no-such-bank", "x"))   # 0.8.6: the same "Document not found"

    def test_a_foreign_404_raises(self):
        self.fake.fail(404, "Not Found", method="GET", suffix="/documents/not-here")
        with self.assertRaises(hindsight.HindsightError) as cm:
            self.c.document("notes", "not-here")
        self.assertEqual(cm.exception.status, 404)
        self.enable(url=self.fake.url + "/elsewhere")
        with self.assertRaises(hindsight.HindsightError):
            hindsight.Client(self.cfg).document("notes", "not-here")
