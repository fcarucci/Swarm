"""The first writer of a memory ref wins under real concurrency (Board.save_memory_ref, R7).

N separate PROCESSES (one per hook call in real life) save the same document_id as different
agents at once: exactly one gets "inserted", every other one "kept", and the stored row is the
winner's. Runs on SQLite and the file backend always, and on Postgres when $SWARM_TEST_CONFIG
names a THROWAWAY database (the harness refuses the live one).
"""
from __future__ import annotations

import lzma
import multiprocessing
import os
import unittest

from support import SMALL_POOL, FileHarness, PostgresHarness, SqliteHarness  # noqa: F401  (sets sys.path)

from swarm.board import MemoryRef, open_board  # noqa: E402

CTX = multiprocessing.get_context("spawn")   # fresh interpreters: nothing shared but the store
N = 8


def _save(cfg, key, barrier, out):   # top level: spawn imports this module by name
    with open_board(cfg) as b:
        barrier.wait()
        body = f"excerpt of {key}\n".encode()
        out.put((key, b.save_memory_ref(MemoryRef(
            document_id="race-doc", bank="notes", job="j", agent_key=key, agent_name=f"Agent {key}",
            harness="claude", host="h", session_id=f"s-{key}", tool_call_id=f"toolu-{key}", writer="note-tool",
            excerpt=lzma.compress(body), raw_bytes=len(body)))))


class RaceBase:
    harness_factory = None

    def setUp(self):
        self.h = self.harness_factory()
        self.addCleanup(self.h.close)
        self.h.reset()

    def test_exactly_one_process_inserts_and_its_row_is_stored(self):
        barrier, out = CTX.Barrier(N, timeout=120), CTX.Queue()
        procs = [CTX.Process(target=_save, args=(self.h.cfg, f"k{i}", barrier, out)) for i in range(N)]
        for p in procs:
            p.start()
        try:
            results = dict(out.get(timeout=120) for _ in procs)
        finally:
            for p in procs:
                p.join(120)
            for p in procs:
                if p.is_alive():
                    p.terminate()   # a failed child must not keep writing across fixture reset
                    p.join(30)
            out.close()
            out.join_thread()
        self.assertEqual([p.exitcode for p in procs], [0] * N)
        winners = [k for k, r in results.items() if r == "inserted"]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(sorted(r for r in results.values() if r != "inserted"), ["kept"] * (N - 1))
        [w] = winners
        with self.h.board() as b:
            [row] = b.memory_refs(document_id="race-doc")
            self.assertEqual((row.agent_key, row.agent_name, row.session_id, row.tool_call_id),
                             (w, f"Agent {w}", f"s-{w}", f"toolu-{w}"))
            self.assertEqual(b.memory_ref_excerpt("race-doc"), f"excerpt of {w}\n".encode())


class SqliteRace(RaceBase, unittest.TestCase):
    harness_factory = SqliteHarness


class FileRace(RaceBase, unittest.TestCase):
    harness_factory = FileHarness


@unittest.skipUnless(os.environ.get("SWARM_TEST_CONFIG"),
                     "set SWARM_TEST_CONFIG to a throwaway Postgres board config to run")
class PostgresRace(RaceBase, unittest.TestCase):
    harness_factory = staticmethod(lambda: PostgresHarness(os.environ["SWARM_TEST_CONFIG"]))


if __name__ == "__main__":
    unittest.main()
