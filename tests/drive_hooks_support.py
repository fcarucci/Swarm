"""A drive job on the hook Env, for the drive hook tests: a ledger with a requester session, supervisor-launched
seats (resume marker, run file, ledger intent, driver-authored seat authorization, board agent), and payload
builders that have the shapes the hosts send (Claude's PostToolUse tool_response has no exit_code; Codex's is a
plain string)."""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest import mock

from test_hooks_cli import Env

from swarm import drive_hooks, enrolment, hosts, receipts
from swarm.board.autoinit import store_key
from swarm.supervisor import drive_authz, ledger, markers, runner

ISO = "2026-10-10T00:00:00+00:00"
REQ = "req-session"
LEAD = "lead"


class DriveCase(Env):
    """Job J (a drive job: ledger, owner record, board claim) with the requester session REQ. `launch_seat`
    stands in for the supervisor's launch of a seat (what driver_exec does around claim_drive_launch)."""

    def setUp(self):
        super().setUp()
        self.key = store_key(self.cfg)
        self.work = Path(os.path.realpath(self.tmp)) / "work"
        self.work.mkdir()
        with self.board() as b:
            b.open_job("J", None, None, None, "me", goal="G")
            b.set_job_data("J", "drive.enabled", "1")
        self.led = ledger.create(self.key, "J", budgets={}, activated_at=ISO)
        self.edit(lambda l: setattr(l, "requester_session", REQ))
        enrolment.write_job_owner(self.key, job="J", harness="claude", session_id=REQ, cwd=str(self.work))
        self.host = hosts.get("claude")
        drive_hooks._LEDGER_CACHE.clear()
        kick = mock.patch.object(drive_hooks, "kick", return_value=True)
        self.kick = kick.start()
        self.addCleanup(kick.stop)

    # ---- state
    def edit(self, fn):
        led = ledger.load(self.key, "J")
        fn(led)
        led.save()
        self.led = ledger.load(self.key, "J")
        return self.led

    def authorize(self, row, *, action=None, attempt=0, seat="__row__", argv=None):
        """The driver's own authorization of a seat row (what driver_exec.authorize_seat writes)."""
        seat = row.seat if seat == "__row__" else seat
        receipts.write(self.key, receipts.Receipt(
            board_key=self.key, job="J", generation=row.generation, incarnation=self.led.incarnation,
            action_id=action or f"driver|authorize|{row.seat}", attempt=attempt, seat=seat, kind="seat_request",
            record_id=str(row.id), rev=row.revision,
            content_sha=receipts.content_sha("seat_request", drive_authz.seat_decision(row)),
            args_sha=receipts.args_sha(argv or drive_authz.canonical_seat_args(row)),
            expires_at=time.time() + 3600))

    def launch_seat(self, name, role="engineer", *, kind="seat", state="claimed", model="haiku", harness="claude",
                    authorize=True, bind=True):
        """A seat the supervisor launched: its row, the driver's authorization, the ledger intent, the restart
        row, the resume marker bound to its session, the run file, and (bind) its board identity. Returns
        (session id, row)."""
        sid = "sid-" + name
        launch = "L-" + name
        old = "launch:" + launch
        with self.board() as b:
            row = b.request_seat("J", 1, name, role=role, harness=harness, model=model, workdir=str(self.work),
                                 brief_path="B.md", requested_by="person")
            b.allocate_name(old, "J", role)
            b.close_agent(old, "drive predecessor")
            rid = b.record_restart("J", old, old, "drive", harness, 60).id
            if bind:
                b.claim_resume(sid, old, "J")
        if authorize:
            self.authorize(row)
        action = f"gen1|{kind}|{name}|-|0|1"
        self.edit(lambda l: l.intents.append(ledger.Intent(
            launch_id=launch, action_id=action, attempt=1, seat=name, reserved_usd=0.5, reserved_minutes=60.0,
            locks=[], state=state, **({"outcome": "completed", "success": True, "charged_usd": 0.1}
                                      if state == "concluded" else {}))))
        path = markers.write_resume_marker(self.cfg, "J", rid, resume_of=old, name="seat", harness=harness,
                                           session_id=sid)
        runner.write_run({"restart_id": rid, "board": self.key, "job": "J", "name": "seat", "resume_of": old,
                          "marker": str(path), "harness": harness, "limit_seconds": 3600.0,
                          "enrol_seconds": 300.0, "launch_id": launch, "action_id": action, "seat": name,
                          "generation": 1, "session_id": sid})
        self.action, self.restart_id = action, rid
        return sid, row

    def name_of(self, sid: str) -> str:
        with self.board() as b:
            return b.active_agent_name(sid)

    def post_as(self, sid: str, text: str, to: str | None = None) -> int:
        """A board message from the seat session `sid`, as `swarm post`/`done`/`verdict` write it."""
        with self.board() as b:
            return b.post("J", b.active_agent_name(sid), text, to).id

    # ---- payloads
    def payload(self, sid, tool="Bash", command=None, *, agent_id=None, stdout=None, **extra) -> dict:
        p = {"session_id": sid, "cwd": str(self.work), "tool_name": tool,
             "tool_input": {"command": command} if command is not None else dict(extra.pop("tool_input", {}))}
        p.update(extra)
        if agent_id:
            p["agent_id"] = agent_id
        if stdout is not None:   # Claude Code 2.1.284 PostToolUse(Bash): no exit_code
            p["tool_response"] = {"interrupted": False, "isImage": False, "noOutputExpected": False,
                                  "stderr": "", "stdout": stdout}
        return p

    def pre(self, sid, tool="Bash", command=None, **kw) -> str | None:
        return drive_hooks.pre_tool(self.cfg, self.payload(sid, tool, command, **kw), self.host)

    def post(self, sid, command, record=None, *, agent_id=None, host=None, **kw) -> bool:
        """The PostToolUse of `command` that printed `record` (the swarm-record line) last."""
        out = "posted #1\n" + (record + "\n" if record else "")
        return drive_hooks.post_tool(self.cfg, self.payload(sid, "Bash", command, agent_id=agent_id, stdout=out, **kw),
                                     host or self.host)

    def receipt(self, kind, rid, rev=1, generation=1):
        return receipts.find(self.key, "J", kind, str(rid), rev=rev, incarnation=self.led.incarnation,
                             generation=generation, now=time.time())

    def failures(self) -> str:
        from swarm import paths
        p = paths.host_dir() / drive_hooks.RECEIPT_FAILURES
        return p.read_text() if p.exists() else ""
