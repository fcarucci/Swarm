# Supervise the existing reconciliation listener

`events serve --reconcile` reuses the native listener and its exclusive lock. It polls
persisted work after interactive turns end. When external events are disabled or no
sources exist, reconciliation runs without binding HTTP. This mode adds no timer and
no second scheduling authority.

Hooks can start a missing listener at the next event; they cannot resurrect a killed
sole listener while every session remains quiet. For that guarantee, the owner deploys
this user-service template through the host's authorized service-management procedure.
Replace all absolute paths with the installed, reviewed runtime. Do not point a service
at a mutable development checkout. This document does not install or enable anything.

```ini
[Unit]
Description=Swarm native reconciliation listener
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=PYTHONPATH=/ABS/INSTALLED/SWARM/lib
ExecStart=/ABS/SWARM/VENV/bin/python -m swarm.cli --config /ABS/CONFIG/config.toml events serve --reconcile
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=process
UMask=0077

[Install]
WantedBy=default.target
```

Use the same OS user, board config and private state directory as existing Swarm.
`KillMode=process` keeps already-claimed bounded runners alive during listener restart;
the runner owns its execution timeout and cancellation fence. Do not copy secrets into
the unit or logs. Ensure the service environment can resolve the configured credentials.
If an existing detached listener holds the lock, coordinate a clean handoff before
starting the service; do not run two reconcilers or kill active task runners.

A user manager must remain alive after logout for unattended operation; verify its
existing host policy. A restart loop, missing credentials, unavailable model, or failed
reconciliation is an operational blocker, not evidence of a running agent.

## Acceptance protocol

1. Freeze the source SHA, installed tree identity, role model configuration and native
   task/protocol review results. Record the actual interpreter and plugin paths.
2. Register a real authorized, bounded verification chain for one immutable artifact.
   Use an already-running reporter only through its assigned identity; do not impersonate
   it or restart a completed build. A fresh verifier may inspect an existing archive.
3. End PM/EL turns. The last assigned predecessor records its exact-artifact report
   while its turn is still live, then ends that turn. Its terminal event is the final
   prerequisite; never attempt to report after the assigned executor has already ended.
   Observe exactly one downstream runner from persisted tasks without a requester tool
   call being the dispatch trigger. Record task generation, launch/restart IDs and times.
4. Kill only the listener process using the authorized service test. Confirm the manager
   restarts it and existing claims/results converge without duplicate execution. Verify
   a ready persisted task recovers while no interactive turn supplies a wakeup.
5. Record independent artifact identity and checks. Confirm failures, unavailable models,
   cancellation and budget exhaustion produce precise blockers and no unauthorized launch.
6. Require an exact-candidate full-goal judge verdict after required stages finish. Do not
   infer overall goal completion from a passing subset of tasks. Agents never set job status.

Checkout tests and this template alone do not demonstrate installed unattended behavior.
The final EL handoff names the frozen SHA, test results, independent review identities,
installed native proof, and any remaining operational blocker.
