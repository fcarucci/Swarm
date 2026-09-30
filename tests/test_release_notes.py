"""scripts/release-notes.sh: a release's notes come from CHANGELOG.md's section for that tag,
read as it was at the tag; a tag with no section is an error, not a silent commit dump."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import ROOT

SCRIPT = ROOT / "scripts" / "release-notes.sh"

CHANGELOG = """# Changelog

## [0.2.0] - 2026-02-01

### Added
- Second thing.

## [0.1.0] - 2026-01-01

First release.
"""


class ReleaseNotesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="relnotes-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "scripts").mkdir()
        shutil.copy(SCRIPT, self.tmp / "scripts" / "release-notes.sh")
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", *args], cwd=self.tmp, check=True, env=env, capture_output=True)

    def commit(self, msg, changelog=None):
        (self.tmp / "f.txt").write_text(msg)
        if changelog is not None:
            (self.tmp / "CHANGELOG.md").write_text(changelog)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def notes(self, tag, **env):
        return subprocess.run(["bash", "scripts/release-notes.sh", tag], cwd=self.tmp, capture_output=True,
                              text=True, env=dict(os.environ, **env))

    def test_notes_are_the_tags_section_only(self):
        self.commit("first", CHANGELOG)
        self.git("tag", "v0.1.0")
        self.commit("second")
        self.git("tag", "v0.2.0")
        r = self.notes("v0.2.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        body = r.stdout.split("## What's changed", 1)[1]
        self.assertIn("- Second thing.", body)
        self.assertNotIn("First release.", body)
        self.assertNotIn("0.2.0]", body)
        self.assertIn("Full changelog:", body)

    def test_the_file_is_read_as_it_was_at_the_tag(self):
        self.commit("first", CHANGELOG.replace("Second thing.", "Old wording."))
        self.git("tag", "v0.2.0")
        self.commit("later edit", CHANGELOG)
        self.assertIn("Old wording.", self.notes("v0.2.0").stdout)

    def test_a_tag_without_an_entry_fails(self):
        self.commit("first", CHANGELOG)
        self.git("tag", "v0.3.0")
        r = self.notes("v0.3.0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no '## [0.3.0]' section", r.stderr)

    def test_commit_list_is_still_available_on_request(self):
        self.commit("feat: something", CHANGELOG)
        self.git("tag", "v0.3.0")
        r = self.notes("v0.3.0", RELEASE_NOTES_COMMITS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("feat: something", r.stdout)


if __name__ == "__main__":
    unittest.main()
