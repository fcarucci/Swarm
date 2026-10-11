"""Docs and skills name only the driver notices the code defines (swarm.supervisor.driver_events)."""
from __future__ import annotations

import re
import unittest

from support import ROOT
from swarm.supervisor import driver_events

NAME = re.compile(r"\bDRIVER-[A-Z]+(?:-[A-Z]+)*\b")
DOCS = ["docs/REFERENCE.md", "docs/superpowers/specs/2026-10-10-swarm-autonomy-design.md",
        "skills/swarm/SKILL.md", "skills/engineering-team/SKILL.md"]


class DriverEventNames(unittest.TestCase):
    def test_names_are_unique_and_well_formed(self):
        self.assertEqual(len(set(driver_events.ALL)), len(driver_events.ALL))
        for n in driver_events.ALL:
            self.assertRegex(n, NAME)

    def test_docs_mention_only_defined_names(self):
        known = set(driver_events.ALL)
        for rel in DOCS:
            path = ROOT / rel
            if not path.exists():
                continue
            unknown = sorted(set(NAME.findall(path.read_text(encoding="utf-8"))) - known)
            self.assertEqual(unknown, [], f"{rel} names DRIVER-* notices the code does not define")

    def test_lib_posts_no_undefined_literal(self):
        known = set(driver_events.ALL)
        for path in (ROOT / "lib" / "swarm").rglob("*.py"):
            if path.name == "driver_events.py":
                continue
            unknown = sorted(set(NAME.findall(path.read_text(encoding="utf-8"))) - known)
            self.assertEqual(unknown, [], f"{path} uses DRIVER-* names outside driver_events")


if __name__ == "__main__":
    unittest.main()
