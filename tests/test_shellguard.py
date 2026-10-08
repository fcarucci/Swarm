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


V = "swarm verdict --job j --details - "


class VerdictHeredocTests(unittest.TestCase):
    """A heredoc body is hidden only for `swarm verdict ... --details - <<'WORD'` (a fully quoted
    delimiter, nothing uncertain before it); every other shape keeps its body examined."""

    def flagged(self, command):
        self.assertIsNotNone(writes_files(command), command)

    def test_a_quoted_verdict_heredoc_is_not_a_write(self):
        for command in (V + "<<'EOF'\n# R\n1. a -> b\nrm -rf x; git push\n$(rm -rf y) `rm z`\nEOF\n",
                        V + "<<\"EOF\"\nrm -rf x\nEOF\n", V + "<<'EOF'\nEOF\n",
                        "swarm verdict --job j not_met --reason 'a; b > c' --next \"n\" \\\n  --details - <<'REPORT'\n> x\nREPORT"):
            self.assertIsNone(writes_files(command), command)

    def test_here_string_empty_body_quotes_and_comments_are_not_heredocs(self):
        self.flagged("cat <<<EOF\necho x > f\nEOF")                  # (a)
        self.flagged("cat <<EOF\nEOF\necho x > f\nEOF")             # (b)
        self.flagged("echo '<<EOF'\nrm -rf x\nEOF")                  # (c)
        self.flagged("true # <<EOF\nrm -rf x\nEOF")                  # (d)

    def test_a_heredoc_fed_to_a_shell_is_code(self):
        self.flagged("bash <<'EOF'\nrm -rf x\nEOF")                  # (e)
        self.flagged("sh <<EOF\necho > /etc/f\nEOF")
        self.flagged("ssh host <<'EOF'\nrm -rf x\nEOF")

    def test_an_unquoted_delimiter_expands_its_body(self):
        self.flagged(V + "<<EOF\n$(rm -rf x)\nEOF")
        self.flagged(V + "<<EOF\n`rm -rf x`\nEOF")
        self.flagged(V + "<<EOF\nbody\nEOF\nrm -rf x")
        self.flagged(V + "<<\\EOF\nrm -rf x\nEOF")
        self.flagged(V + "<<-'EOF'\nrm -rf x\nEOF")

    def test_the_delimiter_is_the_whole_word(self):
        self.flagged(V + "<<'EOFx'\nEOF\nrm -rf x\n")                 # EOF does not end an EOFx body
        self.flagged(V + "<<'EOF'x\nrm -rf x\nEOF")
        self.flagged(V + "<<'EOF'\nbody\nEOF \nrm -rf x\n")        # a trailing blank is no terminator
        self.flagged(V + "<<'EOF'\nrm -rf x\n")                       # unterminated hides nothing

    def test_arithmetic_and_comment_desync_hide_nothing(self):
        self.flagged("echo $((1<<2))\necho '\n2\n" + V + "<<'EOF'\n' ; rm -rf x\nEOF")
        self.flagged("(true)#'\necho '\n" + V + "<<'EOF'\n' ; rm -rf x\nEOF")
        self.flagged("echo $((1<<2)); " + V + "<<'EOF'\nrm -rf x\nEOF\nrm -rf y")
        self.flagged("echo $'a\\'b'; " + V + "<<'EOF'\nrm -rf x\nEOF")

    def test_only_a_leading_clean_verdict_call_qualifies(self):
        self.flagged("rm x; " + V + "<<'EOF'\nhi\nEOF")
        self.flagged("cd d && " + V + "<<'EOF'\nrm -rf x\nEOF")
        self.flagged(V + "<<'EOF' ; bash <<'E2'\nEOF\nrm x\nE2")
        self.flagged("swarm verdict met ok <<'EOF'\n> x\nEOF")        # no --details -
        self.flagged("swarm verdict --reason $(rm x) --details - <<'EOF'\nhi\nEOF")
        self.flagged("swarm verdict --reason a#b --details - <<'EOF'\n> x\nEOF")

    def test_what_follows_the_terminator_is_examined(self):
        self.flagged(V + "<<'EOF'\nbody\nEOF\nrm -rf x\n")

    def test_the_mask_follows_the_same_rules(self):
        from swarm.shellguard import mask_heredocs
        v = V + "<<'EOF'\nrm -rf x\nEOF\n"
        self.assertNotIn("rm -rf", mask_heredocs(v))
        self.assertEqual(len(mask_heredocs(v)), len(v))
        for other in ("bash <<'EOF'\nrm -rf x\nEOF\n", V + "<<EOF\nrm x\nEOF\n", "cat <<<EOF\nrm x\nEOF\n"):
            self.assertEqual(mask_heredocs(other), other)
