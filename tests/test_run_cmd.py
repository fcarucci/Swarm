"""`swarm run`: queues a named launch for the job; the stateless pass starts it (lib/swarm/cli.py _board_run)."""
import argparse
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support
from support import drive_backends
from swarm import cli


def run_args(**kw):
    base = dict(job="J", name="rev", role="reviewer", brief="check the diff", harness="claude", model=None,
                workdir=None)
    base.update(kw)
    return argparse.Namespace(**base)


class RunCmd(unittest.TestCase):
    harness_factory = staticmethod(drive_backends()["memory"])

    def setUp(self):
        self.h = self.harness_factory(self.id())
        self.addCleanup(self.h.close)
        self.h.reset()
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job("J", None, None, None, "me", goal="ship it")

    def run_cmd(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli._board_run(self.b, self.h.cfg, run_args(**kw))
        return rc, out.getvalue(), err.getvalue()

    def test_queues_the_launch_with_the_given_workdir_and_records_it_for_the_job(self):
        rc, out, _ = self.run_cmd(workdir="/work/shared")
        self.assertEqual(rc, None)
        self.assertEqual(out, "rev: queued\n")
        row = self.b.get_launch("J", "rev")
        self.assertEqual((row.state, row.role, row.workdir, row.queued_by),
                         ("queued", "reviewer", "/work/shared", "swarm run"))
        self.assertEqual(self.b.job_data("J").get("workdir"), "/work/shared")

    def test_a_later_launch_reuses_the_recorded_workdir(self):
        self.run_cmd(workdir="/work/shared")
        rc, out, _ = self.run_cmd(name="rev2")
        self.assertIsNone(rc)
        self.assertEqual(self.b.get_launch("J", "rev2").workdir, "/work/shared")

    def test_no_workdir_anywhere_is_refused(self):
        rc, _, err = self.run_cmd()
        self.assertEqual(rc, 1)
        self.assertIn("no work dir", err)
        self.assertIsNone(self.b.get_launch("J", "rev"))

    def test_an_unknown_job_is_refused(self):
        rc, _, err = self.run_cmd(job="NOPE", workdir="/work/shared")
        self.assertEqual(rc, 1)
        self.assertIn("NOPE", err)


if __name__ == "__main__":
    unittest.main()
