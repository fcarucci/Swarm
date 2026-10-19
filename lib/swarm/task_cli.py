"""Native task registration and assigned exact-artifact reports (normal trusted enrolment)."""
import json
import os
import sys
from swarm import cli

term_safe = cli.term_safe


def configure(sub):
    tk = sub.add_parser("task", help="persist exact-artifact engineering dependencies before ending a turn")
    tks = tk.add_subparsers(dest="task_cmd", required=True)
    for action in ("add", "report", "list"):
        tp = tks.add_parser(action)
        tp.add_argument("--job", required=True)
        if action == "list":
            tp.add_argument("--json", action="store_true")
            continue
        tp.add_argument("--name", required=True, help="stable task name")
        tp.add_argument("--generation", type=int, default=1)
        tp.add_argument("--artifact", required=True)
        tp.add_argument("--as", dest="actor")
        if action == "report":
            tp.add_argument("--outcome", choices=("passed", "failed"), required=True)
            tp.add_argument("--reason", required=True)
        else:
            tp.add_argument("--depends", action="append", default=[])
            tp.add_argument("--role", required=True)
            tp.add_argument("--host", "--harness", dest="harness", choices=("claude", "codex"), required=True)
            tp.add_argument("--model")
            tp.add_argument("--workdir", required=True)
            tp.add_argument("--brief", required=True)
            tp.add_argument("--reporter", help="existing active external agent; task is never launched")


def run(board, cfg: dict, args) -> int:
    from swarm import dependencies, enrolment, hosts
    from swarm.board.autoinit import store_key
    if args.task_cmd == "list":
        rows = dependencies.describe(board, args.job)
        if args.json:
            print(json.dumps(rows))
        else:
            for row in rows:
                print(term_safe(f"{row['name']} generation {row['generation']}: {row['state']} "
                                f"{row['artifact']} {row['reason']}"))
        return 0
    bk = store_key(cfg)
    # Codex children inherit the coordinator's CODEX_SESSION_ID; only the current
    # thread identifies the worker. A shared root route never authorizes a sibling.
    codex = hosts.detect_cli_host(os.environ) == 'codex'
    sid = (os.environ.get('CODEX_THREAD_ID') or hosts.cli_session_id(os.environ)) if codex else hosts.cli_session_id(os.environ)
    def is_caller(a):
        return bool(sid and (a.agent_key == sid or (
            not codex and board.route(a.agent_key).session_id == sid)))
    members = board.agents(args.job, include_departed=False)
    session_agents = [a for a in members if is_caller(a)]
    actor = args.actor or (session_agents[0].name if len(session_agents) == 1 else cli._blocker_actor(board, cfg))
    agent = next((a for a in members if a.name == actor), None)
    # Ordinary job enrolment grants task registration, never drive-owner authority. Evidence
    # additionally belongs to the concrete assigned agent's current enrolled session.
    local = enrolment.find_job(bk, args.job) or enrolment.find_job_owner(bk, args.job)
    if local is None and not any(enrolment.owns(bk, a.agent_key, args.job) for a in members):
        print("swarm task: this host has no trusted enrolment for the job", file=sys.stderr)
        return 1
    try:
        if args.task_cmd == "add":
            from swarm.supervisor.command import workdir_problem
            why = workdir_problem(cfg, args.workdir)
            if why:
                raise ValueError(why)
            row = dependencies.register(board, args.job, args.generation, args.name,
                artifact=args.artifact, depends=args.depends, role=args.role, harness=args.harness,
                model=args.model, workdir=args.workdir, brief=args.brief, queued_by=actor, reporter=args.reporter, cfg=cfg)
            print(term_safe(f"{row.name}: {row.state}; dependencies persisted"))
        else:
            if (agent is None or not sid or not enrolment.owns(bk, agent.agent_key, args.job)
                    or not is_caller(agent)):
                raise ValueError("task evidence requires the assigned enrolled agent session")
            _, created = dependencies.report(board, args.job, args.generation, args.name,
                artifact=args.artifact, outcome=args.outcome, reason=args.reason, actor=actor)
            print("task evidence recorded" if created else "task evidence already recorded; first report preserved")
        why = dependencies.wake(cfg)
        if why:
            board.set_job_data(args.job, "task.reconciler_error", why)
            print("swarm task: reconciliation startup blocked: " + term_safe(why), file=sys.stderr)
        else:
            board.set_job_data(args.job, "task.reconciler_error", None)
    except ValueError as exc:
        print("swarm task: " + term_safe(str(exc)), file=sys.stderr)
        return 1
    return 0
