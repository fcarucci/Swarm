"""Drive mode documentation (Task 14): the shipped `[drive]` defaults, the "Drive mode" reference
section, and the engineering-team / swarm skills that must never tell the requester to spawn agents
in a drive job (staffing goes through `swarm seat request`)."""
from __future__ import annotations

import re
import tomllib
import unittest

from support import ROOT

EXAMPLE = ROOT / "config.example.toml"
REFERENCE = ROOT / "docs" / "REFERENCE.md"
TEAM_EXAMPLE = ROOT / "team.example.toml"
CHANGELOG_DRAFT = ROOT / "docs" / "superpowers" / "plans" / "drive-changelog-draft.md"
SKILL_DIR = ROOT / "skills" / "engineering-team"
SKILL_FILES = [SKILL_DIR / "SKILL.md"] + [SKILL_DIR / "references" / n for n in
                                          ("hosts.md", "pm-events.md", "recovery.md", "team-roles.md")]
SWARM_SKILL = ROOT / "skills" / "swarm" / "SKILL.md"

# Recipe keys come from the owner record at activation (`--recipe`), not from config.toml.
NOT_CONFIG = {"recipe", "review_role"}
BREAKING = (
    ("goal implies drive", r"goal implies drive"),
    ("--default required", r"--default"),
    ("requester spawns refused", r"requester.{0,80}(refus|no longer)|(refus|no longer).{0,80}requester"),
    ("no timer: the session driver", r"session driver"),
)


def sections(text: str):
    """(heading, level, body-lines) for each markdown heading; text before the first has heading ''."""
    out, head, level, body, fence = [], "", 0, [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            out.append((head, level, body))
            head, level, body = m.group(2).strip(), len(m.group(1)), []
        else:
            body.append(line)
    out.append((head, level, body))
    return out


def drive_text(text: str) -> list[tuple[int, str]]:
    """The (line number, line) pairs that are not inside a heading whose title says "non-drive", nor
    inside one of its sub-headings."""
    keep, skip_level = [], None
    for head, level, body in sections(text):
        if skip_level is not None and level and level <= skip_level:
            skip_level = None
        if skip_level is None and level and "non-drive" in head.lower():
            skip_level = level
        if skip_level is None:
            keep.extend((0, line) for line in ([f"# {head}"] if head else []) + body)
    return keep


SPAWN = re.compile(r"spawn_agent|Agent[- ]tool|\broot[- ]spawns?\b|\bspawn(?:s|ed|ing)?\b", re.I)
REFUSAL = re.compile(r"\b(never|not|refus\w*|cannot|can't|no longer|instead|without)\b", re.I)


def spawn_violations(lines) -> list[str]:
    """Lines that tell the reader to spawn agents (no refusal word on the line)."""
    return [line for _, line in lines if SPAWN.search(line) and not REFUSAL.search(line)]


def heading_body(text: str, title: str) -> str:
    """The text under the first heading starting with `title`, sub-sections included."""
    out, level = [], None
    for head, lvl, body in sections(text):
        if level is None:
            if lvl and head.lower().startswith(title.lower()):
                level = lvl
                out.extend(body)
        elif lvl and lvl <= level:
            break
        else:
            out.extend(([f"{'#' * lvl} {head}"] if head else []) + body)
    return "\n".join(out)


class DriveDefaultsDocumented(unittest.TestCase):
    def setUp(self):
        self.example = EXAMPLE.read_text()
        self.ref = REFERENCE.read_text()

    def test_drive_defaults_documented(self):
        from swarm.supervisor.driver_model import DEFAULT_CFG
        from swarm.supervisor.driver_life import DEFAULTS as LIFE
        for key in {**DEFAULT_CFG, **LIFE}:
            self.assertIn(key, self.example, key)
            self.assertIn(f"`{key}`", self.ref, key)

    def test_reference_table_values_match_defaults(self):
        from swarm.supervisor.driver_life import DEFAULTS as LIFE
        from swarm.supervisor.driver_model import DEFAULT_CFG
        rows = {}
        for line in heading_body(self.ref, "Drive mode").splitlines():
            m = re.match(r"^\| `(\w+)` \| (.*?) \| (.*?) \|$", line)
            if m:
                rows[m.group(1)] = (m.group(2).strip(), m.group(3).strip())
        for key, want in {**DEFAULT_CFG, **LIFE}.items():
            if key in NOT_CONFIG:
                continue
            self.assertIn(key, rows, key)
            cell = rows[key][0].replace("`", "")
            got = tomllib.loads(f"v = {cell.split(' (')[0].split(',')[0].strip()}")["v"]
            self.assertEqual(got, want, key)
        self.assertIn("DRIVER-ERROR", rows["driver_error_passes"][1])
        self.assertIn("ruling", rows["driver_error_passes"][1])
        self.assertIn("ruling", rows["max_sessions"][1])
        self.assertIn("applied", rows["question_expiry"][1])
        for key, (_, exhaustion) in rows.items():
            self.assertTrue(exhaustion, key)

    def test_example_values_equal_the_shipped_defaults(self):
        from swarm.drive_activate import DEFAULT_ALLOWED_MODELS, ROLE_MODELS
        from swarm.supervisor.driver_model import DEFAULT_CFG
        drive = tomllib.loads(self.example).get("drive")
        self.assertIsInstance(drive, dict, "config.example.toml needs a [drive] table")
        from swarm.supervisor.driver_life import DEFAULTS as LIFE
        for key, want in {**DEFAULT_CFG, **LIFE}.items():
            if key not in NOT_CONFIG:
                self.assertEqual(drive.get(key), want, key)
        self.assertEqual(drive.get("codex_usd_per_mtok"), 10)
        self.assertEqual(drive.get("models"), ROLE_MODELS)
        self.assertEqual(tuple(drive.get("allowed_models")), DEFAULT_ALLOWED_MODELS)


class SkillsNeverTellRequesterToSpawn(unittest.TestCase):
    def test_checker_flags_instructions_and_accepts_refusals(self):
        bad = ["Spawn the engineers in waves.", "PM calls `spawn_agent` with a role-prefixed task name.",
               "Schedule its requested roles through root spawns.", "Use the Agent tool to start a reviewer."]
        good = ["The requester never spawns agents: request seats with `swarm seat request`.",
                "Agent-tool spawns by the requester are refused in drive jobs.", "No staffing text here."]
        self.assertEqual(spawn_violations([(0, l) for l in bad]), bad)
        self.assertEqual(spawn_violations([(0, l) for l in good]), [])

    def test_drive_text_skips_only_non_drive_sections(self):
        doc = "# T\nkeep one spawn\n## Non-drive jobs\nspawn here\n### sub\nspawn too\n## Drive\nspawn kept\n"
        kept = [l for _, l in drive_text(doc)]
        self.assertIn("keep one spawn", kept)
        self.assertIn("spawn kept", kept)
        self.assertNotIn("spawn here", kept)
        self.assertNotIn("spawn too", kept)

    def test_skills_never_tell_requester_to_spawn_in_drive_jobs(self):
        for path in SKILL_FILES:
            bad = spawn_violations(drive_text(path.read_text()))
            self.assertEqual(bad, [], f"{path.name}: spawn instructions outside a 'Non-drive jobs' section")
        swarm_drive = heading_body(SWARM_SKILL.read_text(), "Drive mode")
        self.assertTrue(swarm_drive, "skills/swarm/SKILL.md needs a 'Drive mode' section")
        self.assertEqual(spawn_violations([(0, l) for l in swarm_drive.splitlines()]), [])


if __name__ == "__main__":
    unittest.main()
