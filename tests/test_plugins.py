"""CLI plugins (swarm.plugins): discovered from <config dir>/plugins, $SWARM_PLUGIN_PATH, the skills'
swarm_plugin.py and entry points; a broken one is reported by `swarm plugins` and never breaks core."""
from __future__ import annotations

import os
import textwrap
from unittest import mock

from test_hooks_cli import Env  # noqa: E402  (sets sys.path)

GOOD = '''
def hello(ctx, args):
    print("hello", args.who, ctx.plugin)

def setup(p):
    p.add_argument("--who", default="world")

def after_status(ctx, args):
    print("after status")

def lines(ctx, job):
    with ctx.open_board() as b:
        return [f"plug  {job} {b.job_status(job).status}"]

def register(api):
    api.add_command("hello", hello, setup=setup, help="say hello")
    api.extend_command("status", after=after_status)
    api.add_status_lines(lines)
'''


class PluginEnv(Env):
    def setUp(self):
        super().setUp()
        self.pdir = self.tmp / "plugins"
        self.pdir.mkdir()

    def plugin(self, name: str, body: str) -> None:
        (self.pdir / f"{name}.py").write_text(textwrap.dedent(body))


class PluginTests(PluginEnv):
    def test_core_works_with_no_plugins_and_lists_none(self):
        self.pdir.rmdir()
        rc, out, _ = self.cli("plugins")   # (the skill's plugin is disabled by the test config)
        self.assertEqual(rc, 0)
        self.assertRegex(out, r"engineering-team\tdisabled")
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        from swarm import paths, plugins
        with mock.patch.object(paths, "PLUGIN_ROOT", self.tmp):
            reg = plugins.Registry({}, self.config).load()
        self.assertEqual(reg.plugins, [])
        self.assertIn("no plugins found", reg.report()[0])

    def test_a_plugin_adds_a_command_arguments_and_status_lines(self):
        self.plugin("good", GOOD)
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        rc, out, err = self.cli("hello", "--who", "bob")
        self.assertEqual((rc, out, err), (0, "hello bob good\n", ""))
        rc, out, _ = self.cli("status", "--job", "J", "--no-color")
        self.assertIn("plug  J active", out)
        self.assertIn("after status", out)
        rc, out, _ = self.cli("plugins")
        self.assertRegex(out, r"good\tloaded\thello, \+status\t")

    def test_a_broken_plugin_is_listed_and_core_commands_go_on(self):
        self.plugin("syntax", "def register(:\n")
        self.plugin("noregister", "x = 1\n")
        self.plugin("raises", "def register(api):\n    api.add_command('half', lambda c, a: 0)\n    raise RuntimeError('boom')\n")
        self.plugin("clash", "def register(api):\n    api.add_command('post', lambda c, a: 0)\n")
        self.plugin("badext", "def register(api):\n    api.extend_command('nonesuch', after=lambda c, a: 0)\n")
        self.plugin("exits", "def register(api):\n    raise SystemExit(3)\n")
        self.plugin("good", GOOD)
        self.assertEqual(self.cli("activate", "--job", "J")[0], 0)
        self.assertEqual(self.cli("hello")[0], 0)   # the good one is unaffected
        rc, out, _ = self.cli("plugins")
        self.assertEqual(rc, 0)
        for name in ("syntax", "noregister", "raises", "clash", "badext", "exits"):
            self.assertRegex(out, rf"{name}\tERROR", name)
        self.assertIn("RuntimeError: boom", out)
        self.assertIn("already exists (swarm core)", out)
        self.assertIn("good\tloaded", out)
        with self.assertRaises(SystemExit):   # what a failed plugin registered is gone (argparse: unknown command)
            with mock.patch("sys.stderr"):
                self.cli("half")

    def test_a_failing_command_or_hook_does_not_break_core(self):
        self.plugin("bad", '''
            def boom(ctx, args):
                raise ValueError("nope")
            def before(ctx, args):
                raise ValueError("hook")
            def lines(ctx, job):
                raise ValueError("lines")
            def register(api):
                api.add_command("boom", boom)
                api.extend_command("status", before=before)
                api.add_status_lines(lines)
        ''')
        self.cli("activate", "--job", "J")
        rc, out, err = self.cli("boom")
        self.assertEqual(rc, 1)
        self.assertIn("plugin bad failed: ValueError: nope", err)
        rc, out, err = self.cli("status", "--job", "J", "--no-color")
        self.assertEqual(rc, 0)
        self.assertIn("job        J", out)
        self.assertIn("status hook failed", err)

    def test_a_before_hook_can_stop_the_command(self):
        self.plugin("gate", '''
            def before(ctx, args):
                print("refused by gate")
                return 7
            def register(api):
                api.extend_command("activate", before=before)
        ''')
        rc, out, _ = self.cli("activate", "--job", "J")
        self.assertEqual((rc, out), (7, "refused by gate\n"))
        with self.board() as b:
            self.assertIsNone(b.job_status("J"))

    def test_extra_search_path_and_disable(self):
        extra = self.tmp / "more"
        (extra / "pkg").mkdir(parents=True)
        (extra / "pkg" / "__init__.py").write_text("def register(api):\n    api.add_command('pk', lambda c, a: print('pk'))\n")
        with mock.patch.dict(os.environ, {"SWARM_PLUGIN_PATH": str(extra)}):
            self.assertEqual(self.cli("pk")[1], "pk\n")
            self.config.write_text(self.config.read_text().replace('disabled = ["engineering-team"]',
                                                                   'disabled = ["engineering-team", "pkg"]'))
            rc, out, _ = self.cli("plugins")
            self.assertRegex(out, r"pkg\tdisabled")

    def test_the_first_of_two_plugins_with_a_name_wins(self):
        extra = self.tmp / "more"
        extra.mkdir()
        (extra / "good.py").write_text("def register(api):\n    api.add_command('other', lambda c, a: 0)\n")
        self.plugin("good", GOOD)
        with mock.patch.dict(os.environ, {"SWARM_PLUGIN_PATH": str(extra)}):
            rc, out, _ = self.cli("plugins")
        self.assertIn("shadowed", out)
        self.assertEqual(out.count("loaded"), 1)

    def test_a_broken_config_still_runs_core(self):
        self.plugin("good", GOOD)
        rc, out, _ = self.cli("hello")
        self.assertEqual(rc, 0)

    def test_job_data_is_namespaced_per_plugin(self):
        self.plugin("kv", '''
            def put(ctx, args):
                with ctx.open_board() as b:
                    ctx.set_job_data(b, "J", "k", "v")
                    print(ctx.job_data(b, "J"), b.job_data("J"))
            def register(api):
                api.add_command("put", put)
        ''')
        self.cli("activate", "--job", "J")
        rc, out, _ = self.cli("put")
        self.assertEqual(out.strip(), "{'k': 'v'} {'kv.k': 'v'}")
