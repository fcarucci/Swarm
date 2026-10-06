"""The plugin's manifests and hook files are consistent."""
from __future__ import annotations

import json
import io
import re
import subprocess
import tarfile
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

    def test_refactoring_skill_name_and_paths(self):
        skill = ROOT / "skills/refactoring"
        text = (skill / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: refactoring\n"))
        self.assertIn("Suggest Mode", text)
        self.assertIn("Apply Mode", text)
        prompt = (skill / "agents/openai.yaml").read_text()
        self.assertIn("Use $refactoring to refactor", prompt)
        for path in skill.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.relative_to(skill)):
                    self.assertNotIn("refactoring-fowler", path.read_text())
                    self.assertNotIn("~/.claude", path.read_text())
        # Loading the generator must resolve its outputs against the skill, regardless of cwd.
        import runpy
        namespace = runpy.run_path(str(skill / "scripts/generate_refactoring_contexts.py"))
        self.assertEqual(namespace["ROOT"], skill)
        self.assertEqual(namespace["OUT"], skill / "references/refactorings")

    def test_release_archive_ships_refactoring_skill(self):
        # Match release.yml: git archive applies export-ignore. The root /scripts exclusion
        # must never strip a skill's own scripts or reference/language context files.
        archive = subprocess.check_output(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as packaged:
            for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
                self.assertEqual(json.load(packaged.extractfile(manifest))["name"], "swarm")
            packaged.getmember("skills/engineering-team/SKILL.md")
            skill = ROOT / "skills/refactoring"
            for path in skill.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(ROOT).as_posix()
                    with self.subTest(path=rel):
                        self.assertEqual(packaged.extractfile(rel).read(), path.read_bytes())
                        if path.parent.name == "scripts":
                            self.assertTrue(packaged.getmember(rel).mode & 0o111)

    def test_complexity_analyzer_skill_ships_for_both_hosts(self):
        skill = ROOT / "skills/complexity-analyzer"
        text = (skill / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\nname: complexity-analyzer\n"))
        self.assertNotIn("~/.claude/skills/complexity-analyzer", text)
        self.assertFalse((skill / "docs").exists())
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        staged = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT).decode().split("\0")
        executable = {entry.split("\t", 1)[1] for entry in staged if entry.startswith("100755 ")}
        for rel in filter(None, tracked):
            self.assertTrue({"tools", "node_modules"}.isdisjoint(rel.split("/")), rel)
        archive = subprocess.check_output(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as packaged:
            for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
                self.assertEqual(json.load(packaged.extractfile(manifest))["name"], "swarm")
            for rel in tracked:
                if rel.startswith("skills/complexity-analyzer/") and not rel.endswith((".gitignore", ".gitattributes")):
                    self.assertEqual(packaged.extractfile(rel).read().replace(b"\r\n", b"\n"),
                                     (ROOT / rel).read_bytes().replace(b"\r\n", b"\n"), rel)
                    if rel in executable:
                        self.assertTrue(packaged.getmember(rel).mode & 0o111, rel)
            for member in packaged.getmembers():
                self.assertTrue({"tools", "node_modules"}.isdisjoint(member.name.split("/")), member.name)

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
        events = {**HOOK_EVENTS, "SessionEnd": ("session-end", 10, None)}
        self.assertEqual(set(hooks), set(events))
        for event, (arg, timeout, matcher) in events.items():
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
