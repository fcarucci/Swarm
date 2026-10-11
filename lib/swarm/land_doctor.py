"""Doctor lines for landing (spec 3.8 [G2] [G2a] [F24]): what bootstrap.drive_checks adds per pinned
destination, plus the text that explains a flagged audit. Pure functions over the owner record's pinned
remotes and credentials; nothing here writes. Git's version and the helper path are checked by
bootstrap.drive_checks (Task 1b); this module adds the dry-run `git ls-remote` per destination with the
pinned environment, the ssh agent socket and key, and the private known_hosts entry."""
from __future__ import annotations

import os
import stat
from collections import namedtuple

from swarm import land
from swarm.supervisor import mirror as mirror_mod
from swarm.supervisor.mirror import Mirror, MirrorError

# name, ok (True | False | None for a warning), detail, fix: the fields of bootstrap.Check
Line = namedtuple("Line", "name ok detail fix")
LS_REMOTE_SECONDS = 20.0

AUDIT_HELP = (
    "DRIVER-AUDIT means the target branch holds a commit since activation that no receipted met verdict of "
    "this job covers: it is not reachable from a judged SHA and has no matching patch-id in a judged chain. "
    "Landings made by `swarm land` push exactly the judged SHA, so they never cause it. Typical causes: a person "
    "or another job pushed to the target during the run; a judged branch that forked before activation; a merge "
    "or empty commit inside a judged chain (it has no patch-id to match); two judged artifacts whose commits "
    "twin each other; a reordered or partially landed chain. The job does not close MET: a ruling becomes due "
    "with the finding in its prompt. To resolve: read `git log <activation head>..<target>` for the listed "
    "commits; if they are wanted, put them on a branch, hand it off and have the judge record a met verdict for "
    "its exact SHA (the audit then accepts them); if not, revert them through a branch, judge and `swarm land` "
    "it. A MissingCommit finding means a judged SHA is not in the private mirror (its branch was deleted): "
    "push it to a branch again. A rewound target means the target was reset behind its activation head: "
    "restore it before the job can close.")


AUDIT_SUMMARY = ("DRIVER-AUDIT = an unjudged commit is on the target; the job holds for a ruling, not a MET close. "
                 "Revert it, or hand it off and judge it (docs/REFERENCE.md, Drive mode, flagged audit)")


def _clean(text: str, url: str) -> str:
    return " ".join(str(text).replace(url, "<remote>").split())[:200]


def check_destination(name: str, url: str, credential: dict, *, mirror_factory=Mirror,
                      timeout: float = LS_REMOTE_SECONDS) -> list:
    """The doctor lines of one pinned destination: the ssh agent socket or key, the private known_hosts
    entry of an ssh host, and a dry-run `git ls-remote` with exactly the environment fetch and push use."""
    lines = []
    kind = (credential or {}).get("kind")
    label = f"land {name}"
    if kind == "ssh-agent":
        sock = credential.get("socket", "")
        try:
            ok = stat.S_ISSOCK(os.stat(sock).st_mode)
        except OSError:
            ok = False
        lines.append(Line(label + " agent", ok, f"ssh-agent socket {sock} " + ("is there" if ok else "is missing"),
                          None if ok else "an agent socket does not survive a reboot: start the agent and run "
                                          "`swarm activate` again to pin the new socket"))
    elif kind == "ssh-key":
        path = credential.get("path", "")
        ok = os.path.isfile(path) and os.access(path, os.R_OK)
        lines.append(Line(label + " key", ok, f"ssh key {path} " + ("is readable" if ok else "is missing or unreadable"),
                          None if ok else "pin a readable key with --credential"))
    host = mirror_mod.ssh_host_of(url)
    if host is not None:
        try:
            known = mirror_mod.known_host_present(mirror_mod.known_hosts_file(), *host)
        except MirrorError as exc:
            known = False
            lines.append(Line(label + " hosts", False, _clean(exc, url), None))
        else:
            lines.append(Line(label + " hosts", known,
                              f"host key of {host[0]} is " + ("trusted" if known else "not in the private known_hosts"),
                              None if known else f"swarm land hosts add {url}   (a person confirms the fingerprint)"))
    try:
        refs = mirror_factory(url, credential).ls_remote(timeout)
    except MirrorError as exc:
        lines.append(Line(label + " ls-remote", False, f"git ls-remote failed: {_clean(exc, url)}",
                          "check the pinned credential (helper path and GH_CONFIG_DIR, key, agent socket) "
                          "and that the destination is reachable as the supervisor user"))
    else:
        lines.append(Line(label + " ls-remote", True, f"reachable, {len(refs)} branch(es), pinned credential works",
                          None))
    return lines


def checks(cfg: dict, owners=None, *, mirror_factory=Mirror, timeout: float = LS_REMOTE_SECONDS) -> list:
    """Lines for every pinned destination of every owner record (each owner a mapping with `remotes` and
    `credentials`; None means none known), then one line explaining a flagged audit."""
    out, seen = [], set()
    for owner in owners or ():
        try:
            dests = land.destinations_from_owner(owner)
        except land.LandRefused as exc:
            out.append(Line("land destinations", False, str(exc), "activate the job with --remote/--credential"))
            continue
        for d in dests:
            key = (land.normalize_url(d.url), repr(sorted(d.credential.items())))
            if key in seen:
                continue
            seen.add(key)
            out += check_destination(d.name, d.url, d.credential, mirror_factory=mirror_factory, timeout=timeout)
    out.append(Line("land audit", True, AUDIT_SUMMARY, None))
    return out
