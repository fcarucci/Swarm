"""Atomic create-new contract, including concurrent callers and failed commits."""
import concurrent.futures
import os
import multiprocessing
import uuid
import threading
import unittest
from unittest import mock

from support import MemoryHarness, FileHarness, SqliteHarness, PostgresHarness
from swarm.board import BoardError, open_board


def process_create(cfg, barrier, queue, request, crash=False, job="process"):
    with open_board(cfg) as board:
        if crash:
            board._write_meta = lambda *args: os._exit(73)
        if barrier:
            barrier.wait(timeout=15)
        try:
            receipt = board.create_job_once(job, 'description', 'task', 'root', 'owner',
                                            goal='goal', request_id=request, request_hash='abc')
            if queue:
                queue.put(receipt)
        except BoardError:
            queue.put(None)


class CreateContract:
    factory = None

    def setUp(self):
        self.request_id = str(uuid.uuid4())
        self.h = self.factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)

    def create(self, b=None, **kw):
        args = dict(job='new', description='description', task='task', session_id='root',
                    created_by='owner', goal='goal', request_id=self.request_id, request_hash='abc')
        args.update(kw)
        return (b or self.b).create_job_once(**args)

    def test_receipt_and_closed_retry_no_mutation(self):
        receipt = self.create()
        row = next(j for j in self.b.jobs() if j.job == 'new')
        self.assertEqual((row.status, row.task, row.goal, row.session_id),
                         ('active', 'task', 'goal', 'root'))
        self.assertEqual(receipt['created_at'], row.created_at.isoformat())
        self.assertEqual(row.created_at, row.activated_at)
        self.b.close_job('new', 'completed', 'done')
        before = self.b.jobs()
        self.assertEqual(receipt, self.create())
        self.assertEqual(before, self.b.jobs())

    def test_conflicts_never_change_existing(self):
        self.create()
        before = self.b.jobs()
        for kw in ({'request_id': 'other'}, {'request_hash': 'other'},
                   {'session_id': 'other'}, {'job': 'other'}):
            with self.subTest(kw=kw), self.assertRaises(BoardError):
                self.create(**kw)
        self.assertEqual(before, self.b.jobs())

    def test_legacy_job_refused(self):
        self.b.ensure_job('new', 'original')
        before = self.b.jobs()
        with self.assertRaises(BoardError):
            self.create()
        self.assertEqual(before, self.b.jobs())

    def test_parallel_identical_and_competing_requests(self):
        for competing in (False, True):
            barrier = threading.Barrier(8)
            def run(i):
                with self.h.board() as board:
                    barrier.wait(timeout=10)
                    try:
                        return self.create(board, job='race'+str(competing),
                                           request_id=self.request_id+('race'+str(i) if competing else 'same'))
                    except BoardError:
                        return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(run, range(8)))
            accepted = [r for r in results if r]
            self.assertEqual(len(accepted), 1 if competing else 8)
            self.assertTrue(all(r == accepted[0] for r in accepted))

    def test_receipt_write_failure_rolls_back_job(self):
        original = self.b._write_meta
        def fail_after_write(*args):
            original(*args)
            raise RuntimeError('crash')
        with mock.patch.object(self.b, '_write_meta', side_effect=fail_after_write):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                self.create()
        self.assertEqual([], self.b.jobs())
        self.create()
        self.assertEqual(['new'], [j.job for j in self.b.jobs()])

    def test_purged_job_retry_returns_receipt_without_recreation(self):
        receipt = self.create()
        self.b.close_job('new', 'completed', 'done')
        self.h.backdate_job('new', created_at=90*86400, activated_at=90*86400,
                            finished_at=90*86400)
        self.b.purge()
        self.assertEqual([], self.b.jobs(include_closed=True))
        self.assertEqual(receipt, self.create())
        self.assertEqual([], self.b.jobs(include_closed=True))

    def test_process_races_and_killed_writer(self):
        if self.h.name == 'memory':
            self.skipTest('in-process reference backend')
        ctx = multiprocessing.get_context('spawn')
        # Kill after the insert but before receipt persistence/commit.
        child = ctx.Process(target=process_create,
                            args=(self.h.cfg, None, None, self.request_id, True))
        child.start()
        child.join(20)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 73)
        self.assertEqual([], self.b.jobs(include_closed=True))
        for competing in (False, True):
            barrier, queue = ctx.Barrier(4), ctx.Queue()
            processes = [ctx.Process(target=process_create, args=(
                self.h.cfg, barrier, queue, self.request_id+(str(i) if competing else 'same'), False, 'process'+str(competing)))
                for i in range(4)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(25)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            receipts = [queue.get(timeout=5) for _ in processes]
            accepted = [r for r in receipts if r]
            self.assertEqual(len(accepted), 1 if competing else 4)
            if accepted:
                self.assertTrue(all(r == accepted[0] for r in accepted))
            queue.close()


class MemoryCreate(CreateContract, unittest.TestCase):
    factory = MemoryHarness


class FileCreate(CreateContract, unittest.TestCase):
    factory = FileHarness


class SqliteCreate(CreateContract, unittest.TestCase):
    factory = SqliteHarness


@unittest.skipUnless(os.environ.get('SWARM_TEST_CONFIG'), 'throwaway Postgres config required')
class PostgresCreate(CreateContract, unittest.TestCase):
    factory = PostgresHarness
