"""The QA run's `swarm`: the real CLI with ONE seam stubbed, in the isolated test HOME only.

`swarm activate` of a drive job checks that the supervisor's systemd user timer is active (the old precondition,
blocked on plan Task 21, the hook-triggered driver). The QA rig never installs a timer (swarm must not install
system timers): it runs the supervisor pass itself, exactly what the timer would run. So the timer-state seam
(`swarm.drive_activate.timer_state`) answers "active, enabled" here and nothing else changes: the last-pass
recency check still needs a real pass."""
import runpy
import sys

try:
    import swarm.drive_activate as _d
    _d.timer_state = lambda: {"ActiveState": "active", "UnitFileState": "enabled"}
except ImportError:      # a checkout without drive activation: nothing to stub
    pass
sys.argv[0] = "swarm"
runpy.run_module("swarm.cli", run_name="__main__")
