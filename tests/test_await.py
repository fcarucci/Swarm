"""swarm await: the orchestrator's items, its own cursor, items re-reported until resolved, duplicates collapsed."""
import unittest

from support import MemoryHarness, add_backend_variants
from swarm import awaititems


class AwaitContract:
    def setUp(self):
        self.h = self.harness_factory()
        self.h.reset()
        self.addCleanup(self.h.close)
        self.b = self.h.board()
        self.addCleanup(self.b.close)
        self.b.open_job('j', None, None, None, None, goal='ship')
        self.b.allocate_name('pm', 'j', role='project_manager')
        self.b.allocate_name('eng', 'j', role='engineer')
        self.pm = self.b.active_agent_name('pm')
        self.eng = self.b.active_agent_name('eng')

    def test_each_session_sees_every_item_and_the_read_cursor_is_untouched(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        question = self.b.open_blocker('j', 'question', 'human', 'Which colour?', created_by=self.eng)
        expected = sorted([f'Q{question.id}', 'BRANCH READY feat/x@abcdef1'])
        self.assertEqual(sorted(i.key for i in awaititems.report(self.b, 'j', 's1')), expected)
        self.assertEqual(sorted(i.key for i in awaititems.report(self.b, 'j', 's2')), expected)
        read = self.b.read_unread(name=self.pm, job='j')
        self.assertEqual([m.message for m in read.messages], ['BRANCH READY feat/x@abcdef1'])

    def test_unresolved_item_is_reported_again_on_the_next_call(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        self.assertEqual(len(awaititems.report(self.b, 'j', 's1')), 1)
        again = awaititems.report(self.b, 'j', 's1')
        self.assertEqual([i.key for i in again], ['BRANCH READY feat/x@abcdef1'])
        self.b.post('j', self.pm, 'FINALIZED feat/x@abcdef1 landed')
        self.assertEqual(awaititems.report(self.b, 'j', 's1'), [])

    def test_duplicate_posts_collapse_into_one_item(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1 (again)')
        items = awaititems.report(self.b, 'j', 's1')
        self.assertEqual([i.key for i in items], ['BRANCH READY feat/x@abcdef1'])

    def test_newer_branch_ready_supersedes_the_older_sha(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        self.b.post('j', self.eng, 'BRANCH READY feat/x@1234567')
        items = awaititems.report(self.b, 'j', 's1')
        self.assertEqual([i.key for i in items], ['BRANCH READY feat/x@1234567'])

    def test_landing_another_sha_does_not_settle_a_branch_ready(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        self.b.post('j', self.pm, 'FINALIZED feat/x@1234567 landed')
        self.assertEqual([i.key for i in awaititems.report(self.b, 'j', 's1')], ['BRANCH READY feat/x@abcdef1'])
        self.b.post('j', self.pm, 'INTEGRATED feat/x@abcdef1')
        self.assertEqual(awaititems.report(self.b, 'j', 's1'), [])

    def test_orchestrator_own_posts_are_not_items(self):
        self.b.post('j', self.pm, 'BRANCH READY feat/y@aaaaaaa')
        self.assertEqual(awaititems.report(self.b, 'j', 's1'), [])

    def test_question_stays_open_until_its_blocker_is_resolved(self):
        blocker = self.b.open_blocker('j', 'question', 'human', 'Which colour?', created_by=self.eng)
        self.assertEqual([i.key for i in awaititems.report(self.b, 'j', 's1')], [f'Q{blocker.id}'])
        self.assertEqual([i.key for i in awaititems.report(self.b, 'j', 's1')], [f'Q{blocker.id}'])
        self.b.resolve_blocker(blocker.id, how='blue', actor=self.pm)
        self.assertEqual(awaititems.report(self.b, 'j', 's1'), [])

    def test_every_check_of_a_blocking_await_runs_the_pm_stall_hook(self):
        calls = []
        self.assertEqual(awaititems.await_items(self.b, 'j', 's1', timeout=0, poll=0,
                                                on_poll=lambda: calls.append(1)), [])
        self.assertEqual(calls, [1])

    def test_a_blocker_whose_pr_landed_is_flagged_on_its_await_item(self):
        blocker = self.b.open_blocker('j', 'question', 'human', 'Rebase on main once PR #24 is in?', created_by=self.eng)
        self.b.post('j', self.eng, 'PR #24 landed on main')
        items = awaititems.report(self.b, 'j', 's1')
        self.assertEqual([i.key for i in items], [f'Q{blocker.id}'])
        self.assertIn('POSSIBLY STALE: PR #24 landed', awaititems.format_item(items[0]))

    def test_post_addressed_to_the_orchestrator_stays_until_it_replies(self):
        self.b.post('j', self.eng, 'which branch should I rebase on?', to=self.pm)
        items = awaititems.report(self.b, 'j', 's1')
        self.assertEqual([i.kind for i in items], ['addressed'])
        self.assertIn('reply to', items[0].action)
        self.b.post('j', self.pm, 'main')
        self.assertEqual(awaititems.report(self.b, 'j', 's1'), [])

    def test_blocking_await_returns_when_a_post_arrives(self):
        self.assertEqual(awaititems.await_items(self.b, 'j', 's1', timeout=0), [])
        self.b.post('j', self.eng, 'ORDER land feat/x after CI')
        items = awaititems.await_items(self.b, 'j', 's1', timeout=5, poll=0.05)
        self.assertEqual([i.kind for i in items], ['order'])

    def test_timeout_without_items_returns_nothing_and_records_nothing(self):
        self.assertEqual(awaititems.await_items(self.b, 'j', 's1', timeout=0), [])
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        self.assertEqual(len(awaititems.await_items(self.b, 'j', 's1', timeout=0)), 1)

    def test_reported_item_is_due_again_only_after_remind_seconds(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        awaititems.report(self.b, 'j', 's1')
        self.assertEqual(awaititems.await_items(self.b, 'j', 's1', timeout=0, remind=3600), [])
        self.assertEqual(len(awaititems.await_items(self.b, 'j', 's1', timeout=0, remind=0)), 1)

    def test_new_post_wakes_the_await_with_every_open_item(self):
        self.b.post('j', self.eng, 'BRANCH READY feat/x@abcdef1')
        awaititems.report(self.b, 'j', 's1')
        self.assertEqual(awaititems.await_items(self.b, 'j', 's1', timeout=0, remind=3600), [])
        self.b.post('j', self.eng, 'ORDER land it')
        items = awaititems.await_items(self.b, 'j', 's1', timeout=0, remind=3600)
        self.assertEqual(sorted(i.key for i in items), sorted(['BRANCH READY feat/x@abcdef1', f'ORDER #{self.b.last_message_id("j")}']))

    def test_job_state_change_is_announced_once(self):
        awaititems.report(self.b, 'j', 's1')
        self.b.pause_job('j', by='human', reason='hold')
        items = awaititems.report(self.b, 'j', 's1')
        self.assertEqual([i.kind for i in items], ['state'])
        self.assertEqual(awaititems.report(self.b, 'j', 's1'), [])

    def test_is_resolved_follows_the_board(self):
        blocker = self.b.open_blocker('j', 'question', 'human', 'Which colour?', created_by=self.eng)
        item = awaititems.report(self.b, 'j', 's1')[0]
        self.assertFalse(awaititems.is_resolved(item, self.b))
        self.b.resolve_blocker(blocker.id, how='blue', actor=self.pm)
        self.assertTrue(awaititems.is_resolved(item, self.b))


class AwaitTests(AwaitContract, unittest.TestCase):
    harness_factory = staticmethod(lambda key=None: MemoryHarness())


add_backend_variants(globals(), AwaitTests)
