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
    ("activate --goal needs supervise enable", r"supervise enable"),
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
        for key in DEFAULT_CFG:
            self.assertIn(key, self.example, key)
            self.assertIn(f"`{key}`", self.ref, key)

    def test_extra_drive_keys_documented(self):
        for key in ("codex_usd_per_mtok", "allowed_models", "credential_helpers", "ruling_reserve_usd"):
            self.assertIn(key, self.example, key)
            self.assertIn(f"`{key}`", self.ref, key)

    def test_example_values_equal_the_shipped_defaults(self):
        from swarm.drive_activate import DEFAULT_ALLOWED_MODELS, ROLE_MODELS
        from swarm.supervisor.driver_model import DEFAULT_CFG
        drive = tomllib.loads(self.example).get("drive")
        self.assertIsInstance(drive, dict, "config.example.toml needs a [drive] table")
        for key, want in DEFAULT_CFG.items():
            if key not in NOT_CONFIG:
                self.assertEqual(drive.get(key), want, key)
        self.assertEqual(drive.get("codex_usd_per_mtok"), 10)
        self.assertEqual(drive.get("models"), ROLE_MODELS)
        self.assertEqual(tuple(drive.get("allowed_models")), DEFAULT_ALLOWED_MODELS)

    def test_models_default_to_haiku_for_engineers(self):
        drive = tomllib.loads(self.example)["drive"]["models"]
        self.assertEqual({r: drive[r] for r in ("engineer", "worker", "integrator")}, dict.fromkeys(("engineer", "worker", "integrator"), "haiku"))
        self.assertEqual({r: drive[r] for r in ("reviewer", "qa")}, {"reviewer": "sonnet", "qa": "sonnet"})
        self.assertEqual({r: drive[r] for r in ("judge", "lead", "security")}, dict.fromkeys(("judge", "lead", "security"), "opus"))

    def test_caps_are_presented_as_configurable_defaults_not_limits(self):
        cost = next(l for l in self.example.splitlines() if l.startswith("max_cost_usd"))
        reserve = next(l for l in self.example.splitlines() if l.startswith("ruling_reserve_usd"))
        for line in (cost, reserve):
            self.assertRegex(line, r"(?i)shipped default")
            self.assertRegex(line, r"(?i)configurable")
        body = heading_body(self.ref, "Drive mode")
        self.assertRegex(body, r"(?is)`max_cost_usd`.{0,200}configurable")
        self.assertRegex(body, r"--max-cost-usd")
        self.assertRegex(body, r"drive budget")

    def test_team_example_points_drive_jobs_at_the_driver(self):
        text = TEAM_EXAMPLE.read_text()
        self.assertRegex(text, r"(?i)drive")
        self.assertIn("swarm land", text)
        tomllib.loads(text)


class DriveReferenceSection(unittest.TestCase):
    def setUp(self):
        self.body = heading_body(REFERENCE.read_text(), "Drive mode")

    def test_section_exists_and_covers_the_contract(self):
        self.assertTrue(self.body, "docs/REFERENCE.md needs a 'Drive mode' section")
        for needle in ("MET", "UNREACHABLE", "CANCELLED", "GOAL-READY", "swarm seat request", "requester",
                       "--no-drive", "supervise enable", "[drive.models]", "--model", "swarm land",
                       "swarm halt", "swarm-record:"):
            self.assertIn(needle, self.body, needle)

    def test_breaking_changes_listed(self):
        for name, pattern in BREAKING:
            self.assertRegex(self.body, "(?is)" + pattern, name)

    def test_credential_pinning_unc_and_flagged_audit_explained(self):
        for pattern in (r"--credential", r"absolute", r"known_hosts", r"UNC", r"flagged", r"git (version )?2\.40|2\.40"):
            self.assertRegex(self.body, "(?i)" + pattern, pattern)

    def test_haiku_review_gates_and_never_tmp(self):
        self.assertRegex(self.body, r"(?is)haiku.{0,300}(gate|strict)|(gate|strict).{0,300}haiku")
        self.assertIn("/tmp", self.body)


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

    def test_non_drive_material_is_labelled_not_deleted(self):
        text = (SKILL_DIR / "SKILL.md").read_text()
        self.assertRegex(text, r"(?m)^#{2,3} .*[Nn]on-drive jobs")
        hosts = (SKILL_DIR / "references" / "hosts.md").read_text()
        self.assertRegex(hosts, r"(?m)^#{2,3} .*[Nn]on-drive jobs")
        self.assertIn("spawn_agent", hosts)          # the Codex procedure survives, under that heading
        pm = (SKILL_DIR / "references" / "pm-events.md").read_text()
        self.assertRegex(pm, r"(?is)non-drive jobs only|only (for|in) non-drive jobs")

    def test_engineering_skill_states_the_four_breaking_changes_and_seat_requests(self):
        text = (SKILL_DIR / "SKILL.md").read_text()
        for name, pattern in BREAKING:
            self.assertRegex(text, "(?is)" + pattern, name)
        for needle in ("swarm seat request", "requester/observer", "--no-drive", "GOAL-READY", "UNREACHABLE",
                       "swarm ci wait", "--timeout 540", "test-slot", "/tmp"):
            self.assertIn(needle, text, needle)

    def test_swarm_skill_drive_section_covers_seat_review_land_halt(self):
        body = heading_body(SWARM_SKILL.read_text(), "Drive mode")
        for needle in ("swarm seat request", "swarm review", "swarm land", "swarm halt", "swarm unhalt",
                       "--no-drive", "supervise enable", "--default", "swarm goal-ready", "[drive.models]"):
            self.assertIn(needle, body, needle)

    def test_hosts_and_recovery_describe_the_driver(self):
        hosts = (SKILL_DIR / "references" / "hosts.md").read_text()
        rec = (SKILL_DIR / "references" / "recovery.md").read_text()
        self.assertIn("swarm seat request", hosts)
        self.assertRegex(rec, r"(?i)swarm drive|driver")
        self.assertRegex(rec, r"(?i)swarm halt")


class ChangelogDraft(unittest.TestCase):
    def test_draft_lists_four_breaking_changes_and_features(self):
        text = CHANGELOG_DRAFT.read_text()
        self.assertTrue(text.startswith("## [Unreleased]") or "### Breaking" in text)
        breaking = text.split("### Breaking", 1)[1].split("\n### ", 1)[0]
        for name, pattern in BREAKING:
            self.assertRegex(breaking, "(?is)" + pattern, name)
        for needle in ("[drive.models]", "max_cost_usd", "swarm land", "swarm seat request"):
            self.assertIn(needle, text, needle)
        self.assertNotIn("gh run watch", text)


if __name__ == "__main__":
    unittest.main()
