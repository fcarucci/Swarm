"""swarm migrate: old settings.json hooks out (backup first), old skill dir moved, refusal while a
job is active. All under a temp $HOME."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT  # noqa: F401

from swarm import bootstrap  # noqa: E402

def settings_with_old_hooks(home: Path) -> dict:
    OLD = f"{home}/.claude/skills/swarm/bin/swarm-hook"   # exactly what the old install_hooks wrote
    return {"model": "opus", "hooks": {
        "SubagentStart": [{"hooks": [{"type": "command", "command": f"{OLD} start", "timeout": 15}]}],
        "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"{OLD} turn", "timeout": 10}]},
                       {"matcher": "Bash", "hooks": [{"type": "command", "command": "/usr/bin/other-hook"}]},
                       # same basename, not the old skill's: must survive
                       {"matcher": "*", "hooks": [{"type": "command", "command": "/opt/tools/bin/swarm-hook turn"}]},
                       {"matcher": "*", "hooks": [{"type": "command", "command": f"{OLD} turn --verbose"}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": f"{OLD} done", "timeout": 10}]}],
        "SubagentStop": [{"hooks": [{"type": "command", "command": f"{OLD} stop", "timeout": 10}]}]}}


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-mig-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        # support.py turns migrate (and auto-init) off for the whole suite; these tests run them, under the temp $HOME
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "SWARM_NO_MIGRATE": "0", "SWARM_AUTO_INIT": "1"}); p.start(); self.addCleanup(p.stop)
        os.environ.pop("CLAUDE_SETTINGS", None)
        self.settings = self.home / ".claude/settings.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_text(json.dumps(settings_with_old_hooks(self.home)))
        self.skills = self.home / ".claude/skills"
        (self.skills / "swarm/bin").mkdir(parents=True)
        (self.skills / "swarm/SKILL.md").write_text("old")
        self.markers = self.home / ".local/state/swarm/active"
        self.markers.mkdir(parents=True)

    def migrate(self, **kw):
        return bootstrap.migrate(settings_path=self.settings, skills_dir=self.skills, marker_dir=self.markers, **kw)

    def test_removes_only_swarm_hooks_with_backup_and_moves_dir(self):
        self.settings.chmod(0o600)
        steps = self.migrate()
        s = json.loads(self.settings.read_text())
        self.assertEqual(s["model"], "opus")
        self.assertEqual(list(s["hooks"]), ["PreToolUse"])
        self.assertEqual([g["hooks"][0]["command"] for g in s["hooks"]["PreToolUse"]],
                         ["/usr/bin/other-hook", "/opt/tools/bin/swarm-hook turn",
                          f"{self.home}/.claude/skills/swarm/bin/swarm-hook turn --verbose"])
        backups = list(self.settings.parent.glob("settings.json.pre-swarm-*"))
        self.assertEqual(len(backups), 1)
        self.assertFalse((self.skills / "swarm").exists())
        legacy = list((self.home / ".local/share/swarm").glob("legacy-skill-*"))
        self.assertEqual(len(legacy), 1)
        self.assertEqual((legacy[0] / "SKILL.md").read_text(), "old")
        self.assertFalse((self.home / "src/Swarm").exists())                        # never ~/src/Swarm
        self.assertTrue(any(st.status == "changed" for st in steps))
        self.assertEqual(oct(self.settings.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(backups[0].stat().st_mode & 0o777), "0o600")

    def test_bootstrap_skips_migrate_when_told(self):
        cfg = self.home / "c.toml"; cfg.write_text('[board]\nbackend = "memory"\n')
        with mock.patch.dict(os.environ, {"SWARM_NO_MIGRATE": "1"}):
            steps = bootstrap.bootstrap("claude", config=cfg)
        self.assertIn(bootstrap.Step("migrate", "skipped", "SWARM_NO_MIGRATE set"), steps)
        self.assertTrue((self.skills / "swarm").exists())
        s = json.loads(self.settings.read_text())
        # the old hooks stay (no migrate); bootstrap --host claude only adds the per-user spool to
        # the Claude sandbox's write allowlist, nothing else
        self.assertEqual(s.pop("sandbox"), {"filesystem": {"allowWrite": [str(self.home / ".local/state/swarm/spool")]}})
        self.assertEqual(s, settings_with_old_hooks(self.home))

    def test_refuses_while_job_active(self):
        (self.markers / "J.json").write_text('{"job": "J"}')
        steps = self.migrate()
        self.assertEqual(steps[-1].status, "refused")
        self.assertIn("J", steps[-1].detail)
        self.assertEqual(json.loads(self.settings.read_text()), settings_with_old_hooks(self.home))
        self.assertTrue((self.skills / "swarm").exists())

    def test_force_overrides(self):
        (self.markers / "J.json").write_text('{"job": "J"}')
        self.migrate(force=True)
        self.assertFalse((self.skills / "swarm").exists())
        self.assertEqual(bootstrap.legacy_hook_commands(json.loads(self.settings.read_text()), self.skills), [])

    def test_unrelated_empty_groups_and_events_are_kept(self):
        s = settings_with_old_hooks(self.home)
        s["hooks"]["Notification"] = []                                    # the user's own, empty
        s["hooks"]["PostToolUse"].append({"matcher": "Edit", "hooks": []})  # likewise
        self.settings.write_text(json.dumps(s))
        self.migrate()
        after = json.loads(self.settings.read_text())["hooks"]
        self.assertEqual(after["Notification"], [])
        self.assertEqual(after["PostToolUse"], [{"matcher": "Edit", "hooks": []}])
        self.assertNotIn("SubagentStart", after)                          # emptied by us: dropped
        self.assertNotIn("SubagentStop", after)

    def cli(self, *argv):
        import contextlib
        import io
        from swarm import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_cli_uses_the_selected_configs_markers(self):
        other = self.home / "elsewhere/active"; other.mkdir(parents=True)
        (other / "K.json").write_text('{"job": "K"}')                     # active under the other config only
        cfg = self.home / "alt.toml"
        cfg.write_text(f'[board]\nbackend = "memory"\n[hook]\nmarker_dir = "{other}"\n')
        with mock.patch.dict(os.environ, {"CLAUDE_SETTINGS": str(self.settings)}):
            rc, out, _ = self.cli("--config", str(cfg), "migrate")
        self.assertEqual(rc, 1)
        self.assertIn("refused", out)
        self.assertIn("K", out)
        self.assertEqual(json.loads(self.settings.read_text()), settings_with_old_hooks(self.home))
        self.assertTrue((self.skills / "swarm").exists())

    def test_cli_malformed_settings_is_one_line_and_untouched(self):
        bad = '{"model": "secret-value", "hooks": {'
        self.settings.write_text(bad)
        cfg = self.home / "c.toml"; cfg.write_text('[board]\nbackend = "memory"\n')
        with mock.patch.dict(os.environ, {"CLAUDE_SETTINGS": str(self.settings)}):
            rc, out, err = self.cli("--config", str(cfg), "migrate")
        self.assertEqual(rc, 1)
        self.assertEqual(err.count("\n"), 1, err)
        self.assertIn(str(self.settings), err)
        self.assertIn("not valid JSON", err)
        self.assertNotIn("secret-value", err + out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.settings.read_text(), bad)
        self.assertEqual(list(self.settings.parent.glob("settings.json.pre-swarm-*")), [])

    def test_second_run_is_noop(self):
        self.migrate()
        steps = self.migrate()
        self.assertEqual([s.status for s in steps], ["ok"])

    def test_does_not_move_the_running_plugin(self):
        with mock.patch.object(bootstrap.paths, "PLUGIN_ROOT", self.skills / "swarm"):
            self.migrate()
        self.assertTrue((self.skills / "swarm").exists())

    def test_bootstrap_without_stamp_when_migrate_refused(self):
        (self.markers / "J.json").write_text('{"job": "J"}')
        cfg = self.home / "c.toml"; cfg.write_text('[board]\nbackend = "memory"\n')
        stamp = self.home / "stamp"
        with mock.patch.object(bootstrap, "migrate", return_value=[bootstrap.Step("migrate", "refused", "J")]):
            bootstrap.bootstrap("claude", config=cfg, stamp=stamp)
        self.assertFalse(stamp.exists())


class SettingsWriterTests(unittest.TestCase):
    def test_only_migrate_touches_the_claude_settings(self):
        # the security sweep's rule: nothing but migrate (through safefile) rewrites ~/.claude/settings.json
        users = sorted(p.relative_to(ROOT / "lib").as_posix() for p in (ROOT / "lib").rglob("*.py")
                       if "claude_settings_path()" in p.read_text() or "settings.json" in p.read_text())
        # bootstrap.py: migrate, and bootstrap --host claude's sandbox.filesystem.allowWrite entry.
        # supervisor/command.py and launch.py name a *project's* .claude/settings.json (hashing for
        # `supervise approve`): allowed, as long as it never touches the user's settings.
        readers = {"swarm/supervisor/command.py", "swarm/supervisor/launch.py"}
        for r in readers & set(users):
            self.assertNotIn("claude_settings_path", (ROOT / "lib" / r).read_text())
            self.assertNotIn("write_preserving", (ROOT / "lib" / r).read_text())
        self.assertEqual([u for u in users if u not in readers],
                         ["swarm/bootstrap.py", "swarm/cli.py", "swarm/safefile.py"])
        cli_text = (ROOT / "lib/swarm/cli.py").read_text()
        self.assertNotIn("_write_settings", cli_text)
        callers = [chunk.split("(", 1)[0] for chunk in cli_text.split("\ndef ")[1:]
                   if "claude_settings_path()" in chunk]
        self.assertEqual(callers, ["claude_settings_path", "cmd_migrate"])   # the definition, and migrate


class MoveOldDefaultsTests(unittest.TestCase):
    """`swarm migrate` moves a board at the old default (inside the old Codex writable
    root) to the configured new path under the board's lock, moves queued spool records out of
    the old shared /tmp default when this user owns it, and takes back old Codex grants."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="swarm-mig-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "SWARM_NO_MIGRATE": "0"})
        p.start(); self.addCleanup(p.stop)
        self.settings = self.home / ".claude/settings.json"            # absent: nothing of the old skill
        self.skills = self.home / ".claude/skills"
        self.markers = self.home / ".local/state/swarm/active"; self.markers.mkdir(parents=True)
        self.old_spool = Path(tempfile.mkdtemp(prefix="swarm-oldspool-")) / "claude/swarm-spool"
        self.addCleanup(shutil.rmtree, self.old_spool.parent.parent, ignore_errors=True)
        p = mock.patch.object(bootstrap, "OLD_SPOOL", str(self.old_spool)); p.start(); self.addCleanup(p.stop)
        self.codex = self.home / ".codex"
        p = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex)}); p.start(); self.addCleanup(p.stop)

    def cfg(self, backend="sqlite", **over):
        from swarm.cli import load_config
        cfg = load_config(Path("/nonexistent/x.toml"))
        cfg["board"]["backend"] = backend
        cfg["board"]["spool_dir"] = "~/.local/state/swarm/spool"
        cfg["sqlite"]["path"] = "~/.local/share/swarm-board/board.sqlite3"
        cfg["file"]["path"] = "~/.local/share/swarm-board/board"
        for k, v in over.items():
            cfg[k].update(v)
        return cfg

    def migrate(self, cfg, **kw):
        return bootstrap.migrate(settings_path=self.settings, skills_dir=self.skills, marker_dir=self.markers,
                                 cfg=cfg, **kw)

    def test_nothing_to_do(self):
        self.assertEqual([s.status for s in self.migrate(self.cfg())], ["ok"])

    def old_sqlite(self):
        import sqlite3
        old = self.home / ".local/state/swarm/board.sqlite3"
        db = sqlite3.connect(old)
        db.execute("PRAGMA journal_mode=wal")
        db.execute("CREATE TABLE t (x)"); db.execute("INSERT INTO t VALUES ('kept')"); db.commit(); db.close()
        return old

    def test_moves_old_default_sqlite_board(self):
        import sqlite3
        old = self.old_sqlite()
        steps = self.migrate(self.cfg())
        new = self.home / ".local/share/swarm-board/board.sqlite3"
        self.assertTrue(any(s.status == "changed" and str(new) in s.detail for s in steps), steps)
        self.assertEqual(sqlite3.connect(new).execute("SELECT x FROM t").fetchall(), [("kept",)])
        self.assertFalse(old.exists())
        kept = [f for f in old.parent.glob("board.sqlite3.migrated-*") if not f.name.endswith(("-wal", "-shm"))]
        self.assertEqual(len(kept), 1)                                                 # kept, not deleted
        self.assertEqual(oct(new.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual([s.status for s in self.migrate(self.cfg())], ["ok"])        # done once

    def test_moves_old_default_file_board(self):
        old = self.home / ".local/state/swarm/board"; old.mkdir(parents=True)
        (old / "state.json").write_text("{}"); (old / "lock").write_text("")
        steps = self.migrate(self.cfg("file"))
        new = self.home / ".local/share/swarm-board/board"
        self.assertTrue(any(s.status == "changed" for s in steps), steps)
        self.assertEqual((new / "state.json").read_text(), "{}")
        self.assertFalse(old.exists())

    def test_board_left_when_configured_at_the_old_path_or_new_exists(self):
        old = self.old_sqlite()
        self.migrate(self.cfg(sqlite={"path": "~/.local/state/swarm/board.sqlite3"}))
        self.assertTrue(old.exists())
        new = self.home / ".local/share/swarm-board/board.sqlite3"
        new.parent.mkdir(parents=True); new.write_bytes(b"")
        steps = self.migrate(self.cfg())
        self.assertTrue(old.exists())
        self.assertTrue(any(s.status == "manual" and "both" in s.detail for s in steps), steps)

    def test_old_board_symlink_not_followed(self):
        target = self.home / "elsewhere"; target.mkdir()
        (target / "state.json").write_text("{}")
        st = self.home / ".local/state/swarm"
        (st / "board").symlink_to(target)
        steps = self.migrate(self.cfg("file"))
        self.assertFalse((self.home / ".local/share/swarm-board/board").exists())
        self.assertTrue((target / "state.json").exists())
        self.assertTrue(any(s.status == "manual" for s in steps), steps)

    def test_board_move_refused_while_a_job_is_active(self):
        old = self.old_sqlite()
        (self.markers / "J.json").write_text('{"job": "J"}')
        steps = self.migrate(self.cfg())
        self.assertEqual(steps[-1].status, "refused")
        self.assertTrue(old.exists())

    def old_spool_with(self, names):
        self.old_spool.mkdir(parents=True, mode=0o700)
        for n in names:
            (self.old_spool / n).write_text(json.dumps({"job": "J", "name": "A", "message": n}))
        return self.old_spool

    def test_moves_old_spool_records(self):
        self.old_spool_with(["a1.json", "b2.mem", "c3.vrd", "d4.stuck"])
        (self.old_spool / "notes.txt").write_text("not a record")
        steps = self.migrate(self.cfg())
        new = self.home / ".local/state/swarm/spool"
        self.assertEqual(sorted(p.name for p in new.iterdir()), ["a1.json", "b2.mem", "c3.vrd", "d4.stuck"])
        self.assertEqual(json.loads((new / "a1.json").read_text())["message"], "a1.json")
        self.assertEqual(oct(new.stat().st_mode & 0o777), "0o700")
        self.assertEqual(sorted(p.name for p in self.old_spool.iterdir()), ["notes.txt"])
        self.assertTrue(any(s.status == "changed" and "4" in s.detail for s in steps), steps)

    def test_old_spool_links_and_fifos_not_followed(self):
        self.old_spool_with(["a1.json"])
        victim = self.home / "victim.json"; victim.write_text('{"secret": 1}')
        (self.old_spool / "b2.json").symlink_to(victim)
        os.mkfifo(self.old_spool / "c3.json")
        import threading
        t = threading.Thread(target=lambda: self.migrate(self.cfg()), daemon=True)
        t.start(); t.join(10)
        self.assertFalse(t.is_alive(), "migrate blocked on a FIFO")
        new = self.home / ".local/state/swarm/spool"
        self.assertEqual(sorted(p.name for p in new.iterdir()), ["a1.json"])
        self.assertEqual(victim.read_text(), '{"secret": 1}')

    def test_old_spool_of_another_user_left_alone(self):
        self.old_spool_with(["a1.json"])
        real = os.getuid()
        with mock.patch("os.getuid", return_value=real + 4242):
            steps = bootstrap._move_old_spool(self.cfg())
        self.assertTrue((self.old_spool / "a1.json").exists())
        self.assertEqual(steps, [])

    def test_migrate_takes_back_old_codex_grants(self):
        self.codex.mkdir()
        state = self.home / ".local/state/swarm"
        (self.codex / "config.toml").write_text(
            f'[sandbox_workspace_write]\nnetwork_access = true\nwritable_roots = ["{state}"]\n')
        (self.codex / "config.toml.pre-swarm-20260901-000000").write_text("")
        steps = self.migrate(self.cfg())
        import tomllib
        sw = tomllib.loads((self.codex / "config.toml").read_text())["sandbox_workspace_write"]
        self.assertEqual(sw["writable_roots"], [])
        self.assertNotIn("network_access", sw)
        self.assertTrue(any(s.status == "changed" and "network_access" in s.detail for s in steps), steps)

    def test_sqlite_swapped_for_a_link_mid_move_is_aborted(self):
        old = self.old_sqlite()
        secret = self.home / "secret.sqlite3"
        import shutil, sqlite3
        shutil.copy(old, secret)
        real_connect = sqlite3.connect
        calls = []

        class Src:                                          # the copy's source: swapped during the copy
            def __init__(self, conn):
                self.conn = conn

            def backup(self, dst, *a, **k):
                old.rename(old.with_name("moved-away"))
                old.symlink_to(secret)                      # the entry now points elsewhere
                return self.conn.backup(dst, *a, **k)

            def close(self):
                self.conn.close()

        def connect(path, *a, **k):
            conn = real_connect(path, *a, **k)
            calls.append(path)
            return Src(conn) if len(calls) == 2 else conn
        with mock.patch("sqlite3.connect", connect):
            steps = bootstrap.migrate(settings_path=self.settings, skills_dir=self.skills,
                                      marker_dir=self.markers, cfg=self.cfg())
        new = self.home / ".local/share/swarm-board/board.sqlite3"
        self.assertFalse(new.exists())
        self.assertEqual([f.name for f in new.parent.iterdir()], [])
        self.assertTrue(any(s.status == "manual" for s in steps), steps)
