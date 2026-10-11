"""`swarm land`: the integrator's hand-off to the driver, and `swarm land hosts add` (a person confirms an ssh
host key). Kept out of cli.py (256 KiB cap). See swarm.land for what the driver then does."""
from __future__ import annotations

import sys

from swarm import land


def add_parser(sub) -> None:
    lp = sub.add_parser("land", help="integrator: ask the driver to land the judged artifact (BRANCH@SHA) on the "
                                     "target; `land hosts add URL`: a person trusts an ssh host key")
    lp.add_argument("--job")
    lp.add_argument("--artifact", help="BRANCH@<full sha>: the exact SHA that is judged met, rebased and CI green")
    lp.add_argument("--as", dest="name", help="the integrator's name")
    lp.add_argument("--key", help="the agent's key (instead of --as)")
    ls = lp.add_subparsers(dest="land_cmd", metavar="hosts")
    hs = ls.add_parser("hosts", help="ssh host keys the driver may trust")
    hsub = hs.add_subparsers(dest="hosts_cmd", metavar="add", required=True)
    ha = hsub.add_parser("add", help="show the host's key fingerprints and trust them after you confirm "
                                     "(a person at a terminal; never automatic)")
    ha.add_argument("url", help="an ssh remote URL, e.g. git@host:org/repo.git")


def _cli():
    from swarm import cli
    return cli


def _hosts_add(args) -> int:
    from swarm.supervisor import mirror
    safe = _cli().term_safe
    try:
        hp = mirror.ssh_host_of(args.url)
        if hp is None:
            print("not an ssh remote: only ssh destinations need a host key", file=sys.stderr)
            return 1
        host, port = hp
        path = mirror.known_hosts_file()
        if mirror.known_host_present(path, host, port):
            print(f"{host}:{port} is already trusted")
            return 0
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("refused: host keys are confirmed by a person at a terminal, never by a script or an agent",
                  file=sys.stderr)
            return 1
        keys = mirror.scan_host_keys(host, port)
        print(f"{host}:{port} offers these host keys (compare them with the host's published fingerprints):")
        for _, fp in keys:
            print(f"  {safe(fp)}")
        if input(f"Type the host name ({host}) to trust them: ").strip() != host:
            print("not trusted: the host name did not match", file=sys.stderr)
            return 1
        n = mirror.add_known_host(path, host, port, [line for line, _ in keys], confirmed=True)
    except mirror.MirrorError as exc:
        print(f"host key not added: {safe(exc)}", file=sys.stderr)
        return 1
    print(f"trusted {n} host key(s) for {host}:{port}")
    return 0


def _board_land(board, cfg: dict, args) -> int:
    if getattr(args, "land_cmd", None) == "hosts":
        return _hosts_add(args)
    safe = _cli().term_safe
    if not args.job or not args.artifact:
        print("swarm land: give --job J and --artifact BRANCH@SHA", file=sys.stderr)
        return 2
    try:
        land.parse_artifact(args.artifact)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if board.job_data(args.job).get("drive.enabled") != "1":
        print(f"refused: {safe(args.job)} is not a drive job; there the integrator lands as the team recipe says",
              file=sys.stderr)
        return 1
    if not args.name and args.key:
        args.name = board.active_agent_name(args.key)
    if not args.name:
        print("swarm land: give --as NAME (or a --key that belongs to an agent)", file=sys.stderr)
        return 2
    try:
        from swarm import addressing
        addressing.check_author(board, args.job, args.name, args.key)
        mid = land.request_land(board, args.job, args.artifact, name=args.name, agent_key=args.key)
    except ValueError as exc:
        print(f"not requested: {safe(exc)}", file=sys.stderr)
        return 1
    from swarm.fastpath import changed
    changed(args.job)
    print(f"landing requested for {safe(args.artifact)}. You are done: the driver lands it and posts INTEGRATED "
          f"or LAND-REFUSED <why>.")
    print(f"swarm-record: kind=post id={mid} rev=1")
    return 0
