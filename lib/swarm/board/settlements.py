"""Immutable native late-completion receipts, separate from agent-writable job data.

There is no public insert operation. The writer revalidates the host-private native
review assignment under the job transaction before appending receipt and event.
"""
import copy
import json

COMPLETIONS = 'native.completion-receipts.v1'
FINALIZATIONS = 'native.finalization-receipts.v1'


def _memory_rows(store, kind):
    return json.loads(store.meta.get(kind, '{}'))


def _memory_put(store, kind, key, value):
    rows = _memory_rows(store, kind)
    rows[key] = value
    store.meta[kind] = json.dumps(rows, sort_keys=True)


def _failure_rows(board, js):
    from swarm.settlement import eligible
    from swarm.board.base import run_start
    if not eligible(js):
        return None
    rows = [r for r in board.launches(js.job) if r.role in ('lead', 'judge')
            and r.queued_by in ('swarm pass', 'swarm run') and r.state in ('done', 'failed')
            and run_start(js) <= r.updated_at <= js.finished_at]
    return rows if len(rows) >= int(js.outcome.split(' ', 1)[0]) else None


def sql_schema(pg=False):
    ts = 'timestamptz' if pg else 'TEXT'
    return f'''CREATE TABLE IF NOT EXISTS completion_receipts (
        job TEXT NOT NULL, closure_sha TEXT NOT NULL, body TEXT NOT NULL,
        created_at {ts} NOT NULL, event_id BIGINT NOT NULL,
        PRIMARY KEY (job, closure_sha));
        CREATE TABLE IF NOT EXISTS finalization_receipts (
        job TEXT NOT NULL, closure_sha TEXT NOT NULL, verdict_id BIGINT NOT NULL,
        body TEXT NOT NULL, created_at {ts} NOT NULL,
        PRIMARY KEY (job, closure_sha, verdict_id));'''


class MemorySettlements:
    def native_failure_provenance(self, job):
        from swarm.passcmd import notice
        from swarm.board.base import run_start
        with self._s().lock:
            js = self.job_status(job)
            rows = _failure_rows(self, js)
            if rows is None:
                return None
            msg = next((m for m in reversed(self._s().messages) if m['job'] == job
                and m['agent_name'] == 'swarm pass' and m['message'] == notice(job, js.outcome)
                and run_start(js) <= m['created_at'] <= js.finished_at), None)
            return dict(notice_id=msg['id'], launches=[[r.id, r.revision] for r in rows]) if msg else None

    def finalization_receipt(self, job):
        with self._s().lock:
            rows = [r for r in _memory_rows(self._s(), FINALIZATIONS).values() if r['job'] == job]
            return copy.deepcopy(max(rows, key=lambda r: r['at'])) if rows else None

    def finalize_late_review(self, job):
        from swarm import settlement
        s = self._s()
        with self._task_tx(job):
            proof = settlement.finalization_ready(self, self.cfg, job)
            if proof is None:
                return None
            old = self.finalization_receipt(job)
            if old and settlement.same_finalization(old, proof):
                return old
            key = job + '\0' + proof['closure']['sha'] + '\0' + str(proof['verdict_id'])
            if key in _memory_rows(s, FINALIZATIONS):
                return None
            before = copy.deepcopy((s.messages, s.agents, s.next_id, s.msg_version, s.state_version, s.meta))
            try:
                posted = self.post(job, proof['executor'], 'FINALIZED ' + proof['candidate'])
                if not any(m.id == posted.id and m.message == 'FINALIZED ' + proof['candidate']
                           for m in self.pipeline_messages(job)):
                    raise ValueError('native finalization visibility record was truncated')
                proof = dict(proof, message_id=posted.id, at=self.now().isoformat())
                _memory_put(s, FINALIZATIONS, key, proof)
                s.touch()
            except BaseException:
                s.messages, s.agents, s.next_id, s.msg_version, s.state_version, s.meta = before
                raise
            return copy.deepcopy(proof)

    def completion_receipt(self, job):
        with self._s().lock:
            rows = [r for r in _memory_rows(self._s(), COMPLETIONS).values() if r['job'] == job]
            return copy.deepcopy(max(rows, key=lambda r: r['completed_at'])) if rows else None

    def settle_late_review(self, job):
        from swarm import settlement
        s = self._s()
        with self._task_tx(job):
            old = self.completion_receipt(job)
            if old and settlement.projected(self.job_status(job), old):
                return old
            receipt = settlement.ready(self, self.cfg, job)
            if receipt is None:
                return None
            key = job + '\0' + receipt['closure']['sha']
            if key in _memory_rows(s, COMPLETIONS):
                return None  # stale acceptance cannot be refreshed over immutable history
            # Restore in-memory state as well as FileStore's on-disk transaction
            # if event publication or receipt construction fails.
            events, next_id, metadata, version = copy.deepcopy((s.events, s.next_event_id, s.meta, s.state_version))
            try:
                event_id, _ = self.post_event(job, 'completion', receipt['closure']['sha'],
                    'Native audited full-goal completion', to='@orchestrator', source='native-settlement')
                receipt = dict(receipt, event_id=event_id, completed_at=self.now().isoformat())
                _memory_put(s, COMPLETIONS, key, receipt)
                s.touch()
            except BaseException:
                s.events, s.next_event_id, s.meta, s.state_version = events, next_id, metadata, version
                raise
            return copy.deepcopy(receipt)


