"""The CLI of the memory life cycle (swarm.learnings): `swarm learn`, `swarm remember --none`, `swarm job tags`,
the learnings gate of `swarm deactivate` and the memory scope recorded at activation. Split out of cli.py
(which keeps thin wrappers) to keep that file under the directory edition's per-file size limit."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from swarm import compat
from swarm.cli import _error_name, term_safe


def remember_none(cfg: dict, args) -> int:
    """`swarm remember --none "<reason>"`: the agent declares it has nothing durable to store from this
    run. Recorded on its agent row (what the stop gate reads) and posted on the board, so the others see
    it, and on the job (memory.declared_at). It stores nothing."""
    from swarm import learnings
    from swarm.board import open_board
    reason = " ".join(str(args.none).split())
    if args.fact:
        print("swarm remember: --none takes no fact (store facts without it)", file=sys.stderr)
        return 2
    if len(reason) < 8:
        print("swarm remember --none: say why there is nothing worth keeping (8 characters or more)",
              file=sys.stderr)
        return 2
    try:
        with open_board(cfg) as board:
            board.record_remembered(args.name)
            learnings.note_declared(board, args.job)
            learnings.clear_own_marker(board, args.job, args.name)
            try:
                board.post(args.job, args.name, ("no durable learnings: " + reason)[: board.message_cap()])
            except Exception:
                pass
    except Exception as exc:
        print(f"swarm remember --none: board not reachable ({_error_name(exc)}); retry", file=sys.stderr)
        return 1
    print(term_safe(f"recorded: {args.name} has no durable learnings to store ({reason})"))
    return 0



HARVEST_EXCERPT_CHARS = 4000   # of one unharvested agent's transcript, shown by `swarm learn --harvest`


def _assistant_text(body: bytes, limit: int) -> str:
    """The last `limit` characters of what the agent said (assistant text turns) in a JSONL transcript."""
    said = []
    for line in body.decode("utf-8", "replace").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") if isinstance(row, dict) else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        parts = [content] if isinstance(content, str) else [
            c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
        said += [" ".join(p.split()) for p in parts if p and p.strip()]
    text = "\n".join(said)
    return text[-limit:]


def _learn_harvest(board, js, job: str) -> int:
    from swarm import learnings
    learnings.sweep(board, job, active_too=True)
    pending = learnings.unharvested(board, job)
    if not pending:
        print(f"no unharvested agents on {job}")
        return 0
    print(f"{len(pending)} agent(s) of {job} ended without storing their learnings. Read what each did, distill "
          f"the durable findings (root causes, settings and why, traps, measured numbers) into one self-contained "
          f"fact per line, and retain them: `swarm learn --job {job} --bank BANK -` (stdin) or `--from FILE`; if "
          f"none of them learned anything durable: `swarm learn --job {job} --none \"<why>\"`.")
    for e in pending:
        key, name = e.get("key"), e.get("name")
        print(f"\n=== {term_safe(name)} (ended: {term_safe(e.get('why'))}; key {term_safe(key)})")
        body = None
        try:
            body = board.transcript_body(job, key) if key else None
        except Exception as exc:
            print(f"(transcript unreadable: {_error_name(exc)})")
        text = _assistant_text(body, HARVEST_EXCERPT_CHARS) if body else ""
        if text:
            print(term_safe(text))
        else:
            print(f"(no archived transcript text; read its board posts: `swarm read --job {job} --peek`)")
        if key:
            print(f"full transcript: swarm transcript show --job {job} --key {term_safe(key)}")
    return 0


def cmd_learn(cfg: dict, args) -> int:
    """The job's learnings step. Retain one self-contained fact per line (stdin `-` or --from FILE) with
    durable provenance, or declare with --none that the unharvested agents learned nothing durable; either
    clears the job's unharvested markers it covered (swarm.learnings.harvest) and is recorded. --harvest
    prints those agents with their transcripts first. Unlike remember, this never queues or truncates:
    success certifies a completed retain, which is required before a disposable project bank may be deleted.
    """
    from swarm import hindsight, provenance, hosts, learnings
    from swarm.board import open_board
    from swarm.board.base import MemoryRef, valid_name
    if not hindsight.enabled(cfg):
        print("memory is off: set [hindsight] url in the swarm config", file=sys.stderr)
        return 1
    client = hindsight.Client(cfg)
    if args.list_banks:
        # --job is accepted (a drive launch's broker always adds its own job), and ignored
        if args.bank or args.facts or args.create_bank or args.harvest or args.none or args.from_file:
            print("swarm learn --list-banks cannot be combined with retain options", file=sys.stderr)
            return 2
        try:
            print("\n".join(client.list_banks()))
        except (hindsight.HindsightError, hindsight.HindsightUnavailable) as exc:
            print(f"cannot list banks: {term_safe(exc)}", file=sys.stderr)
            return 1
        return 0
    usage = "usage: swarm learn --job JOB [--harvest] | [--bank BANK] [--create-bank] (- | --from FILE) | --none REASON"
    sources = sum(1 for x in (args.facts == "-", bool(args.from_file), args.none is not None) if x)
    if not args.job or (args.facts not in (None, "-")) or (sources != 1 and not args.harvest) or \
            (args.harvest and sources):
        print(usage, file=sys.stderr)
        return 2
    if args.harvest:
        with open_board(cfg) as board:
            js = board.job_status(args.job)
            if js is None:
                print(f"no such job: {term_safe(args.job)}", file=sys.stderr)
                return 1
            return _learn_harvest(board, js, args.job)
    if args.none is not None:
        reason = " ".join(args.none.split())
        if len(reason) < 8:
            print("swarm learn --none: say why nothing is worth keeping (8 characters or more)", file=sys.stderr)
            return 2
        with open_board(cfg) as board:
            if board.job_status(args.job) is None:
                print(f"no such job: {term_safe(args.job)}", file=sys.stderr)
                return 1
            learnings.sweep(board, args.job, active_too=True)
            pending = learnings.unharvested(board, args.job)
            learnings.harvest(board, args.job, _learn_by(), 0, reason, pending)
        print(term_safe(f"learnings step recorded for {args.job}: nothing durable ({reason}); "
                        f"{len(pending)} unharvested agent(s) covered"))
        return 0
    try:
        extra = learnings.parse_tags(",".join(args.tags))
    except ValueError as exc:
        print(term_safe(f"swarm learn: {exc}"), file=sys.stderr)
        return 2
    bank = args.bank or cfg["hindsight"].get("default_bank", "coding")
    if not valid_name(bank):
        print("invalid bank: use 1-64 letters, digits, spaces and . ' _ -", file=sys.stderr)
        return 2
    if args.from_file:
        try:
            raw = Path(args.from_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"cannot read {term_safe(args.from_file)}: {_error_name(exc)}", file=sys.stderr)
            return 1
    else:
        raw = sys.stdin.read()
    facts = [line.strip() for line in raw.splitlines() if line.strip()]
    cap = int(cfg["hindsight"]["remember_max_chars"])
    if not facts:
        print("no learnings supplied", file=sys.stderr)
        return 1
    if any(len(fact) > cap for fact in facts):
        print(f"learning exceeds {cap} characters; split it into self-contained facts", file=sys.stderr)
        return 1
    # The hook's short timeout is unsuitable for synchronous fact extraction. This
    # explicit CLI step can wait; recall and normal remember keep their own budgets.
    learn_cfg = {**cfg, "hindsight": {**cfg["hindsight"], "timeout_seconds":
                                    max(120.0, float(cfg["hindsight"]["timeout_seconds"]))}}
    try:
        with open_board(cfg) as board:
            js = board.job_status(args.job)
            if js is None:
                print(f"no such job: {term_safe(args.job)}", file=sys.stderr)
                return 1
            learnings.sweep(board, args.job, active_too=True)
            pending = learnings.unharvested(board, args.job)   # what this step covers
            meta = {**provenance.cli_metadata(os.environ), "learning": "true"}
            name = "orchestrator"
            retained = []
            for fact in facts:
                doc = provenance.new_document_id()
                hindsight.remember(board, learn_cfg, args.job, name, fact, bank,
                                   document_id=doc, metadata=meta, create_bank=args.create_bank,
                                   synchronous=True, extra_tags=extra)
                retained.append(doc)
            # A partial batch must not authorise deletion: publish the durable evidence
            # only after every fact in this invocation has been retained successfully.
            for doc in retained:
                board.save_memory_ref(MemoryRef(
                    document_id=doc, bank=hindsight.bank_id(bank), job=args.job,
                    agent_key=f"learn:{js.session_id or args.job}", agent_name=name,
                    harness=hosts.detect_cli_host(os.environ), host=compat.node(),
                    session_id=hosts.cli_session_id(os.environ) or js.session_id,
                    tool_call_id=None, writer="swarm-learn"))
                print(term_safe(f'learned in bank "{bank}" ' + provenance.output_tag(doc, bank)))
            learnings.harvest(board, args.job, _learn_by(), len(retained), None, pending)
    except (hindsight.HindsightError, hindsight.HindsightUnavailable) as exc:
        print(f"learning not retained: {term_safe(exc)}; retry before deleting the project bank",
              file=sys.stderr)
        return 1
    return 0


def _learn_by() -> str:
    from swarm import hosts
    return f"{hosts.detect_cli_host(os.environ) or 'cli'}:{os.environ.get('USER') or 'person'}"[:60]



def job_tags(board, cfg: dict, args) -> int:
    """`swarm job tags --job J [--add S | --remove S | --set S]`: the job's memory subject tags (no flag: list).
    Any job state; every change posts a board notice from "swarm". `swarm remember` reads them at each call."""
    from swarm import learnings
    if not args.tags_job or args.rest or args.into or args.goal is not None or args.description:
        print("usage: swarm job tags --job J [--add a,b | --remove a | --set a,b]", file=sys.stderr)
        return 2
    job = args.tags_job
    try:
        change = next(((k, learnings.parse_tags(getattr(args, k))) for k in ("set", "add", "remove")
                       if getattr(args, k) is not None), None)
    except ValueError as exc:
        print(term_safe(f"swarm job tags: {exc}"), file=sys.stderr)
        return 2
    if board.job_status(job) is None:
        print(f"no such job: {term_safe(job)}", file=sys.stderr)
        return 1
    data = board.job_data(job)
    explicit = learnings.TAGS_KEY in data
    # a job whose tags were never set works from its effective ones (its project's name): a change starts there
    stored = [t for t in (data.get(learnings.TAGS_KEY) or "").split(",") if t] if explicit \
        else learnings.job_tags(board, job, data)
    if change is not None:
        kind, given = change
        if kind == "set":
            tags = given
        elif kind == "add":
            tags = stored + given
        else:
            tags = [t for t in stored if t not in given]
        tags = list(dict.fromkeys(tags))
        if len(tags) > learnings.MAX_TAGS:
            print(f"swarm job tags: at most {learnings.MAX_TAGS} subject tags a job (it would have {len(tags)})",
                  file=sys.stderr)
            return 2
        if tags != stored or not explicit:     # a change always leaves an explicit set ("" = emptied on purpose)
            board.set_job_data(job, learnings.TAGS_KEY, ",".join(tags))
            who = os.environ.get("USER") or "person"
            board.post(job, "swarm", term_safe(f"subject tags of {job} changed by {who}: "
                       f"{', '.join(stored) or '(none)'} -> {', '.join(tags) or '(none)'}")[:900])
            stored, explicit = tags, True
            print(f"{job}: tags changed")
    print(f"{job}: " + (", ".join(stored) if stored else "(no subject tags: its memories carry none)")
          + ("" if explicit or not stored else "  [from its project]"))
    return 0



def learnings_gate(cfg: dict, board, js, args) -> int:
    """The mandatory learnings step of a close (swarm.learnings.close_blockers): refused (1) while agents are
    unharvested or nothing was stored for this run, unless a PERSON passes --no-learn "<reason>" (the same
    person gate as the other person-only actions: the requester's grant, or a confirmed tty outside any
    agent host). The waiver is recorded on the job. 0: go ahead."""
    from swarm import learnings
    blockers = learnings.close_blockers(board, cfg, js)
    if not args.no_learn:
        if blockers:
            print(f"not closing {js.job}: the learnings step is not done:\n- " + "\n- ".join(blockers)
                  + f"\nRun one of:\n  swarm learn --job {js.job} --bank BANK -\n"
                  f"  swarm learn --job {js.job} --none \"<reason>\"\n"
                  f"  swarm deactivate --job {js.job} --status {args.status} --no-learn \"<reason>\" "
                  f"(a person; --force does not skip this step)", file=sys.stderr)
            return 1
        return 0
    reason = " ".join(args.no_learn.split())
    if len(reason) < 8:
        print("swarm deactivate --no-learn: give the reason (8 characters or more)", file=sys.stderr)
        return 2
    how = getattr(args, "_person_how", None)
    if not how:
        from swarm import person
        from swarm.drive_cli import board_key
        try:
            person.forget()
            how = person.require("deactivate --no-learn", js.job, board_key=board_key(cfg), who=None)
        except person.PersonRefused as exc:
            print(term_safe(str(exc)), file=sys.stderr)
            return 1
    board.set_job_data(js.job, learnings.NO_LEARN_KEY, json.dumps(
        {"by": how, "reason": reason[:300], "at": board.now().isoformat(), "blockers": len(blockers)}))
    return 0



def record_memory_scope(cfg: dict, args, explicit: list, cwd: str) -> int:
    """The job's memory subjects and topic (swarm.learnings), right after a drive activation, read back to
    check them. 0 when saved; 1 (the activation fails, with what to run) when they could not be: a job
    never runs without the scope it asked for."""
    from swarm import learnings
    from swarm.board import open_board
    tags = learnings.derive_tags(args.project, cwd, explicit)
    topic = getattr(args, "topic", None)
    try:
        with open_board(cfg) as board:
            learnings.record_scope(board, args.job, tags, topic)
            data = board.job_data(args.job)
            if tags and data.get(learnings.TAGS_KEY) != ",".join(tags):
                raise ValueError("the saved subject tags do not read back")
            if topic and topic.strip() and data.get(learnings.TOPIC_KEY) != " ".join(topic.split())[:1900]:
                raise ValueError("the saved topic does not read back")
    except Exception as exc:
        fix = (f"swarm job tags --job {args.job} --set {','.join(tags)}" if tags else "")
        print(term_safe(f"swarm activate: {args.job} is activated, but its memory subjects/topic were NOT saved "
                        f"({_error_name(exc)}: {exc}). The activation failed: "
                        + (f"set them with `{fix}` before any agent works on it, or " if fix else "")
                        + f"deactivate it and activate again"), file=sys.stderr)
        return 1
    return 0
