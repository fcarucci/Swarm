"""The Codex directory edition: `scripts/build-codex-directory` packages the tree as a hook-free
plugin ZIP (OpenAI's public directory refuses lifecycle hooks). The ZIP has no hooks, no Claude-only
files, no dev files, a directory manifest with the required `interface` fields, skills that
work without hooks, and a CLI that runs a join -> post -> read flow from the extracted tree.
The full edition's own skill text is never touched by the build."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from support import ROOT  # noqa: F401  (sets sys.path)

SCRIPT = ROOT / "scripts" / "build-codex-directory"
KIB = 1024
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
HOOK_WORD = re.compile(r"\bhooks?\b", re.IGNORECASE)
HOOK_FILES = {"hooks.json", "codex-hooks.json", "swarm-hook", "swarm-hook.cmd", "winhook.py"}
TOP_EXCLUDED = (".claude-plugin", "hooks", "tests", "e2e", "scripts", ".github", "docs", "install.sh",
                "install.ps1", "CHANGELOG.md")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CodexDirectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="swarm-cdx-")
        cls.out = Path(cls.tmp.name) / "dist"
        cls.sources = sorted((ROOT / "skills").glob("*/SKILL.md"))
        cls.before = {p: sha(p) for p in cls.sources}
        res = subprocess.run([sys.executable, str(SCRIPT), "--out", str(cls.out)],
                             capture_output=True, text=True, timeout=120)
        cls.build = res
        cls.main_manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())
        cls.zip_path = cls.out / f"swarm-codex-{cls.main_manifest['version']}.zip"
        cls.extract = Path(cls.tmp.name) / "plugin"
        if cls.zip_path.exists():
            with zipfile.ZipFile(cls.zip_path) as z:
                cls.names = z.namelist()
                cls.infos = z.infolist()
                z.extractall(cls.extract)
        else:
            cls.names, cls.infos = [], []

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def manifest(self) -> dict:
        return json.loads((self.extract / ".codex-plugin" / "plugin.json").read_text())

    def test_build_makes_the_named_zip(self):
        self.assertEqual(self.build.returncode, 0, self.build.stderr)
        self.assertTrue(self.zip_path.is_file(), self.build.stdout + self.build.stderr)
        self.assertIn(str(self.zip_path), self.build.stdout)

    def test_dist_is_ignored_by_git(self):
        self.assertIn("dist/", (ROOT / ".gitignore").read_text().split())

    def test_no_hooks_anywhere(self):
        self.assertTrue(self.names)
        for name in self.names:
            parts = Path(name).parts
            self.assertNotIn("hooks", parts[:-1], name)
            self.assertNotIn(parts[-1], HOOK_FILES, name)
            self.assertNotIn("hook", name.lower(), name)     # not even lib/swarm/hooks.py
        self.assertNotIn("hooks", self.manifest())
        for rel in (".codex-plugin/plugin.json", "README.md", "PRIVACY.md", "SECURITY.md", "THIRD_PARTY_NOTICES.md",
                    *[n for n in self.names if re.fullmatch(r"skills/[^/]+/SKILL\.md", n)]):
            text = (self.extract / rel).read_text(encoding="utf-8")
            self.assertIsNone(HOOK_WORD.search(text), f"{rel} mentions hooks")

    def test_claude_only_and_dev_files_are_left_out(self):
        for name in self.names:
            self.assertNotIn(Path(name).parts[0], TOP_EXCLUDED, name)
            self.assertNotIn("tests", Path(name).parts, name)
            self.assertFalse(name.endswith("SKILL.cdx.md"), name)
            self.assertNotIn("/engineering-team/", "/" + name)
            self.assertNotIn("/ci/", "/" + name)
        for must in ("bin/swarm", "lib/swarm/cli.py", "requirements.txt", "config.example.toml", "LICENSE",
                     "NOTICE", "README.md", "PRIVACY.md", "THIRD_PARTY_NOTICES.md",
                     ".codex-plugin/plugin.json", "assets/icon.svg", "assets/logo.svg", "skills/swarm/SKILL.md"):
            self.assertIn(must, self.names)

    def test_sizes_and_no_archives(self):
        self.assertLessEqual(len(self.names), 512)
        for info in self.infos:
            name = info.filename
            self.assertFalse(re.search(r"\.(tar|tgz|zip|gz|zst|xz|bz2|7z|whl|so|exe|dll|dylib)$", name), name)
            limit = 5 * KIB * KIB if name.lower().endswith(IMAGE_SUFFIXES) else 256 * KIB
            self.assertLessEqual(info.file_size, limit, name)

    def test_manifest_has_the_required_fields(self):
        m = self.manifest()
        self.assertEqual(m["version"], self.main_manifest["version"])
        self.assertRegex(m["name"], r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertLessEqual(len(m["name"]), 64)
        self.assertEqual(m["name"], "swarm-team")
        self.assertNotEqual(m["name"], "swarm")           # a single dictionary word is discouraged
        self.assertLessEqual(len(m["description"]), 4000)
        self.assertTrue(m["author"]["name"])
        self.assertEqual(m["license"], "Apache-2.0")
        ui = m["interface"]
        for key in ("displayName", "shortDescription", "longDescription", "developerName", "category",
                    "capabilities", "websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL",
                    "composerIcon", "logo"):
            self.assertIn(key, ui)
            self.assertTrue(ui[key] or key == "capabilities", key)
        self.assertLessEqual(len(ui["displayName"]), 30)
        self.assertLessEqual(len(ui["shortDescription"]), 30)
        self.assertLessEqual(len(ui["longDescription"]), 4000)
        self.assertLessEqual(len(ui["developerName"]), 80)
        self.assertEqual(ui["category"], "Developer Tools")
        self.assertIsInstance(ui["capabilities"], list)
        for key in ("websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL"):
            self.assertTrue(ui[key].startswith("https://"), key)
        for key in ("composerIcon", "logo"):
            self.assertTrue(ui[key].startswith("./"), key)
            icon = self.extract / ui[key]
            self.assertTrue(icon.is_file(), ui[key])
            text = icon.read_text()
            vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', text)
            self.assertIsNotNone(vb)
            self.assertEqual(vb.group(1), vb.group(2))      # square
            self.assertGreaterEqual(int(vb.group(1)), 48)

    def test_skills_load_and_the_full_edition_text_is_untouched(self):
        after = {p: sha(p) for p in self.sources}
        self.assertEqual(self.before, after, "the build changed a source SKILL.md")
        for variant in ("swarm", "ask-answer"):
            cdx = ROOT / "skills" / variant / "SKILL.cdx.md"
            self.assertTrue(cdx.is_file(), cdx)
            self.assertEqual((self.extract / "skills" / variant / "SKILL.md").read_bytes(), cdx.read_bytes())
            self.assertNotEqual(cdx.read_bytes(), (ROOT / "skills" / variant / "SKILL.md").read_bytes())
        for src in self.sources:
            name = src.parent.name
            shipped = self.extract / "skills" / name / "SKILL.md"
            if name in ("engineering-team", "ci"):
                self.assertFalse(shipped.exists(), name)
                continue
            self.assertTrue(shipped.is_file(), name)
            head = shipped.read_text().split("---")[1]
            self.assertRegex(head, rf"(?m)^name: {re.escape(name)}$")
            self.assertRegex(head, r"(?m)^description: \S")
            if not (src.parent / "SKILL.cdx.md").exists():
                self.assertEqual(shipped.read_bytes(), src.read_bytes())

    def cli(self, home: Path, *args: str, check=True, env_extra=None, both=False):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CLAUDE", "CODEX", "SWARM_")) and k not in ("PLUGIN_ROOT",)}
        env.update(HOME=str(home), PYTHONPATH=str(self.extract / "lib"), SWARM_AUTO_INIT="0",
                   SWARM_NO_SYSTEMD="1", SWARM_NO_MIGRATE="1", PYTHONDONTWRITEBYTECODE="1",
                   CODEX_HOME=str(home / ".codex"))
        env.update(env_extra or {})
        res = subprocess.run([sys.executable, "-m", "swarm.cli", *args], capture_output=True, text=True,
                             env=env, cwd=home, timeout=120, stdin=subprocess.DEVNULL)
        if check:
            self.assertEqual(res.returncode, 0, f"swarm {' '.join(args)}: {res.stderr}")
        return (res.stdout + res.stderr) if both else res.stdout

    def test_cdx_skill_commands_exist_in_the_cli(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            home = Path(h)
            skill = (self.extract / "skills" / "swarm" / "SKILL.md").read_text()
            seen = set()
            for line in skill.splitlines():
                m = re.match(r"\s*(?:\S*/)?swarm (\w[\w-]*)((?: .*)?)$", line.strip().lstrip("$ "))
                if not m or m.group(1) in seen or not line.lstrip().startswith(("swarm ", "$ swarm ", "<plugin root>")):
                    continue
                sub = m.group(1)
                seen.add(sub)
                helptext = self.cli(home, sub, "--help")
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", line):
                    self.assertIn(flag, helptext, f"`swarm {sub}` has no {flag}: {line.strip()}")
            for needed in ("join", "read", "post", "activate", "done", "verdict"):
                self.assertIn(needed, seen)
            self.assertIn("swarm join --job", skill)
            self.assertIn("swarm read", skill)
            self.assertIn("swarm post", skill)

    def test_hook_free_flow_join_post_read(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            home = Path(h)
            self.cli(home, "init")
            self.cli(home, "activate", "--job", "cdx1", "--task", "smoke", "--no-supervise")
            a = self.cli(home, "join", "--job", "cdx1", "--key", "eng-1", "--role", "engineer").strip()
            b = self.cli(home, "join", "--job", "cdx1", "--key", "qa-1", "--role", "qa").strip()
            self.assertTrue(a and b and a != b)
            self.cli(home, "post", "--job", "cdx1", "--key", "eng-1", "CLAIM: the parser")
            self.cli(home, "post", "--job", "cdx1", "--key", "eng-1", "--to", "@qa", "please review")
            seen = self.cli(home, "read", "--job", "cdx1", "--key", "qa-1")
            self.assertIn("CLAIM: the parser", seen)
            self.assertIn("please review", seen)
            self.assertIn("no new messages", self.cli(home, "read", "--job", "cdx1", "--key", "qa-1").lower())
            judge = self.cli(home, "join", "--job", "cdx1", "--key", "judge-1", "--judge").strip()
            self.assertTrue(judge)

    def test_hook_free_goal_flow_judge_handoff_and_close(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            home = Path(h)
            self.cli(home, "init")
            self.cli(home, "activate", "--job", "cdx2", "--task", "t", "--goal", "g", "--no-supervise")
            self.cli(home, "join", "--job", "cdx2", "--key", "eng", "--role", "engineer")
            judge = self.cli(home, "join", "--job", "cdx2", "--key", "jdg", "--role", "judge", "--judge").strip()
            self.cli(home, "done", "--job", "cdx2", "--key", "eng", "--summary", "did it")
            seen = self.cli(home, "read", "--job", "cdx2", "--key", "jdg")
            ref = re.search(r"handoff-[0-9a-f]+", seen)
            self.assertIsNotNone(ref, seen)                  # the REF the skill tells the judge to use
            self.cli(home, "verdict", "--job", "cdx2", "--as", judge, "--artifact", ref.group(0), "met", "ok")
            refused = self.cli(home, "deactivate", "--job", "cdx2", "--outcome", "done", check=False, both=True)
            self.assertIn("FINALIZED", refused)              # accepted work awaits finalization
            self.cli(home, "post", "--job", "cdx2", "--key", "eng", f"FINALIZED {ref.group(0)}")
            self.cli(home, "deactivate", "--job", "cdx2", "--outcome", "done")

    def test_the_edition_marker_is_in_the_zip_only(self):
        self.assertEqual((self.extract / "lib" / "swarm" / "EDITION").read_text().strip(), "codex-directory")
        self.assertFalse((ROOT / "lib" / "swarm" / "EDITION").exists())

    def test_activate_gives_no_hook_advice_in_this_edition(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            home = Path(h)
            self.cli(home, "init")
            out = self.cli(home, "activate", "--job", "cdx3", "--task", "t", "--goal", "g", "--no-supervise", both=True)
            for bad in ("[swarm job:", "[swarm role:", "spawned from now on", "/hooks"):
                self.assertNotIn(bad, out)
            self.assertIn("swarm join --job cdx3 --key", out)

    def test_doctor_and_bootstrap_know_a_directory_install(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            home = Path(h)
            doctor = self.cli(home, "doctor", "--host", "codex", check=False, both=True)
            boot = self.cli(home, "bootstrap", "--host", "codex", check=False, both=True)
            for out in (doctor, boot):
                for bad in ("/hooks", "swarm@swarm", "hooks trusted", "supervise codex hooks"):
                    self.assertNotIn(bad, out)
            self.assertIn("directory edition", doctor)

    def test_hook_command_is_not_available_in_this_edition(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            out = self.cli(Path(h), "hook", "turn", check=False, both=True)
            self.assertIn("not available in this edition", out)

    def ask_flow(self, home, env_extra):
        self.cli(home, "init")
        self.cli(home, "activate", "--job", "cdx4", "--task", "t", "--no-supervise")
        eng = self.cli(home, "join", "--job", "cdx4", "--key", "eng", "--role", "engineer").strip()
        qa = self.cli(home, "join", "--job", "cdx4", "--key", "qa", "--role", "qa").strip()
        q = self.cli(home, "ask", "--job", "cdx4", "--key", "eng", "--to", "@qa", "Which tests?",
                     env_extra=env_extra).strip()
        self.assertRegex(q, r"^Q\d+$")
        listing = self.cli(home, "questions", "--job", "cdx4", "--open", "--to", "me", "--key", "qa",
                           env_extra=env_extra)
        self.assertIn("Which tests?", listing)
        self.assertNotIn("Which tests?", self.cli(home, "questions", "--job", "cdx4", "--open", "--to", "me",
                                                  "--key", "eng", env_extra=env_extra))   # not the asker's
        self.cli(home, "answer", q[1:], "--key", "qa", "unit tests", env_extra=env_extra)
        shown = self.cli(home, "questions", "--job", "cdx4", "--all", env_extra=env_extra)
        self.assertIn(f"answer by {qa}", shown)
        self.assertNotIn("answer by human", shown)

    def test_ask_answer_identifies_a_joined_agent_by_key(self):
        for extra in ({}, {"CODEX_THREAD_ID": "0198a1b2-0000-7000-8000-000000000001",
                           "CODEX_SESSION_ID": "0198a1b2-0000-7000-8000-000000000002"},
                      {"CODEX_THREAD_ID": "0198a1b2-0000-7000-8000-000000000001"}):
            with self.subTest(env=sorted(extra)), tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
                self.ask_flow(Path(h), extra)

    def test_a_key_that_is_not_a_joined_agent_is_never_the_human(self):
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-home-") as h:
            home = Path(h)
            self.cli(home, "init")
            self.cli(home, "activate", "--job", "cdx5", "--task", "t", "--no-supervise")
            out = self.cli(home, "ask", "--job", "cdx5", "--key", "ghost", "--to", "human", "q?",
                           check=False, both=True)
            self.assertNotRegex(out, r"^Q\d+")
            self.assertIn("join", out)

    def release_workflow(self) -> str:
        return (ROOT / ".github" / "workflows" / "release.yml").read_text()

    def test_release_workflow_triggers_only_on_tags_and_attaches_the_zip(self):
        text = self.release_workflow()
        on = text.split("\npermissions:")[0]
        self.assertIn('tags:\n      - "v*"', on)
        self.assertIn("workflow_dispatch:", on)
        for other in ("branches:", "pull_request", "schedule:"):
            self.assertNotIn(other, on)
        create = text[text.index("gh release create"):]
        self.assertIn('"dist/swarm-codex-${VERSION}.zip"', create)

    @unittest.skipIf(os.name == "nt", "runs the workflow's bash step")
    def test_release_workflow_build_step_makes_the_zip_it_attaches(self):
        text = self.release_workflow()
        step = text[text.index("- name: Build the Codex directory edition"):]
        script = step.split("run: |\n", 1)[1].split("\n\n      - name:", 1)[0]
        script = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in script.splitlines())
        version = self.main_manifest["version"]
        with tempfile.TemporaryDirectory(prefix="swarm-cdx-rel-") as d:
            work = Path(d)
            (work / "dist").mkdir()
            (work / "dist" / "SHA256SUMS").write_text("")
            res = subprocess.run(["bash", "-c", script.replace("scripts/build-codex-directory",
                                                                f"{ROOT}/scripts/build-codex-directory")],
                                 cwd=work, env={**os.environ, "VERSION": version}, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((work / "dist" / f"swarm-codex-{version}.zip").is_file())
            self.assertIn(f"swarm-codex-{version}.zip", (work / "dist" / "SHA256SUMS").read_text())

    def test_shipped_config_example_has_no_personal_or_dangling_references(self):
        text = (self.extract / "config.example.toml").read_text()
        self.assertNotIn("hermes", text)
        self.assertNotRegex(text, r"(?<!main/)docs/REFERENCE\.md")      # only the absolute GitHub URL


if __name__ == "__main__":
    unittest.main()
