"""`swarm verdict`: the judge's record (met, not_met, unreachable) and `swarm verdict show`.
Kept out of cli.py, which must stay under the Codex directory edition's 256 KiB per-file limit."""
from __future__ import annotations

import sys


def _cli():
    from swarm import cli
    return cli


def _verdict_text(args) -> tuple[str, str | None] | None:
    """(reason, next steps) of a `swarm verdict`, or None (after saying why on stderr) when a
    not_met verdict lacks its --reason or --next: the judge must say why and what would meet the goal.
    Also reads --details once, into args.details_text."""
    if getattr(args, "details", None) and not hasattr(args, "details_text"):
        try:
            args.details_text = _cli()._read_details(args.details)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return None
    reason = (getattr(args, "reason_opt", None) or " ".join(args.reason or ())).strip()
    nxt = (getattr(args, "next_steps", None) or "").strip()
    if args.verdict == "met":
        if not reason:
            print("a verdict needs a reason: swarm verdict --job J met \"<why>\"", file=sys.stderr)
            return None
        return reason, None
    if args.verdict == "unreachable":
        if not reason or not getattr(args, "details_text", None):
            print("refused: an unreachable verdict must say why the goal cannot be met (--reason) and give its "
                  "report (--details -): what was tried, why it cannot be met, and what would change that.",
                  file=sys.stderr)
            return None
        return reason, None
    missing = [flag for flag, v in (("--reason \"<why it is not met>\"", reason),
                                    ("--next \"<what to change, where, and what you will re-check>\"", nxt)) if not v]
    if missing:
        print("refused: a not_met verdict must say why and what to do to meet the goal; missing "
              + " and ".join(missing) + ". The workers are spawned with these instructions.", file=sys.stderr)
        return None
    return reason, nxt


def _verdict_details_line(board, job: str) -> str | None:
    """The status line announcing the job's full verdict report, or None without one."""
    d = board.verdict_details(job)
    if d is None:
        return None
    n = len(d.details.strip().splitlines())
    return f"details    {n} line{'s' if n != 1 else ''}, `swarm verdict show --job {job}`"


def _verdict_show(board, args) -> int:
    d = board.verdict_details(args.job, getattr(args, "artifact", None))
    if d is None:
        which = f" bound to {_cli().term_safe(args.artifact)}" if getattr(args, "artifact", None) else ""
        print(f"no verdict details{which} for job {_cli().term_safe(args.job)} (a judge adds them with "
              f"`swarm verdict ... --details -`)", file=sys.stderr)
        return 1
    head = (f"verdict {d.verdict} by {_cli().term_safe(d.by)}" + (f", {_cli()._ago(d.at, board.now())}" if d.at else "")
            + (f", artifact {_cli().term_safe(d.artifact)}" if d.artifact else ""))
    print(head + "\n")
    print(_cli().term_safe(d.details.rstrip(), keep_newlines=True))
    return 0


def _verdict_scope(args) -> str:
    """The scope a `swarm verdict` judges: --scope, else goal for unreachable (it ends a goal) and
    artifact for met and not_met."""
    return getattr(args, "scope", None) or ("goal" if args.verdict == "unreachable" else "artifact")


def _board_verdict(board, cfg: dict, args) -> int:
    from swarm.spool import deliver_verdict
    if args.verdict == "show":
        return _verdict_show(board, args)
    text = _verdict_text(args)
    if text is None:
        return 1
    scope = _verdict_scope(args)
    generation = None   # no drive generations: the verdict carries no generation
    from swarm.spool import VerdictUnbound
    try:
        message_id = deliver_verdict(board, args.job, args.name, args.verdict, text[0], text[1],
                                     getattr(args, "artifact", None), getattr(args, "details_text", None),
                                     scope=scope, generation=generation,
                                     open_count=getattr(args, "open_count", None),
                                     progress=True if getattr(args, "progress", None) else None,
                                     candidate=getattr(args, "candidate", None), req=getattr(args, "req", None))
    except VerdictUnbound as exc:
        print(f"refused: verdict posted but unbound — rejected: {_cli().term_safe(args.name)} stopped being the "
              f"judge of {_cli().term_safe(args.job)} while it was recorded (message {exc.message_id} is void and "
              "counts for nothing); if you are the judge, run it again", file=sys.stderr)
        return 1
    except ValueError as exc:   # a name the board refuses (board.base.valid_name)
        print(f"verdict not recorded: {_cli().term_safe(exc)}", file=sys.stderr)
        return 1
    if not message_id:
        print(f"refused: {_cli().term_safe(args.name)} is not the judge of job {_cli().term_safe(args.job)}", file=sys.stderr)
        return 1
    from swarm.fastpath import changed
    changed(args.job)
    print(f"verdict {args.verdict} recorded for {args.job}, and posted on the board")
    print(f"swarm-record: kind=verdict id={message_id} rev=1")
    return 0


