"""Generic hand-offs and artifact-bound review records stored with a job.

Artifacts are opaque references, with a branch@sha compatibility convention.
Repository checks remain in plugin recipes.
"""
from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import re

from swarm.board.base import JOB_DATA_VALUE_MAX, parse_job_data


@dataclass(frozen=True)
class Handoff:
    artifact: str
    summary: str
    id: int
    created_at: dt.datetime
    agent_name: str


def branch_revision(artifact: str) -> tuple[str, str] | None:
    """Recognize the coding hand-off convention, including abbreviated Git SHAs."""
    match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._/-]*)@([0-9a-fA-F]{7,64})', artifact)
    if not match:
        return None
    branch, sha = match.groups()
    if ('..' in branch or '//' in branch or branch.endswith(('/', '.', '.lock'))
            or any(part.startswith('.') or part.endswith('.lock') for part in branch.split('/'))):
        return None
    return branch, sha


def ensure_started(board, job: str) -> None:
    """Adopt a pre-pipeline job from now, without replaying its historical DONE posts."""
    if not board.job_data(job).get('pipeline.started_at'):
        board.set_job_data(job, 'pipeline.started_at', board.now().isoformat())


def integrated(board, job: str, artifact: str) -> bool:
    key = 'pipeline.integrated.' + hashlib.sha256(artifact.encode()).hexdigest()[:32]
    return bool(board.job_data(job).get(key))


def latest_handoffs(board, job: str, messages=None) -> list[Handoff]:
    """Newest worker hand-off for each artifact or coding branch, in board order. DONE: claims aren't hand-offs.
    `messages`: the job's Board.pipeline_messages, when the caller has them already."""
    found = {}
    stamp = board.job_data(job).get("pipeline.started_at")
    try:
        since = dt.datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        since = None
    for message in (board.pipeline_messages(job) if messages is None else messages):
        if since and message.created_at < since:
            continue
        text = message.message
        if message.to_agent or not text.startswith('DONE '):
            continue
        first, _, summary = text.partition('\n')
        artifact = first[5:].strip()
        if artifact.startswith('"'):
            try:
                artifact, end = json.JSONDecoder().raw_decode(first[5:].strip())
                if not isinstance(artifact, str):
                    continue
                suffix = first[5:].strip()[end:].strip()
                if suffix and not suffix.startswith('|'):
                    continue
                summary = suffix[1:].strip() if suffix else summary
            except ValueError:
                continue
        # Compatibility with the original DONE branch sha convention.
        if not first[5:].strip().startswith('"'):
            parts = artifact.split()
            if len(parts) == 2 and branch_revision('@'.join(parts)):
                artifact = '@'.join(parts)
            elif len(parts) != 1:
                continue  # multi-word refs must be JSON-quoted; prose isn't an artifact
        revision = branch_revision(artifact)
        key = ('branch', revision[0]) if revision else ('artifact', artifact)
        if artifact:
            found[key] = Handoff(artifact, summary.strip(), message.id,
                                     message.created_at, message.agent_name)
    return sorted(found.values(), key=lambda item: item.id)


def drive_job(raw: str | None) -> bool:
    """Whether a job's job-data marks it a drive job (`drive.enabled`, copied from the ledger)."""
    return parse_job_data(raw).get('drive.enabled') == '1'


