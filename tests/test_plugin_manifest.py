"""The plugin's manifests and hook files are consistent."""
from __future__ import annotations

import json
import re
import unittest

from support import ROOT  # noqa: F401

HOOK_EVENTS = {"SessionStart": ("session-start", 10, None), "SubagentStart": ("start", 15, None),
               "PreToolUse": ("turn", 10, "*"), "PostToolUse": ("done", 10, "*"),
               "Stop": ("session-stop", 10, None), "SubagentStop": ("stop", 10, None)}


class ManifestTests(unittest.TestCase):
    def load(self, rel):
        return json.loads((ROOT / rel).read_text())

    def test_claude_manifest_and_marketplace(self):
        m = self.load(".claude-plugin/plugin.json")
        self.assertEqual(m["name"], "swarm")
        self.assertRegex(m["version"], r"^\d+\.\d+\.\d+$")
        mk = self.load(".claude-plugin/marketplace.json")
        self.assertEqual(mk["name"], "swarm")
        self.assertEqual([p["name"] for p in mk["plugins"]], ["swarm"])
        self.assertIn("owner", mk)

    def test_no_root_plugin_json(self):
        self.assertFalse((ROOT / "plugin.json").exists())   # Codex would prefer it over .codex-plugin

    def test_claude_hooks(self):
        hooks = self.load("hooks/hooks.json")["hooks"]
        self.assertEqual(set(hooks), set(HOOK_EVENTS))
        for event, (arg, timeout, matcher) in HOOK_EVENTS.items():
            (group,) = hooks[event]
            self.assertEqual(group.get("matcher"), matcher)
            (h,) = group["hooks"]
            self.assertEqual(h["command"], f'"${{CLAUDE_PLUGIN_ROOT}}/bin/swarm-hook" --host claude {arg}')
            self.assertEqual(h["timeout"], timeout)

    def test_skill_location_and_paths(self):
        self.assertFalse((ROOT / "SKILL.md").exists())
        text = (ROOT / "skills/swarm/SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: swarm\n"))
        self.assertNotIn("~/.claude/skills/swarm", text)
        # the reference doc must name the old install dir (migrate section), just never as the command path
        self.assertNotIn("~/.claude/skills/swarm/bin/swarm", (ROOT / "docs/REFERENCE.md").read_text())

    def test_plugin_version_helper(self):
        from swarm import paths
        self.assertEqual(paths.plugin_version(), self.load(".claude-plugin/plugin.json")["version"])

    def test_no_ips_in_shipped_text(self):
        ip = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
        for rel in ("README.md", "docs/REFERENCE.md", "skills/swarm/SKILL.md", "config.example.toml"):
            # loopback in docs/examples is not a host address (README/REFERENCE and SKILL.md mention 127.0.0.1)
            self.assertIsNone(ip.search((ROOT / rel).read_text().replace("127.0.0.1", "")), rel)

    def test_codex_manifest_and_hooks(self):
        c = self.load(".codex-plugin/plugin.json")
        self.assertEqual((c["name"], c["version"]), ("swarm", self.load(".claude-plugin/plugin.json")["version"]))
        self.assertEqual(c["hooks"], "./hooks/codex-hooks.json")
        hooks = self.load("hooks/codex-hooks.json")["hooks"]
        self.assertEqual(set(hooks), set(HOOK_EVENTS))
        for event, (arg, timeout, matcher) in HOOK_EVENTS.items():
            (group,) = hooks[event]
            self.assertEqual(group.get("matcher"), matcher)
            (h,) = group["hooks"]
            self.assertEqual(h["command"], f'"${{PLUGIN_ROOT}}/bin/swarm-hook" --host codex {arg}')
            self.assertEqual(h["timeout"], timeout)
            if event in ("SubagentStart", "PreToolUse"):
                self.assertEqual(h["additionalContextLimit"], 8000)


class DocsTests(unittest.TestCase):
    def test_docs_cover_both_hosts(self):
        skill = (ROOT / "skills/swarm/SKILL.md").read_text()
        for needle in ("spawn_agent", "`message`", "--attach", "${CLAUDE_PLUGIN_ROOT}/bin/swarm doctor", "swarm command:",
                       "[swarm role: verifier]", "agents.max_depth = 2"):
            self.assertTrue(needle in skill, needle)
        readme = (ROOT / "docs/REFERENCE.md").read_text()
        for needle in ("swarm migrate", "swarm doctor", "/hooks", "stop_quiet_minutes", "[models]", "web search"):
            self.assertTrue(needle in readme, needle)
        example = (ROOT / "config.example.toml").read_text()
        for needle in ("[models]", "[models.claude]", "[codex]", "stop_quiet_minutes"):
            self.assertTrue(needle in example, needle)

    def test_docs_name_every_cli_command(self):
        """Every `swarm` subcommand is in both command references (hook is internal: reference doc only)."""
        import argparse
        from swarm import cli
        sub = next(a for a in cli._parser()._actions if isinstance(a, argparse._SubParsersAction))
        def rows(rel):   # the command reference tables' rows
            return [line for line in (ROOT / rel).read_text().splitlines() if line.startswith("| `")]
        skill, reference = rows("skills/swarm/SKILL.md"), rows("docs/REFERENCE.md")
        for name in sub.choices:
            self.assertTrue(any(f"`{name}" in row for row in reference), name)
            if name != "hook":
                self.assertTrue(any(f"`{name}" in row for row in skill), name)
