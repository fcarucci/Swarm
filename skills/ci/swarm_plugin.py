"""ci plugin: everything that talks to a CI or repo host.

Commands: `swarm ci status|wait` (one shared, budget-safe poller per box). Host adapters,
webhook event sources and the CI events arrive here from the adapters branch; the engineering-team
plugin only consumes the CLI and the events and imports nothing from this directory.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _ci_wait():
    """The sibling module, loaded by path: a plugin file is not imported as part of a package."""
    name = "swarm_ci_wait"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "ci_wait.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


def run_ci(ctx, args) -> int:
    return _ci_wait().run_ci(ctx, args)


def setup_ci(parser) -> None:
    _ci_wait().setup_ci(parser)


def register(api) -> None:
    api.add_command("ci", run_ci, setup=setup_ci,
                    help="budget-safe CI status/wait for an exact SHA: one shared poller per box (use instead of gh run watch)")
