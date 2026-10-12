"""A seat's named Job Object on Windows (review rr2): a process other than the runner that made the job can
stop the seat, grandchildren included, and tell whether any process of it is left, not only its leader."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
import unittest
from pathlib import Path

import support  # noqa: F401  (puts lib/ on sys.path)

from swarm.supervisor import runner

REPO = Path(__file__).resolve().parent.parent
WINDOWS = sys.platform == "win32"

# the leader starts a grandchild that outlives it, prints its pid, then sleeps (or exits at once with "exit")
LEADER = ("import subprocess, sys, time\n"
          "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
          "print(g.pid, flush=True)\n"
          "if 'exit' not in sys.argv: time.sleep(120)\n")


def outside(op: str, run: dict) -> str:
    """runner.<op> on the run in a fresh process (it holds no handle of the job: it must open it by name)."""
    code = ("import json, sys; from swarm.supervisor import runner; run = json.loads(sys.argv[1])\n"
            "if sys.argv[2] == 'stop': runner._stop_session(run); print('stopped')\n"
            "else: print(runner.group_presence(run))\n")
    env = {**os.environ, "PYTHONPATH": str(REPO / "lib")}
    r = subprocess.run([sys.executable, "-c", code, json.dumps(run), op], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def pid_alive(pid: int) -> bool:
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000 | 0x00100000, False, pid)   # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(ctypes.c_void_p(h), 0) == 0x102   # WAIT_TIMEOUT: still running
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


@unittest.skipUnless(WINDOWS, "Windows Job Objects")
class NamedJobObjectTests(unittest.TestCase):
    def contained(self, *args: str):
        rid = 900000 + secrets.randbelow(99999)
        run = {"restart_id": rid, "run_tag": f"r{rid}-{secrets.token_hex(8)}", "contained": "job"}
        proc = subprocess.Popen([sys.executable, "-c", LEADER, *args], stdout=subprocess.PIPE,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 0x4)   # CREATE_SUSPENDED
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        runner._job_contain(rid, proc, runner.job_name(run))
        self.addCleanup(self.let_go, rid)
        run["child_pid"] = proc.pid
        grandchild = int(proc.stdout.readline())
        return run, proc, grandchild

    @staticmethod
    def let_go(rid: int) -> None:
        job = runner._JOBS.pop(rid, None)
        if job is not None:
            runner._k32().CloseHandle(job)

    def test_another_process_stops_the_seat_and_its_grandchild_then_presence_is_absent(self):
        run, proc, grandchild = self.contained()
        self.assertEqual(outside("presence", run), "present")
        self.assertEqual(outside("stop", run), "stopped")
        proc.wait(10)
        deadline = time.monotonic() + 10
        while pid_alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(pid_alive(grandchild))
        self.assertEqual(outside("presence", run), "absent")

    def test_a_seat_whose_leader_exited_is_present_while_its_grandchild_runs(self):
        run, proc, grandchild = self.contained("exit")
        proc.wait(30)
        self.assertTrue(pid_alive(grandchild))
        self.assertEqual(outside("presence", run), "present")
        outside("stop", run)
        self.assertEqual(outside("presence", run), "absent")

    def test_when_the_runner_lets_go_of_the_job_the_seat_ends_and_reads_absent(self):
        run, proc, grandchild = self.contained()
        runner._stop_session(run, proc)               # the runner's own stop: closes its handle
        self.assertNotIn(run["restart_id"], runner._JOBS)
        self.assertFalse(pid_alive(grandchild))
        self.assertEqual(outside("presence", run), "absent")

    def test_the_job_has_a_name_only_for_a_run_with_a_tag(self):
        self.assertEqual(runner.job_name({"run_tag": "r5-abc123"}), "Local\\swarm-r5-abc123")
        self.assertIsNone(runner.job_name({}))
        self.assertIsNone(runner.job_name({"run_tag": "r5-..\\x"}))

    def test_a_job_that_cannot_be_opened_is_unknown_not_absent(self):
        run = {"restart_id": 5, "contained": "job"}           # no tag: no named job to look at
        self.assertEqual(runner.group_presence(run), "unknown")


class JobNameTests(unittest.TestCase):
    def test_the_name_is_derived_from_the_run_tag_in_the_local_namespace(self):
        self.assertEqual(runner.job_name({"run_tag": "r12-0f0f"}), "Local\\swarm-r12-0f0f")
        self.assertIsNone(runner.job_name({"run_tag": 3}))


if __name__ == "__main__":
    unittest.main()
