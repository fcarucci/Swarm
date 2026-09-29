"""Hindsight documents for memory provenance: existence checks and the (capability-gated)
metadata patch. Fake server only; the real 0.8.6 schema is a committed fixture."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from test_hindsight import HindsightEnv  # noqa: E402  (sets sys.path, skips without loopback)

from swarm import hindsight  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "hindsight" / "0.8.6" / "openapi-UpdateDocumentRequest.json"


class DocumentTests(HindsightEnv):
    def setUp(self):
        super().setUp()
        self.enable()
        self.client = hindsight.Client(self.cfg)
        self.client.retain("notes", "a fact", ["t"], {"source": "claude-code-session", "host": "x"},
                           document_id="doc-1")

    def test_document_found_and_missing(self):
        d = self.client.document("notes", "doc-1")
        self.assertEqual((d["id"], d["document_metadata"]["source"]), ("doc-1", "claude-code-session"))
        self.assertIsNone(self.client.document("notes", "nope"))
        self.assertIsNone(self.client.document("no-such-bank", "doc-1"))

    def test_real_0_8_6_schema_has_no_metadata_patch(self):
        self.assertFalse(hindsight.metadata_patch_in(json.loads(FIX.read_text())))

    def test_schema_with_metadata_is_detected(self):
        self.fake.metadata_patch = True
        self.assertTrue(hindsight.metadata_patch_in(self.client.openapi()))

    def test_refresh_caps_then_supported_reads_the_cache_only(self):
        self.fake.metadata_patch = True
        self.assertTrue(hindsight.refresh_caps(self.cfg))
        self.assertEqual(os.stat(hindsight.caps_path(self.cfg)).st_mode & 0o777, 0o600)
        before = len(self.fake.requests)
        self.assertTrue(hindsight.metadata_patch_supported(self.cfg))
        self.assertEqual(len(self.fake.requests), before)

    def test_supported_is_false_without_a_fresh_cache(self):
        self.assertFalse(hindsight.metadata_patch_supported(self.cfg))
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        old = time.time() - hindsight.CAPS_TTL - 10
        os.utime(hindsight.caps_path(self.cfg), (old, old))
        self.assertFalse(hindsight.metadata_patch_supported(self.cfg))

    def test_patch_merges_the_documents_own_metadata(self):
        self.fake.metadata_patch = True
        self.client.patch_document_metadata("notes", "doc-1", {"swarm_job": "J"})
        [req] = self.fake.calls("PATCH", "/documents/doc-1")
        self.assertEqual(req["body"], {"metadata": {"source": "claude-code-session", "host": "x", "swarm_job": "J"}})

    def test_patch_of_a_missing_document_is_a_404(self):
        self.fake.metadata_patch = True
        with self.assertRaises(hindsight.HindsightError) as cm:
            self.client.patch_document_metadata("notes", "nope", {"swarm_job": "J"})
        self.assertEqual(cm.exception.status, 404)

    def test_a_planted_cache_link_is_not_trusted(self):
        """The hook trusts the cache, so it is read through safefs: a symlink or hard link named
        like it counts as no cache."""
        self.fake.metadata_patch = True
        hindsight.refresh_caps(self.cfg)
        p = hindsight.caps_path(self.cfg)
        real = p.with_name("elsewhere.json")
        os.replace(p, real)
        os.symlink(real, p)
        self.assertFalse(hindsight.metadata_patch_supported(self.cfg))
        p.unlink()
        os.link(real, p)
        self.assertFalse(hindsight.metadata_patch_supported(self.cfg))

    def test_only_a_literal_true_in_the_cache_counts(self):
        from swarm import paths, safefs
        p = hindsight.caps_path(self.cfg)
        for value in (1, "yes", "true", [True], {"x": 1}, None, False):
            with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:
                safefs.write_atomic(d, p.name, json.dumps({"metadata_patch": value}))
            self.assertFalse(hindsight.metadata_patch_supported(self.cfg), repr(value))
        with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:   # positive control
            safefs.write_atomic(d, p.name, json.dumps({"metadata_patch": True}))
        self.assertTrue(hindsight.metadata_patch_supported(self.cfg))
        for junk in ("[]", "null", "not json", '"metadata_patch"', "{" * 5000):
            with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:
                safefs.write_atomic(d, p.name, junk)
            self.assertFalse(hindsight.metadata_patch_supported(self.cfg), junk[:20])

    def test_malformed_openapi_documents_mean_no_metadata_patch(self):
        def doc(props):
            return {"components": {"schemas": {"UpdateDocumentRequest": {"properties": props}}}}
        for bad in (None, [], "metadata", {}, {"components": None}, {"components": []},
                    {"components": {"schemas": "metadata"}},
                    {"components": {"schemas": {"UpdateDocumentRequest": ["metadata"]}}},
                    doc(["metadata"]), doc("metadata"), doc(None), doc({"tags": {}})):
            self.assertFalse(hindsight.metadata_patch_in(bad), repr(bad))
        self.assertTrue(hindsight.metadata_patch_in(doc({"metadata": {}})))