def verdict_data(raw: str | None, artifact: str | None, verdict: str, reason: str,
                 next_steps: str | None, judge: str, at: dt.datetime, *, scope: str = 'artifact',
                 generation: int | None = None, open_count: int | None = None, progress: bool | None = None,
                 candidate: str | None = None, req: str | None = None,
                 message_id: int | None = None) -> str:
    """New job-data JSON, written atomically with the legacy latest-verdict columns.

    Chunking preserves long --next briefs within the ordinary job-data value bound. A record written by
    deliver_verdict carries `message_id`: 0 from its first write (recorded, not yet bound), then the id of the
    VERDICT message it was posted as. bound_verdict() refuses 0, so a verdict whose binding write failed counts
    for no reader. A record written without a message id (a direct board call) has no such field: a legacy
    artifact verdict, never a drive goal or ruling verdict.
    An artifact verdict is stored under `pipeline.verdict.<hash of the artifact>`, bound to its artifact.
    A goal or ruling verdict of a drive generation is stored under drive_verdict_key(): one record per
    (generation, scope, candidate, req), so a verdict on another candidate never replaces or stands in for
    it (its artifact is None); without a generation it has no record.
    The record keeps its scope, drive generation, open count (acceptance criteria still unmet), progress
    flag, candidate and requirements digest, for the drive pipeline to read.
    """
    data = parse_job_data(raw)
    data['pipeline.verdict_artifact'] = artifact or ''
    if artifact is not None:
        key = 'pipeline.verdict.' + hashlib.sha256(artifact.encode()).hexdigest()[:32]
    elif scope in ('goal', 'ruling') and generation is not None:
        key = drive_verdict_key(generation, scope, candidate, req)
    else:
        return json.dumps(data, ensure_ascii=False)
    fields = dict(artifact=artifact, verdict=verdict, reason=reason, next_steps=next_steps, judge=judge,
                  at=at.isoformat(), scope=scope, generation=generation, open=open_count, progress=progress,
                  candidate=candidate, req=req)
    if message_id is not None:   # 0: recorded, binding pending (deliver_verdict's first write)
        fields['message_id'] = message_id
    record = json.dumps(fields, ensure_ascii=False)
    chunks = [record[i:i + JOB_DATA_VALUE_MAX] for i in range(0, len(record), JOB_DATA_VALUE_MAX)]
    # Remove obsolete tail chunks when a shorter verdict replaces a longer one.
    data = {k: v for k, v in data.items() if not k.startswith(key + '.')}
    data[key] = str(len(chunks))
    data.update({f'{key}.{i}': chunk for i, chunk in enumerate(chunks)})
    return json.dumps(data, ensure_ascii=False)


def drive_verdict_key(generation: int, scope: str, candidate: str | None, req: str | None) -> str:
    """The job-data key of a goal or ruling verdict: per generation, scope and (candidate, req) digest."""
    digest = hashlib.sha256(f"{candidate or ''}\0{req or ''}".encode()).hexdigest()[:32]
    return f'drive.verdict.{generation}.{scope}.{digest}'


def bound_verdict(record, *, legacy_ok: bool = False) -> dict | None:
    """The verdict record if it is bound to its posted VERDICT message (a positive int `message_id`), else
    None. A record written before the binding (message_id 0: the second write of deliver_verdict failed or
    has not happened yet) is never accepted. `legacy_ok` accepts a record with no message_id field at all
    (written before verdict binding existed; non-drive artifact verdicts only). Every reader of verdict
    records goes through this (Tasks 7/8 included)."""
    if not isinstance(record, dict):
        return None
    if 'message_id' not in record:
        return record if legacy_ok else None
    mid = record['message_id']
    return record if type(mid) is int and mid > 0 else None


def _chunked(data: dict, key: str):
    count = int(data[key])
    if not 0 < count <= 10000:
        raise ValueError('chunk count')
    return json.loads(''.join(data[f'{key}.{i}'] for i in range(count)))


def drive_verdict(board, job: str, generation: int, scope: str, candidate: str | None,
                  req: str | None) -> dict | None:
    """The bound goal or ruling verdict on exactly this (generation, scope, candidate, req), or None. The
    consumer still checks the message id against the board and the verdict's receipt (spec 4.0)."""
    data = board.job_data(job)
    try:
        rec = _chunked(data, drive_verdict_key(generation, scope, candidate, req))
    except (KeyError, ValueError, TypeError):
        return None
    rec = bound_verdict(rec)
    if rec is None or (rec.get('generation'), rec.get('scope'), rec.get('candidate'), rec.get('req')) != (
            generation, scope, candidate, req):
        return None
    return rec


def verdict_text(verdict: str, reason: str, *, scope: str = 'artifact', generation: int | None = None,
                 candidate: str | None = None, req: str | None = None) -> str:
    """The VERDICT message: a goal or ruling verdict of a drive generation names what it judges, so a reader
    holding the message can tell (before the reason: the board caps a message's length)."""
    if scope in ('goal', 'ruling') and generation is not None:
        return (f"VERDICT {verdict} [{scope} gen={generation} candidate={candidate or '-'} req={req or '-'}]: "
                f"{reason}")
    return f"VERDICT {verdict}: {reason}"


def is_drive_job(board, job: str, data: dict | None = None) -> bool:
    """Whether `job` is driven: its board row claims it (drive.enabled), or this host holds a drive ledger
    for it (a bad one included). Either is enough: a writer that clears the board claim does not turn a drive
    job's verdict rules back into the legacy ones."""
    data = board.job_data(job) if data is None else data
    if data.get('drive.enabled') == '1':
        return True
    try:
        from swarm.board.autoinit import store_key
        from swarm.supervisor import ledger
        cfg = getattr(board, 'cfg', None)
        if cfg is None:
            return False
        try:
            return ledger.load(store_key(cfg), job) is not None
        except ledger.LedgerBadError:
            return True
    except Exception:
        return False