class SqlSettlements:
    def native_failure_provenance(self, job):
        from swarm.passcmd import notice
        from swarm.board.base import run_start
        with self._event_tx(False) as c:
            js = self.job_status(job)
            rows = _failure_rows(self, js)
            if rows is None:
                return None
            msg = self._exec(c, "SELECT id FROM messages WHERE job=? AND agent_name='swarm pass' "
                'AND message=? AND created_at>=? AND created_at<=? ORDER BY id DESC LIMIT 1',
                (job, notice(job, js.outcome), self._launch_time(run_start(js)), self._launch_time(js.finished_at))).fetchone()
            return dict(notice_id=msg[0], launches=[[r.id, r.revision] for r in rows]) if msg else None

    def finalization_receipt(self, job):
        with self._event_tx(False) as c:
            row = self._exec(c, 'SELECT body FROM finalization_receipts WHERE job=? '
                             'ORDER BY created_at DESC LIMIT 1', (job,)).fetchone()
            return json.loads(row[0]) if row else None

    def finalize_late_review(self, job):
        from swarm import settlement
        with self._task_tx(job), self._launch_tx() as c:
            proof = settlement.finalization_ready(self, self.cfg, job)
            if proof is None:
                return None
            old = self.finalization_receipt(job)
            if old and settlement.same_finalization(old, proof):
                return old
            if self._exec(c, 'SELECT 1 FROM finalization_receipts WHERE job=? AND closure_sha=? AND verdict_id=?',
                          (job, proof['closure']['sha'], proof['verdict_id'])).fetchone():
                return None
            posted = self.post(job, proof['executor'], 'FINALIZED ' + proof['candidate'])
            if not any(m.id == posted.id and m.message == 'FINALIZED ' + proof['candidate']
                       for m in self.pipeline_messages(job)):
                raise ValueError('native finalization visibility record was truncated')
            at = self.now()
            proof = dict(proof, message_id=posted.id, at=at.isoformat())
            self._exec(c, 'INSERT INTO finalization_receipts(job,closure_sha,verdict_id,body,created_at) VALUES(?,?,?,?,?)',
                       (job, proof['closure']['sha'], proof['verdict_id'], json.dumps(proof, sort_keys=True), self._launch_time(at)))
            return proof

    def completion_receipt(self, job):
        with self._event_tx(False) as c:
            row = self._exec(c, 'SELECT body FROM completion_receipts WHERE job=? '
                             'ORDER BY created_at DESC LIMIT 1', (job,)).fetchone()
            return json.loads(row[0]) if row else None

    def settle_late_review(self, job):
        from swarm import settlement
        with self._task_tx(job), self._launch_tx() as c:
            old = self.completion_receipt(job)
            if old and settlement.projected(self.job_status(job), old):
                return old
            receipt = settlement.ready(self, self.cfg, job)
            if receipt is None:
                return None
            key = receipt['closure']['sha']
            if self._exec(c, 'SELECT 1 FROM completion_receipts WHERE job=? AND closure_sha=?',
                          (job, key)).fetchone():
                return None
            event_id, _ = self.post_event(job, 'completion', key,
                'Native audited full-goal completion', to='@orchestrator', source='native-settlement')
            at = self.now()
            receipt = dict(receipt, event_id=event_id, completed_at=at.isoformat())
            self._exec(c, 'INSERT INTO completion_receipts(job,closure_sha,body,created_at,event_id) '
                       'VALUES (?,?,?,?,?)', (job, key, json.dumps(receipt, sort_keys=True),
                                            self._launch_time(at), event_id))
            return receipt
