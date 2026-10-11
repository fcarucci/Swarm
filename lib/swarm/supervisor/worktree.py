"""Worktree roots of drive writer seats (schema 25, spec §3.4 [F20]).

A writer seat owns the checkout its workdir lies in: the first directory, walking up from the
realpath, that holds a `.git` file (a linked worktree) or directory (a clone). Found without running
git. Two live writers may not share a checkout, and may not nest one inside the other, so the drive
lock of a `wt:` key stores the root and the claim refuses any overlap (roots_overlap).
"""

from __future__ import annotations

import os


def worktree_root(path: str) -> str | None:
    """The realpath of the first directory at or above `path` that holds a `.git` entry, with `/` as
    the separator (the form a drive lock's key is computed from); None when no ancestor does."""
    d = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d.replace(os.sep, "/")
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def roots_overlap(a: str, b: str) -> bool:
    """Whether two worktree roots are one checkout or nested: equal, or one is inside the other.
    Compares whole path components, so /w/repo-2 is not inside /w/repo."""
    return a == b or _inside(a, b) or _inside(b, a)


def _inside(path: str, root: str) -> bool:
    return path.startswith(root if root.endswith("/") else root + "/")
