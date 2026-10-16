"""The hook's parse of a control command must see the CLI plugins' options (2026-10-15).

engineering-team adds `activate --team`. drivegate's parser used to be the bare core parser, so the hook could
not parse `swarm activate ... --team ...`, wrote no grant, and the CLI refused the requester session's own
command as "needs a person". The parser now loads the plugins exactly as `swarm` does, and restores the CLI
module's run-global plugin state (a hook process never keeps plugins loaded).
Run: cd tests && python3 -m unittest -v test_grant_plugin_options
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from swarm import cli, drivegate  # noqa: E402


def _fake_load_plugins(argv, parser):
    """Stand-in for cli._load_plugins: what engineering-team registers, plus a run-global side effect."""
    parser._swarm_subparsers.choices["activate"].add_argument("--team")
    cli.PLUGINS = object()
    return cli.PLUGINS


ARGV = ["activate", "--job", "J", "--workdir", "/w", "--goal", "g", "--team", "product_manager,build_engineer"]


class TestGrantParserLoadsPlugins(unittest.TestCase):
    def test_activate_with_a_plugin_option_parses(self):
        with mock.patch.object(cli, "_load_plugins", side_effect=_fake_load_plugins):
            ns = drivegate.parse_swarm(list(ARGV))
        self.assertIsNotNone(ns, "a plugin option must not fail the hook's parse (no grant would be written)")
        self.assertEqual(ns.team, "product_manager,build_engineer")
        self.assertIn("--team", drivegate.submitted_argv(ns))   # the grant's fingerprint covers it, as the CLI's does

    def test_plugin_state_is_restored(self):
        before = cli.PLUGINS
        with mock.patch.object(cli, "_load_plugins", side_effect=_fake_load_plugins):
            drivegate.parse_swarm(list(ARGV))
        self.assertIs(cli.PLUGINS, before)

    def test_a_broken_plugin_discovery_still_parses_core_commands(self):
        with mock.patch.object(cli, "_load_plugins", side_effect=RuntimeError("boom")):
            ns = drivegate.parse_swarm(["unhalt", "--job", "J"])
        self.assertIsNotNone(ns)


if __name__ == "__main__":
    unittest.main()