def artifact_verdicts(board, job: str, *, drive: bool | None = None) -> dict[str, dict]:
    """The artifact verdicts of `job` by artifact. Every record goes through bound_verdict(); a record with
    no message id at all (written before verdict binding, or by a direct board call) counts only in a job
    that is not driven (`drive`: is_drive_job() when not given)."""
    data = board.job_data(job)
    legacy_ok = not (is_drive_job(board, job, data) if drive is None else drive)
    result = {}
    for key, count in data.items():
        if not re.fullmatch(r'pipeline\.verdict\.[0-9a-f]{32}', key):
            continue
        try:
            count = int(count)
            if not 0 < count <= 10000:
                continue
            record = bound_verdict(json.loads(''.join(data[f'{key}.{i}'] for i in range(count))), legacy_ok=legacy_ok)
            if record is not None and record['verdict'] in ('met', 'not_met'):
                result[record['artifact']] = record
        except (KeyError, ValueError, TypeError):
            continue
    return result


def covered(handoff: Handoff, verdict: dict | None) -> bool:
    if not verdict:
        return False
    try:
        return verdict['artifact'] == handoff.artifact and dt.datetime.fromisoformat(verdict['at']) >= handoff.created_at
    except (KeyError, TypeError, ValueError):
        return False


def pipeline_status(raw: str | None) -> dict:
    data = parse_job_data(raw)
    return dict(evidence_command=data.get('pipeline.evidence_command'),
                finalize=data.get('pipeline.finalize'),
                verdict_artifact=data.get('pipeline.verdict_artifact') or None)


def judge_artifact(board, job: str, agent_key: str | None = None, *, name: str | None = None) -> str | None:
    """The supervisor's hand-off binding follows resume_of when a session takes its seat."""
    data = board.job_data(job)
    agent = next((a for a in board.agents(job) if (agent_key and a.agent_key == agent_key)
                  or (name and a.name == name and a.role == 'judge' and a.status not in ('left', 'completed'))), None)
    if agent:
        for key in (agent.agent_key, agent.resume_of):
            if key:
                digest = hashlib.sha256(key.encode()).hexdigest()[:32]
                ref = data.get('pipeline.judge.' + digest)
                if ref:
                    return ref
    return None


def pending_artifacts(board, job: str, handoffs=None) -> list[str]:
    """Unaccepted current hand-offs; recipes may persist explicit revision supersessions."""
    data = board.job_data(job)
    verdicts = artifact_verdicts(board, job)
    pending = []
    for h in (latest_handoffs(board, job) if handoffs is None else handoffs):
        key = 'pipeline.superseded.' + hashlib.sha256(h.artifact.encode()).hexdigest()[:32]
        if data.get(key) or integrated(board, job, h.artifact):
            continue
        verdict = verdicts.get(h.artifact)
        if not covered(h, verdict) or verdict['verdict'] != 'met':
            pending.append(h.artifact)
    return pending


def auto_close_pending(board, job: str) -> bool:
    """A pipeline hand-off stays open until a separate executor records finalization."""
    messages = board.pipeline_messages(job)   # read once: a long job has thousands of messages
    handoffs = latest_handoffs(board, job, messages)
    if pending_artifacts(board, job, handoffs):
        return True
    data = board.job_data(job)
    verdicts = artifact_verdicts(board, job)
    for h in handoffs:
        superseded = 'pipeline.superseded.' + hashlib.sha256(h.artifact.encode()).hexdigest()[:32]
        if data.get(superseded) or integrated(board, job, h.artifact):
            continue
        since = dt.datetime.fromisoformat(verdicts[h.artifact]['at'])
        if not any(m.created_at >= since and m.message.startswith(('FINALIZED ', 'INTEGRATED '))
                   and m.message.partition(' ')[2] == h.artifact for m in messages):
            return True
    return False


def clear_verdict_data(raw: str | None) -> str:
    """A different goal starts judging afresh without losing evidence/finalize settings."""
    return json.dumps({k: v for k, v in parse_job_data(raw).items()
                       if not k.startswith(('pipeline.verdict.', 'pipeline.judge.', 'pipeline.superseded.'))
                       and k != 'pipeline.verdict_artifact'}, ensure_ascii=False)


def completion_pending(board, job: str, goal: str | None, verdict: str | None) -> bool:
    """Current artifacts govern acceptance; legacy verdicts govern jobs without hand-offs."""
    if not goal:
        return False
    if latest_handoffs(board, job):
        return auto_close_pending(board, job)
    return verdict != 'met'
