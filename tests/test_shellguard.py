from __future__ import annotations

import unittest

from support import ROOT  # noqa: F401

from swarm.shellguard import writes_files  # noqa: E402

DENY = ["echo x > out.txt", "cat a >> b", "ls 2> err.log", "cmd &> all.log", "sed -i 's/a/b/' f",
        "sed --in-place -e s/a/b/ f", "perl -pi -e 's/a/b/' f", "perl -i.bak -pe 's/a/b/' f", "perl -pie 's/a/b/' f", "perl -ibak -pe 's/a/b/' f", "perl -iorig -pe 's/a/b/' f", "perl -wi~ -pe 's/a/b/' f", "rm -rf build", "cd x && mv a b",
        "mkdir -p d", "touch f", "chmod +x f", "chown u f", "ln -s a b", "truncate -s0 f",
        "dd if=/dev/zero of=f bs=1 count=1", "echo hi | tee f", "sudo rm f", "find . -name x -delete",
        "find . -exec rm {} +", "git commit -am x", "git -C repo push", "git stash", "git checkout -- f",
        "curl -o f https://example.org", "wget https://example.org/f", "apply_patch <<'EOF'\n*** Begin Patch\nEOF",
        "python3 -c \"open('f','w').write('x')\"", "python -c 'import pathlib; pathlib.Path(\"f\").write_text(\"x\")'",
        "node -e \"require('fs').writeFileSync('f','x')\""]
ALLOW = ["ls -la", "grep -rn 'a > b' .", "cat file 2>/dev/null", "make test 2>&1 | head -50", "git status",
         "git log --oneline -5", "git diff HEAD~1", "python3 -c 'print(1)'", "echo 'rm -rf /'", "pytest -q",
         ".venv/bin/python -B -m unittest discover -s tests -q", "find . -name '*.py'",
         "swarm post --job j --as 'Lisa' \"VERIFIED: a > b\"", "cat <<'EOF'\nhello\nEOF", "wc -l < input.txt",
         "perl -Mstrict -e 1", "perl -MList::Util=sum -le 'print sum(1,2)'", "echo oops >/dev/stderr",
         "echo out >> /dev/stdout", "read -p x 2>/dev/tty", "cmd 2> /dev/stderr"]


class ShellGuardTests(unittest.TestCase):
    def test_denied(self):
        for cmd in DENY:
            self.assertIsNotNone(writes_files(cmd), cmd)

    def test_allowed(self):
        for cmd in ALLOW:
            self.assertIsNone(writes_files(cmd), cmd)
