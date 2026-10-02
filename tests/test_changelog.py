"""CHANGELOG.md must describe the version being shipped: release notes are extracted from it by
scripts/release-notes.sh, and a tag whose version has no section fails to publish."""
from __future__ import annotations

import json
import re
import unittest

from support import ROOT

MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
HEADING = re.compile(r"^## \[([^\]]+)\](?: - (\S+))?\s*$", re.M)
FIX = (
    "To fix: bump `version` in BOTH .claude-plugin/plugin.json and .codex-plugin/plugin.json, "
    "and add a `## [x.y.z] - YYYY-MM-DD` section for it at the top of CHANGELOG.md "
    "(newest first, see README 'Development and releasing')."
)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


class ChangelogTest(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "CHANGELOG.md").read_text()
        # an optional undated "## [Unreleased]" section on top collects changes until the next release
        self.headings = [h for h in HEADING.findall(self.text) if h[0] != "Unreleased"]

    def test_plugin_versions_agree(self):
        versions = {m: json.loads((ROOT / m).read_text())["version"] for m in MANIFESTS}
        self.assertEqual(len(set(versions.values())), 1, f"plugin versions differ: {versions}. {FIX}")

    def test_current_version_has_a_section(self):
        version = json.loads((ROOT / MANIFESTS[0]).read_text())["version"]
        found = [v for v, _ in self.headings]
        self.assertIn(version, found, f"CHANGELOG.md has no '## [{version}]' section. {FIX}")

    def test_headings_are_dated_and_strictly_descending(self):
        self.assertTrue(self.headings, "CHANGELOG.md has no '## [x.y.z] - YYYY-MM-DD' sections")
        for v, date in self.headings:
            self.assertRegex(v, r"^\d+\.\d+\.\d+$", f"bad version heading [{v}]")
            self.assertTrue(date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date),
                            f"section [{v}] must be dated '## [{v}] - YYYY-MM-DD', got {date!r}")
        versions = [_version(v) for v, _ in self.headings]
        for newer, older in zip(versions, versions[1:]):
            self.assertGreater(newer, older, "CHANGELOG.md sections must be newest first, strictly descending")


if __name__ == "__main__":
    unittest.main()
