"""Reassign an existing coordinator without reopening its job or moving workers."""
import dataclasses
import os
from unittest import mock

from support import ROOT  # noqa: F401
from test_hooks_cli import Env
from swarm import cli, enrolment
from swarm.board.autoinit import store_key

OLD = '11111111-aaaa-bbbb-cccc-000000000001'
NEW = '22222222-aaaa-bbbb-cccc-000000000002'


class SessionReassignmentTests(Env):
    def session(self, *args):
        try:
            return self.cli('session', *args)
        except SystemExit as exc:
            return exc.code, '', 'session command unavailable'

    def make_job(self, job='J'):
        self.assertEqual(self.cli('activate', '--job', job, '--session', OLD)[0], 0)
        with self.board() as b:
            worker = b.allocate_name('worker-' + job, job, 'engineer')
            b.post(job, worker, 'durable history ' + job)
        return worker

    def test_reassign_preserves_closed_lifecycle_verdict_agents_and_history(self):
        self.make_job()
        with self.board() as b:
            judge = b.allocate_name('judge', 'J', 'judge')
            b.claim_judge('judge', 'J')
            b.record_verdict('J', judge, 'met', 'accepted')
            b.close_job('J', 'completed', 'owner', forced=False)
            before, agents, messages = b.job_status('J'), b.agents('J'), b.recent_messages(100, job='J')
        rc, out, err = self.session('--job', 'J', '--session', NEW, '--no-routing')
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            after = b.job_status('J')
            self.assertEqual(dataclasses.replace(after, session_id=OLD), before)
            self.assertEqual(b.agents('J'), agents)
            self.assertEqual(b.recent_messages(100, job='J'), messages)
        self.assertIn('workers remain in their original sessions', out)

    def test_existing_owner_pins_and_job_record_reassigned_without_moving_workers(self):
        self.make_job()
        bk = store_key(self.cfg)
        owner = enrolment.write_job_owner(bk, job='J', harness='claude', session_id=OLD,
            cwd=str(self.tmp), roots=[str(self.tmp)], remotes={}, credentials={}, recipe='coding', goal='ship', now=123)
        job = enrolment.write_job(bk, job='J', harness='claude', session_id=OLD, cwd=str(self.tmp), now=124)
        worker = enrolment.write(bk, job='J', agent_key='w', harness='claude', session_id=OLD,
                                 cwd=str(self.tmp), now=125)
        rc, _, err = self.session('--job', 'J', '--session', NEW, '--no-routing')
        self.assertEqual(rc, 0, err)
        self.assertEqual(enrolment.find_job_owner(bk, 'J'), dataclasses.replace(owner, session_id=NEW))
        self.assertEqual(enrolment.find_job(bk, 'J'), dataclasses.replace(job, session_id=NEW))
        self.assertEqual(enrolment.find(bk, 'w'), worker)

    def test_absent_owner_is_reported_and_never_created(self):
        with self.board() as b:
            b.open_job('remote', None, None, OLD, 'other-user')
        rc, out, err = self.session('--job', 'remote', '--session', NEW, '--no-routing')
        self.assertEqual(rc, 0, err)
        self.assertIn('local owner absent', out)
        self.assertIsNone(enrolment.find_job_owner(store_key(self.cfg), 'remote'))

    def test_unknown_job_is_not_created(self):
        self.assertNotEqual(self.session('--job', 'missing', '--session', NEW)[0], 0)
        with self.board() as b:
            self.assertIsNone(b.job_status('missing'))

    def test_clear_only_removes_display_binding(self):
        self.make_job()
        before = enrolment.find_job_owner(store_key(self.cfg), 'J')
        marker = cli._marker(self.cfg, 'J').read_bytes()
        rc, out, err = self.session('--job', 'J', '--clear')
        self.assertEqual(rc, 0, err)
        with self.board() as b:
            self.assertIsNone(b.job_status('J').session_id)
        self.assertEqual(enrolment.find_job_owner(store_key(self.cfg), 'J'), before)
        self.assertEqual(cli._marker(self.cfg, 'J').read_bytes(), marker)
        self.assertIn('ownership and routing unchanged', out)

    def test_compact_watch_shows_exactly_two_assigned_jobs(self):
        for name in ('loom-one', 'loom-two', 'unrelated'):
            self.make_job(name)
        for name in ('loom-one', 'loom-two'):
            self.assertEqual(self.session('--job', name, '--session', NEW, '--no-routing')[0], 0)
        view = dict(compact=True, session=NEW, offset=0, wrap=False, max_offset=0,
                    all_agents=False, recent_minutes=10)
        with self.board() as b, mock.patch('shutil.get_terminal_size', return_value=os.terminal_size((160, 60))):
            self.assertEqual({j.job for j in b.session_shown_jobs(NEW)}, {'loom-one', 'loom-two'})
            text = '\n'.join(cli._watch_frame(b, None, 2, False, True, view))
        self.assertIn('loom-one', text)
        self.assertIn('loom-two', text)
        self.assertNotIn('unrelated', text)

    def test_routing_collision_fails_before_mutation(self):
        self.make_job()
        self.cli('activate', '--job', 'other', '--session', NEW)
        rc, _, err = self.session('--job', 'J', '--session', NEW)
        self.assertNotEqual(rc, 0)
        self.assertIn('--no-routing', err)
        with self.board() as b:
            self.assertEqual(b.job_status('J').session_id, OLD)

    def test_routing_moves_only_existing_coordinator_marker(self):
        self.make_job()
        child = cli._marker(self.cfg, 'J').with_name('J--child.json')
        cli._write_marker(child, {'job': 'J', 'session_id': 'child', 'attached': True})
        rc, _, err = self.session('--job', 'J', '--session', NEW)
        self.assertEqual(rc, 0, err)
        self.assertEqual(cli._read_marker(cli._marker(self.cfg, 'J'))['session_id'], NEW)
        self.assertEqual(cli._read_marker(child)['session_id'], 'child')

    def test_enrolled_agent_cannot_transfer_coordinator(self):
        self.make_job()
        enrolment.write(store_key(self.cfg), job='J', agent_key='child', harness='codex',
                         session_id='child', cwd=str(self.tmp))
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'child'}):
            rc, _, err = self.session('--job', 'J', '--session', NEW, '--no-routing')
        self.assertNotEqual(rc, 0)
        self.assertIn('enrolled agent', err)
        with self.board() as b:
            self.assertEqual(b.job_status('J').session_id, OLD)

    def test_invalid_session_is_rejected_before_changes(self):
        self.make_job()
        self.assertNotEqual(self.session('--job', 'J', '--session', '../unsafe')[0], 0)
        with self.board() as b:
            self.assertEqual(b.job_status('J').session_id, OLD)

    def test_compare_and_set_rejects_stale_binding_and_unknown_job(self):
        self.make_job()
        with self.board() as b:
            self.assertFalse(b.reassign_job_session('missing', NEW, expected_session=OLD))
            self.assertFalse(b.reassign_job_session('J', NEW, expected_session='stale'))
            self.assertEqual(b.job_status('J').session_id, OLD)
            self.assertTrue(b.reassign_job_session('J', NEW, expected_session=OLD))
            self.assertTrue(b.reassign_job_session('J', None, expected_session=NEW))
            self.assertTrue(b.reassign_job_session('J', NEW, expected_session=None))

    def test_corrupt_owner_refuses_before_board_mutation(self):
        self.make_job()
        bk = store_key(self.cfg)
        rec = enrolment.find_job_owner(bk, 'J')
        self.assertIsNotNone(rec)
        ownerpath = enrolment.enrolled_dir() / ('owner-' + enrolment._digest('owner', bk, 'J') + '.json')
        ownerpath.write_text('{bad json')
        rc, _, _ = self.session('--job', 'J', '--session', NEW, '--no-routing')
        self.assertNotEqual(rc, 0)
        with self.board() as b:
            self.assertEqual(b.job_status('J').session_id, OLD)

    def test_no_routing_preserves_all_markers_and_is_allowed_from_root_session(self):
        self.make_job()
        before = {p.name: p.read_bytes() for p in self.markers.glob('*.json')}
        with mock.patch.dict(os.environ, {'CODEX_SESSION_ID': NEW, 'CODEX_THREAD_ID': NEW}):
            rc, _, err = self.session('--job', 'J', '--session', NEW, '--no-routing')
        self.assertEqual(rc, 0, err)
        self.assertEqual({p.name: p.read_bytes() for p in self.markers.glob('*.json')}, before)

    def test_target_worker_session_cannot_become_owner(self):
        self.make_job()
        enrolment.write(store_key(self.cfg), job='J', agent_key='workerSID', harness='codex',
                         session_id='workerSID', cwd=str(self.tmp))
        rc, _, err = self.session('--job', 'J', '--session', 'workerSID', '--no-routing')
        self.assertNotEqual(rc, 0)
        self.assertIn('enrolled agent', err)
        with self.board() as b:
            self.assertEqual(b.job_status('J').session_id, OLD)

    def test_read_only_board_refuses_session_mutation(self):
        from swarm.board.base import refuse_writes, ReadOnlyBoard
        self.make_job()
        with self.board() as b:
            refuse_writes(b, 'test')
            with self.assertRaises(ReadOnlyBoard):
                b.reassign_job_session('J', NEW, expected_session=OLD)

    def test_binding_only_then_routing_can_repair_previous_marker(self):
        self.make_job()
        self.assertEqual(self.session('--job', 'J', '--session', NEW, '--no-routing')[0], 0)
        rc, out, err = self.session('--job', 'J', '--session', NEW)
        self.assertEqual(rc, 0, err)
        self.assertEqual(cli._read_marker(cli._marker(self.cfg, 'J'))['session_id'], NEW)

    def test_busy_coordinator_update_refuses_without_mutating(self):
        import threading
        self.make_job()
        held, release = threading.Event(), threading.Event()
        def hold():
            with enrolment.coordinator_lock(store_key(self.cfg), 'another-job'):
                held.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            rc, _, err = self.session('--job', 'J', '--session', NEW, '--no-routing')
            self.assertNotEqual(rc, 0)
            self.assertIn('in progress', err)
            with self.board() as b:
                self.assertEqual(b.job_status('J').session_id, OLD)
        finally:
            release.set()
            thread.join(5)

    def test_failed_compensation_does_not_restore_stale_owner(self):
        self.make_job()
        bk = store_key(self.cfg)
        owner = enrolment.find_job_owner(bk, 'J')
        def concurrent_update(records, session):
            with self.board() as b:
                b.reassign_job_session('J', 'third', expected_session=NEW)
            enrolment._store(dataclasses.replace(owner, session_id='third'),
                'owner-' + enrolment._digest('owner', bk, 'J') + '.json')
            raise ValueError('concurrent change')
        with mock.patch.object(enrolment, 'reassign_coordinator_records', side_effect=concurrent_update):
            rc, _, err = self.session('--job', 'J', '--session', NEW, '--no-routing')
        self.assertNotEqual(rc, 0)
        with self.board() as b:
            self.assertEqual(b.job_status('J').session_id, 'third')
        self.assertEqual(enrolment.find_job_owner(bk, 'J').session_id, 'third')

    def test_board_worker_target_without_local_enrolment_is_rejected(self):
        self.make_job()
        with self.board() as b:
            b.allocate_name('remote-worker-session', 'J', 'engineer')
        rc, _, err = self.session('--job', 'J', '--session', 'remote-worker-session', '--no-routing')
        self.assertNotEqual(rc, 0)
        self.assertIn('worker', err)

    def test_coordinator_cleanup_cannot_interleave_with_reassignment(self):
        import threading
        self.make_job()
        bk = store_key(self.cfg)
        enrolment.write_job(bk, job='J', harness='codex', session_id=OLD, cwd=str(self.tmp), now=1)
        held, release = threading.Event(), threading.Event()
        def hold():
            with enrolment.coordinator_lock(bk, 'J'):
                held.set()
                release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            with self.assertRaises(BlockingIOError):
                enrolment.remove_job(bk, 'J')
            self.assertEqual(enrolment.prune(1, now=10), 0)
            self.assertIsNotNone(enrolment.find_job(bk, 'J'))
        finally:
            release.set()
            thread.join(5)
