"""Drive-seat command refusals (spec 3.4, 4.0, 4.4): target pushes in every recognisable spelling, force pushes,
headless sessions, merges through gh, and the shell-aware splitting that finds them inside sh -c, bash -lc, eval,
script -c, $(...) and backticks. A guardrail, not a boundary: what decides an outcome is `swarm land` and the
driver's mirror audit."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from support import SANDBOX  # noqa: F401  (puts lib/ on sys.path)

from swarm import shellguard
from swarm.shellguard import drive_command_refusal as refuse

T = ["main", "master"]


def on(branch):
    return lambda directory=None: branch


class DriveRefusals(unittest.TestCase):
    def test_push_to_target_refused_in_every_spelling(self):
        for cmd in ("git push origin HEAD:main", "git push origin main", "git push origin feat:refs/heads/main",
                    "git -C /w/a push origin HEAD:main", "git push origin :main",
                    "git push origin 'HEAD:main'", 'git push origin "HEAD:main"', "git push origin HEAD:refs/heads/Main",
                    "git -c core.x=1 push origin HEAD:main", "git --git-dir .git push origin HEAD:main",
                    "git --git-dir=.git --work-tree . push origin HEAD:main", "git --namespace n push origin main",
                    "git push --repo=origin HEAD:main", "git push --repo origin HEAD:main",
                    "git push -u origin HEAD:main", "git push --no-verify origin main", "git push -o x origin main",
                    "git send-pack origin HEAD:main", "git-push origin main",
                    "env GIT_TRACE=1 git push origin main", "sudo -u x git push origin main",
                    "timeout 60 git push origin main", "nohup git push origin main &"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, T, branch_of=on("feat/a")), cmd)

    def test_push_to_another_branch_allowed(self):
        for cmd in ("git push origin HEAD:feat/a", "git push origin feat/mainline", "git push -u origin feat/a",
                    "git push origin feat/a:feat/a", "git push origin HEAD:refs/heads/feat/a",
                    "git push --tags origin", "git push --follow-tags origin feat/a", "git push --no-verify origin x",
                    "git push -o ci.skip origin feat/a", "git push --repo=origin feat/a",
                    "git push --repo=origin -o main feat/a", "git push -o main origin feat/a",
                    "git push --repo=origin --push-option main feat/a", "git push --repo=origin --receive-pack main feat/a",
                    "git push --repo=origin --exec main feat/a", "git push --repo=origin --push-option=main feat/a"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_force_pushes_refused(self):
        for cmd in ("git push --force origin feat/a", "git push -f origin feat/a", "git push origin +feat/a",
                    "git push --force-with-lease origin feat/a", "git push --force-with-lease=main origin feat/a",
                    "git push --force-if-includes origin feat/a", "git push -fu origin feat/a",
                    "git push -uf origin feat/a", "git -c k=v push -f origin x", "bash -lc 'git push --force origin x'",
                    "git push origin +HEAD:feat/a"):
            with self.subTest(cmd=cmd):
                self.assertIn("force", refuse(cmd, ["main"], branch_of=on("feat/a")))

    def test_mirror_all_delete_and_glob_pushes_refused(self):
        for cmd in ("git push origin --mirror", "git push --mirror", "git push origin --all", "git push --branches",
                    "git push origin --delete feat/a", "git push -d origin feat/a", "git push origin :feat/a",
                    "git push origin 'refs/heads/*:refs/heads/*'", "git push origin 'feat/*'", "git push --prune origin x"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_a_push_that_names_no_branch_pushes_the_checked_out_one(self):
        for cmd in ("git push", "git push origin", "git push -u origin", "git push origin HEAD", "git push origin @",
                    "git push --repo=origin"):
            with self.subTest(cmd=cmd):
                self.assertIn("main", refuse(cmd, ["main"], branch_of=on("main")))
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")))
                self.assertTrue(refuse(cmd, ["main"], branch_of=on(None)))     # detached: unknown, refused
                self.assertTrue(refuse(cmd, ["main"]))                          # nothing says what is checked out

    def test_branch_of_gets_the_dash_c_directory(self):
        seen = []
        refuse("git -C /w/other push origin", ["main"], branch_of=lambda d=None: seen.append(d) or "feat/a")
        refuse("git push", ["main"], branch_of=lambda d=None: seen.append(d) or "feat/a")
        self.assertEqual(seen, ["/w/other", None])

    def test_aliases_and_push_settings_cannot_hide_a_push(self):
        for cmd in ("git config alias.p push", "git config remote.origin.push HEAD:main",
                    "git config --global alias.p push", "git -c alias.p=push p origin HEAD:main",
                    "git remote set-url --push origin x", "git config url.x.pushInsteadOf y",
                    "git config push.default matching"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        self.assertIsNone(refuse("git config user.name x", ["main"]))
        self.assertIsNone(refuse("git config --get remote.origin.url", ["main"]))

    def test_wrappers_do_not_hide_a_push_or_a_launch(self):
        for cmd in ("sh -c 'git push origin HEAD:main'", "bash -lc 'git push --force origin x'",
                    "eval 'git push origin HEAD:main'", "zsh -c \"git push origin main\"",
                    "script -qc 'claude -p hi' /dev/null", "sh -c 'sh -c \"git push origin main\"'",
                    "echo $(git push origin main)", "echo `git push origin main`", "echo \"$(git push origin main)\"",
                    "xargs git push origin <<< HEAD:main", "ls | xargs -I{} git push origin {}",
                    "watch 'git push origin main'", "su -c 'git push origin main' x",
                    "if true; then git push origin main; fi", "for b in a; do git push origin main; done",
                    "{ git push origin main; }", "(git push origin main)", "true && git push origin main",
                    "false || git push origin main", "git status; git push origin main"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_headless_sessions_refused(self):
        for cmd in ("claude -p 'do it'", "claude --print x", "claude --print=yes hi", "claude -pc hi", "claude -cp hi",
                    "echo hi | claude --output-format json -p", "timeout 60 claude -p hi", "setsid claude -p hi",
                    "sh -c 'claude -p hi'", "npx @anthropic-ai/claude-code -p hi",
                    "node ~/.npm/claude/cli.js -p hi", "~/.local/bin/claude -p hi", "claude --model opus -p x"):
            with self.subTest(cmd=cmd):
                self.assertIn("claude -p", refuse(cmd, ["main"]))
        for cmd in ("codex exec --json 'x'", "codex e 'do stuff'", "codex --full-auto exec hi", "nohup codex exec hi &",
                    "codex -m gpt exec x", "codex -c k=v e x", "npx @openai/codex exec x", "sh -c 'codex exec x'"):
            with self.subTest(cmd=cmd):
                self.assertIn("codex exec", refuse(cmd, ["main"]))

    def test_gh_merges_and_ref_moves_refused(self):
        for cmd in ("gh pr merge 12 --admin --merge", "gh pr merge 12", "gh -R o/r pr merge 3",
                    "gh api -X PATCH repos/o/r/git/refs/heads/main -f sha=abc -F force=true",
                    "gh api repos/o/r/git/refs -f ref=refs/heads/x", "gh api repos/o/r/merges -f base=main -f head=x",
                    "gh api repos/o/r/git/commits -f message=x"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"]), cmd)
        for cmd in ("gh pr view 12", "gh pr list", "gh api repos/o/r/pulls", "gh run view 5", "gh pr create --fill",
                    "gh issue list"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"]), cmd)

    def test_ordinary_commands_allowed(self):
        for cmd in ("git status", "git fetch origin", "swarm done --job J", "python3 -m unittest test_x",
                    "echo 'git push origin main'", "codex --version", "claude", "claude --version", "claude mcp list",
                    "grep 'claude -p' file", "cat <<'EOF'\ngit push origin main\nEOF", "git log --oneline",
                    "git diff main...HEAD", "git checkout main", "cd tests && python3 -B -m unittest x 2>&1 | tail",
                    "echo hi > /dev/null 2>&1", "ls; echo 'codex exec'", "gh --version", "cargo build 2>&1"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_no_targets_still_refuses_force_and_headless(self):
        self.assertIsNone(refuse("git push origin main", (), branch_of=on("x")))
        self.assertIn("force", refuse("git push -f origin main", ()))
        self.assertIn("claude -p", refuse("claude -p x", ()))

    def test_targets_may_be_a_function_called_only_when_needed(self):
        calls = []

        def targets():
            calls.append(1)
            return ["main"]
        self.assertIsNone(refuse("git status; ls", targets))
        self.assertEqual(calls, [])
        self.assertTrue(refuse("git push origin main", targets))
        self.assertEqual(calls, [1])
        self.assertTrue(refuse("git push origin feat/a; git push origin feat/b; git push origin main", targets))
        self.assertEqual(calls, [1, 1])      # once per command, however many pushes it has

    def test_targets_given_in_mixed_case_match_a_lower_case_push(self):
        self.assertTrue(refuse("git push origin HEAD:develop", ["Develop"]))
        self.assertTrue(refuse("git push origin HEAD:DEVELOP", ["develop"]))

    def test_targets_are_compared_without_case_and_with_or_without_refs_heads(self):
        self.assertTrue(refuse("git push origin HEAD:MAIN", ["main"]))
        self.assertTrue(refuse("git push origin HEAD:main", ["refs/heads/main"]))
        self.assertIsNone(refuse("git push origin HEAD:mainline", ["main"]))

    def test_empty_and_odd_input_never_raises(self):
        for cmd in ("", "   ", ";;", "git", "git push origin 'unterminated", "$(", "`", "(((", "git push \\"):
            refuse(cmd, ["main"], branch_of=on("feat/a"))
        self.assertIsNone(refuse("", ["main"]))
        self.assertIsNone(refuse(None, ["main"]))

    def test_a_script_fed_to_a_shell_on_stdin_is_looked_at(self):
        self.assertTrue(refuse("bash <<'EOF'\ngit push origin main\nEOF", ["main"]))
        self.assertTrue(refuse("sh <<EOF\nclaude -p hi\nEOF", ["main"]))
        self.assertIsNone(refuse("cat <<'EOF'\ngit push origin main\nEOF", ["main"]))
        self.assertTrue(refuse("python3 - <<'EOF'\nprint(1)\nEOF", ["main"]))    # code from a heredoc (round 4)

    def test_inline_scripts_that_push_to_a_target_or_launch_a_session_are_refused(self):
        for cmd in ("python3 -c \"import subprocess;subprocess.run(['git','push','origin','HEAD:main'])\"",
                    "python3 -c \"import subprocess;subprocess.run(['claude','-p','x'])\"",
                    "node -e \"require('child_process').exec('git push -f origin x')\"",
                    "nodejs -e \"require('child_process').exec('git push origin refs/heads/main')\"",
                    "perl -e 'system(\"codex exec x\")'", "python3.12 -c \"import os; os.system('git push --mirror')\"",
                    "python3 - <<'EOF'\nimport os; os.system('git push origin main')\nEOF",
                    "ruby -e 'system(\"claude --print hi\")'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        for cmd in ("python3 -c \"import subprocess;subprocess.run(['git','push','origin','feat/a'])\"",
                    "python3 -c 'print(1)'", "python3 -c \"print('mainline')\"", "python3 -m unittest x",
                    "perl -e 'print 1'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_rr_abbreviated_long_options_and_dwim_destinations(self):
        for cmd in ("git push --mirr origin", "git push --al origin", "git push --force-w origin HEAD:feat/a",
                    "git push --delet origin feat/a", "git push --prun origin", "git push --bran origin",
                    "git push origin HEAD:heads/main", "git push origin HEAD:refs/main",
                    "git push origin 'HEAD:{main,x}'", "git push origin 'HEAD:$B'", "git push origin 'HEAD:ma\\in'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        for cmd in ("git push --tags origin", "git push --no-verify origin feat/a", "git push --set-upstream origin feat/a",
                    "git push --follow-tags origin feat/a", "git push --porcelain origin feat/a"):
            self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_rr_git_config_by_environment_and_config_env(self):
        for cmd in ("GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.push GIT_CONFIG_VALUE_0=HEAD:main git push origin",
                    "GIT_CONFIG_PARAMETERS=\"'remote.origin.push=HEAD:main'\" git push origin",
                    "PV=HEAD:main git --config-env=remote.origin.push=PV push origin",
                    "git --config-env remote.origin.push=PV push origin", "export GIT_CONFIG_COUNT=1; git push origin feat/a",
                    "env GIT_CONFIG_COUNT=1 git p", "sh -c 'GIT_CONFIG_COUNT=1 git p'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        self.assertIsNone(refuse("git --config-env=user.name=NAME log", ["main"]))

    def test_rr_shells_reading_a_pipe_and_gh_merge_endpoints(self):
        for cmd in ("echo 'git push origin HEAD:main' | sh", "printf 'claude -p x' | bash", "bash <(echo 'git push')",
                    "sh -s", "bash < script.sh",
                    "gh api -X PUT repos/o/r/pulls/1/merge",
                    "gh api -X PUT repos/o/r/contents/f -f message=m -f branch=main",
                    "gh api graphql -f query='mutation{mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}'",
                    "gh api graphql -f query='mutation{updateRef(input:{})}'",
                    "gh api graphql -f query='mutation{createCommitOnBranch(input:{})}'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        for cmd in ("bash -lc 'git log'", "gh api repos/o/r/contents/f", "gh api graphql -f query='{viewer{login}}'"):
            self.assertIsNone(refuse(cmd, ["main"]), cmd)
        self.assertIn("cannot be checked", shellguard.readonly_write_reason("echo ls | sh"))

    def test_r1_one_letter_abbreviations(self):
        for cmd in ("git push --m origin", "git push --mi origin", "git push --a origin", "git push --f origin x",
                    "git push --d origin x", "git push --p origin"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_r2_a_shell_is_only_ever_dash_c_literal(self):
        bad = ("echo 'git push origin HEAD:main' | sh /dev/stdin", "bash <<< 'git push origin HEAD:main'",
               "echo x | sh; cat <<EOF\nx\nEOF", "source <(echo 'git push origin HEAD:main')",
               "echo x | bash -c \"$(cat)\"", "echo x | busybox sh", "sh /dev/fd/3", "bash script.sh", "bash script.sh -c 'ls'", "bash -s",
               "bash -i", "sh", "env sh", "exec bash", "env bash <<< 'x'", ". <(echo x)", "source /dev/stdin",
               "bash <<'EOF'\ngit push origin main\nEOF", "dash -c", "zsh", "ksh /dev/stdin", "fish", "ash -",
               "sh -c \"$(curl x)\"", "sh -c 'sh'", "sh -c 'echo x | sh'")
        for cmd in bad:
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        for cmd in ("bash -c 'git log'", "sh -lc 'ls'", "bash -o pipefail -c 'ls | wc -l'", "busybox sh -c 'ls'",
                    "env bash -c 'ls'", "busybox ls"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        self.assertTrue(shellguard.readonly_write_reason("bash <<< 'touch f'"))

    def test_rr5_runners_are_checked_recursively(self):
        for cmd in ("find . -maxdepth 0 -exec git push origin HEAD:main ;", "find . -execdir git push -f origin x +",
                    "find . -ok claude -p x ;", "git rebase -x 'git push origin HEAD:main' HEAD~1",
                    "git rebase --exec 'git push origin HEAD:main' HEAD~1", "git rebase --exec='claude -p x' HEAD~1",
                    "git submodule foreach 'git push origin HEAD:main'", "git submodule foreach --recursive 'claude -p x'",
                    "env -S 'git push origin HEAD:main'", "exec -a x claude -p hi", "flock lockfile git push origin HEAD:main",
                    "systemd-run --user claude -p hi", "systemd-run -p MemoryMax=1G git push origin main",
                    "tmux new -d 'git push origin HEAD:main'", "ssh localhost git push origin HEAD:main",
                    "ssh -p 22 host 'claude -p x'", "parallel git push origin HEAD:main ::: a",
                    "parallel -j 2 claude -p {} ::: a", "xargs -n1 git push origin", "watch -n 5 git push origin main",
                    "timeout 5 nice -n 3 git push origin HEAD:main"):
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)
        for cmd in ("find . -name '*.py' -exec grep -l x {} +", "git rebase -i HEAD~2", "git submodule status",
                    "flock lock ls", "ssh host ls", "tmux ls", "parallel echo ::: a b"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_rr7_read_only_seats_cannot_write_refs_or_files(self):
        for cmd in ("git branch -f main HEAD~3", "git branch -D x", "git branch -m a b", "git branch newbranch",
                    "git branch -fd x", "git tag v1", "git update-ref refs/heads/x HEAD", "git reset --hard", "git checkout -B x",
                    "git commit -m x", "git stash", "git push origin x", "git config user.name x", "git config --add a b",
                    "git symbolic-ref HEAD refs/heads/x", "git remote add x y", "git reflog expire --expire=now --all",
                    "git hash-object -w f", "python3 - <<'EOF'\nopen('f','w').write('x')\nEOF",
                    "python3 - <<'EOF'\nimport subprocess\nEOF", "tar xf a.tar", "tar -xzf a.tgz", "sort -o f a",
                    "awk '{print > \"f\"}' a", "sed -n '1w out' f", "pip install x", "python3 -m pip install x"):
            with self.subTest(cmd=cmd):
                self.assertTrue(shellguard.readonly_write_reason(cmd), cmd)
        for cmd in ("git branch", "git branch --list", "git branch -a", "git branch -v", "git branch --contains abc",
                    "git config --get user.name", "git config --list", "git symbolic-ref HEAD", "git remote -v", "git remote show origin",
                    "git reflog", "git hash-object f", "tar tf a.tar", "sort a", "awk '{print $1}' a", "sed -n 1p f",
                    "git log", "git tag -l" if False else "git show HEAD"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(shellguard.readonly_write_reason(cmd), cmd)

    def test_quoting_and_assignments(self):
        self.assertTrue(refuse("GIT_DIR=x git push origin main", ["main"]))
        self.assertTrue(refuse("git push 'origin' \"main\"", ["main"]))
        self.assertTrue(refuse("git\tpush\torigin\tmain", ["main"]))
        self.assertTrue(refuse("git push origin ma\\in", ["main"]))
        self.assertTrue(refuse("git push \\\norigin main", ["main"]))


P = "git push origin HEAD:main"


class Round4OneRule(unittest.TestCase):
    """Round 4: code that can run commands is a literal argument that is scanned; code that arrives any other way
    (stdin, a pipe, a here-string, a heredoc, <(...), a variable or $(...), eval of a non-literal, a script the seat
    could write) is refused. Each command below is a bypass the round-3 guard let through."""

    def bad(self, *cmds, branch="feat/a"):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertTrue(refuse(cmd, ["main"], branch_of=on(branch)), cmd)

    def good(self, *cmds):
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_g1_dash_dash_and_options_after_c(self):
        self.bad(f"bash -c -- '{P}'", f"sh -c -x '{P}'", f"bash -xc -- '{P}'", "bash -c --", f"bash -- -c '{P}'")
        self.good("bash -c -- 'ls'", "bash -c 'ls' -- a b")

    def test_g2_the_program_or_the_code_is_an_expansion(self):
        self.bad(f"X='{P}'; $X", f"X='{P}'; bash -c \"$X\"", f"X='{P}'; eval \"$X\"", f"bash -c '$(echo {P})'",
                 f"bash -c 'eval \"$X\"'", "${X} push origin HEAD:main", "$'\\x67it' push origin HEAD:main",
                 "/bin/?h -c 'git push origin HEAD:main'", "{git,push,origin,HEAD:main}", "`echo git` push origin x",
                 f"python3 -c \"$CODE\"", "awk \"$PROG\" f", "bash \"$SCRIPT\"", "source \"$F\"",
                 f"read -r X <<< '{P}'; $X", "set -- git push; \"$@\" origin HEAD:main")
        self.good("echo $HOME", "bash -c 'echo $HOME'", "ls *.py", "[ -f x ] && echo y", "git commit -m \"$(cat msg)\"",
                  "echo {a,b}", "eval 'git log'", "find . -name '*.py' -exec grep -l x {} +")

    def test_every_shell_is_a_shell(self):
        for sh in ("rbash", "mksh", "lksh", "posh", "yash", "oksh", "loksh", "hush", "elvish", "nu", "xonsh", "pwsh",
                   "powershell", "ksh93", "tcsh", "csh"):
            self.bad(f"echo '{P}' | {sh}", f"{sh} -c '{P}'", f"{sh} <<< '{P}'")
        self.bad("pwsh -EncodedCommand ZQB4AA==", "pwsh -e ZQ==", "powershell -EncodedCommand ZQ==", "bash -ic 'ls'", f"pwsh -Command '{P}'", "pwsh -Command -",
                 f"powershell -c \"{P}\"")
        self.good("pwsh -c 'Get-ChildItem'", "pwsh -Version", "rbash -c 'ls'", "nu -c 'ls'")

    def test_interpreters_take_code_only_as_a_literal_argument(self):
        self.bad(f"awk 'BEGIN{{system(\"{P}\")}}'", f"gawk 'BEGIN{{system(\"{P}\")}}'", f"mawk -e 'BEGIN{{system(\"{P}\")}}'",
                 f"echo \"import os; os.system('{P}')\" | python3", f"python3 <<< \"import os; os.system('{P}')\"",
                 "python3 - <<'EOF'\nprint(1)\nEOF", "python3", "python3 -", "python3 /dev/stdin", "echo x | perl",
                 "node", "node -", "ruby < x.rb", f"lua -e 'os.execute(\"{P}\")'", f"php -r 'system(\"{P}\");'",
                 f"tclsh <<< 'exec {P}'", f"expect -c 'spawn {P}'", f"sed -n '1e {P}' f", f"vim -c ':!{P}' -c ':q'",
                 f"vim '+!{P}'", f"make -f <(printf 'a:\\n\\t{P}\\n')", "make -f -", f"osascript -e 'do shell script \"{P}\"'",
                 "echo 'print 1' | lua", "perl -ple 'x' -", "python3 -i", "awk -f /dev/stdin", "sed -f - f",
                 f"ed f <<< '!{P}'", f"bun -e 'Bun.spawn([\"git\",\"push\",\"origin\",\"HEAD:main\"])'",
                 f"deno eval 'new Deno.Command(\"git\", {{args: [\"push\",\"origin\",\"HEAD:main\"]}}).spawn()'",
                 f"perl -le 'system(\"{P}\")'", f"python3 -Bc \"import os; os.system('{P}')\"",
                 f"node --eval='require(\"child_process\").execSync(\"{P}\")'")
        self.good("python3 -c 'print(1)'", "python3 -m unittest x", "python3 -B -m unittest x", "python3 --version",
                  "python3 -V", "node --version", "perl -ne 'print' f", "perl -v", "ruby -e 'puts 1'", "sed -n 1p f",
                  "sed -i 's/a/b/' f", "sed -i.bak -e 's/a/b/' f", "awk -F: '{print $1}' f", "awk -v x=1 '{print x}' f",
                  "vim -c ':wq' f", "make", "make test", "make -C sub", "lua -v", "php -v",
                  "python3 -c 'import sys' -m x", "python3 -c 'import sys; print(sys.argv)' -i",
                  "cat <<EOF > f\nx\nEOF")

    def test_scheduling_is_refused(self):
        self.bad(f"at now <<< '{P}'", "at now + 1 minute -f job.sh", "batch", f"crontab <<< '* * * * * {P}'",
                 "crontab f", "crontab -r")
        self.good("crontab -l", "atq" if False else "ls")

    def test_git_settings_and_variables_that_run_commands(self):
        self.bad(f"GIT_SSH_COMMAND='{P}' git fetch", f"export GIT_SSH_COMMAND='{P}'; git fetch",
                 f"GIT_EXTERNAL_DIFF='sh -c \"{P}\"' git diff", f"GIT_PAGER='{P}' git log", f"PAGER='{P}' git log",
                 f"GIT_EDITOR='{P}' git commit", f"GIT_SEQUENCE_EDITOR='{P}' git rebase -i HEAD~2",
                 f"EDITOR='claude -p x' git commit", f"git -c core.pager='sh -c \"{P}\"' log",
                 f"git -c core.editor='{P}' commit", f"git -c diff.x.textconv='{P}' diff", f"git -c credential.helper='!{P}' fetch",
                 f"git -c sequence.editor='{P}' rebase -i HEAD~1", f"git -c pager.log='{P}' log",
                 "git -c core.sshCommand=x fetch", "git --config-env core.pager=X log", "git config core.pager x",
                 "git config --global core.editor x", "git config diff.external x", "GIT_SSH=./evil git fetch",
                 "GIT_EDITOR=\"$E\" git commit", "BASH_ENV=x.sh bash -c true", "ENV=x.sh sh -c true",
                 "LD_PRELOAD=./x.so ls", "env 'BASH_FUNC_ls%%=() { git push origin HEAD:main; }' bash -c ls",
                 "NODE_OPTIONS='--require ./x.js' node --version", "PERL5OPT=-Mx perl -e 1", f"PS4='$({P})' bash -xc true",
                 "GIT_EXEC_PATH=. git status", "git -c protocol.ext.allow=always fetch", "git fetch 'ext::sh -c x'",
                 f"git fetch --upload-pack='{P}' .", f"git difftool -x 'sh -c \"{P}\"'",
                 f"git filter-branch --tree-filter '{P}' HEAD", f"git bisect run sh -c '{P}'", f"git bisect run {P}",
                 "git subtree push --prefix=d origin main", "git subtree push -P d origin HEAD:refs/heads/main",
                 f"rsync -e 'sh -c \"{P}\"' a h:b", f"ssh -o ProxyCommand='{P}' host", f"ssh -oProxyCommand='{P}' host",
                 f"ssh -o 'LocalCommand {P}' -o PermitLocalCommand=yes host", f"scp -o ProxyCommand='{P}' a h:b",
                 f"echo '{P}' | ssh localhost", f"ssh localhost \"$X\"")
        self.good("GIT_EDITOR=true git rebase --continue", "GIT_SSH_COMMAND='ssh -i ~/.ssh/k' git fetch",
                  "git -c core.editor=true commit --amend --no-edit", "git -c core.pager=less log", "PAGER=cat git log",
                  "git config --get core.pager", "git -c user.name=x log", "git bisect run make test",
                  "git subtree push --prefix=d origin feat/a", "rsync -e 'ssh -p 2222' a h:b", "ssh host ls",
                  "ssh -N -L 1:x:2 host", "NODE_OPTIONS=--max-old-space-size=4096 node --version", "PYTHONPATH=lib python3 -m x")

    def test_wrappers_are_unwrapped(self):
        self.bad(f"chroot / sh -c '{P}'", f"chroot --userspec=x / sh -c '{P}'", f"unshare -r sh -c '{P}'",
                 f"nsenter -t 1 -m sh -c '{P}'", f"dbus-run-session -- sh -c '{P}'", f"echo '{P}' | busybox -- sh",
                 f"busybox awk 'BEGIN{{system(\"{P}\")}}'", f"setsid -w sh -c '{P}'", f"nohup {P}", f"screen -dm {P}",
                 f"screen -S x -dm bash -c '{P}'", "screen -X stuff 'git push origin HEAD:main\\n'", f"tmux new -d {P}",
                 f"tmux new-session -d -s x '{P}'", "tmux send-keys -t x 'git push origin HEAD:main' Enter",
                 f"tmux run-shell '{P}'", f"tmux if-shell true '{P}'", f"tmux -c '{P}'", f"echo '{P}' | sudo -s",
                 f"echo '{P}' | sudo -i", f"echo '{P}' | su", f"echo '{P}' | su -l x", f"echo '{P}' | script -q /dev/null",
                 "chroot /", "unshare -r", f"strace -f -o /dev/null sh -c '{P}'", f"coproc sh -c '{P}'",
                 f"env -S'{P}'", f"env --split-string='{P}'", f"sg grp '{P}'", "newgrp grp", f"runuser -u x -- {P}",
                 f"pkexec --user x sh -c '{P}'", f"trap '{P}' EXIT", f"alias x='{P}'",
                 f"swarm bg --job J -- '{P}'")
        self.good("tmux ls", "tmux new -d", "screen -ls", "sudo -u x ls", "nohup make &", "setsid make", "busybox ls",
                  "swarm bg --job J -- 'make test'", "trap - EXIT")

    def test_xargs_and_parallel_do_not_run_code_from_their_input(self):
        self.bad(f"parallel git push origin ::: HEAD:main", f"parallel ::: '{P}'", f"echo '{P}' | parallel",
                 f"parallel bash -c ::: '{P}'", f"parallel 'git push origin {{}}' ::: main",
                 f"echo '{P}' | xargs -I{{}} sh -c '{{}}'", f"echo '{P}' | xargs sh -c", "echo x | xargs python3 -c",
                 "echo 's/a/b/' | xargs -I{} sed -i {} f", "find . -exec {} ;", "find . -name x -exec sh {} ;")
        self.good("parallel echo ::: a b", "xargs grep x", "xargs sed -i s/a/b/", "find . -exec grep -l x {} +")

    def test_headless_launches(self):
        self.bad("codex app-server", "codex proto", "codex mcp-server", "codex e hi", "npx @openai/codex app-server",
                 "codex --model x app-server", "claude mcp serve")
        self.good("codex --help", "codex", "claude mcp list")


@unittest.skipUnless(shutil.which("git"), "needs git")
class Round4ScriptFiles(unittest.TestCase):
    """A script file runs (directly, through a shell or an interpreter, or sourced) only when it is tracked
    unmodified at HEAD in its repository, or when the seat cannot write it."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.repo = os.path.realpath(self.td.name)
        git = ["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        os.makedirs(os.path.join(self.repo, "scripts"))
        for rel, text in (("scripts/check.sh", "echo ok\n"), ("tool.py", "print(1)\n"), ("env.sh", "X=1\n"),
                          ("mod.sh", "echo ok\n"), ("staged.sh", "echo ok\n"), ("Makefile", "a:\n\techo a\n")):
            with open(os.path.join(self.repo, rel), "w", newline="\n") as f:
                f.write(text)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-qm", "init"], check=True)
        with open(os.path.join(self.repo, "mod.sh"), "a") as f:
            f.write("git push origin HEAD:main\n")
        with open(os.path.join(self.repo, "staged.sh"), "a") as f:
            f.write("git push origin HEAD:main\n")
        subprocess.run(git + ["add", "staged.sh"], check=True)
        with open(os.path.join(self.repo, "new.sh"), "w") as f:
            f.write("git push origin HEAD:main\n")

    def at(self, cmd, cwd="__repo__"):
        return refuse(cmd, ["main"], branch_of=on("feat/a"), cwd=self.repo if cwd == "__repo__" else cwd)

    def test_tracked_unmodified_scripts_run_in_every_form(self):
        for cmd in ("./scripts/check.sh", "scripts/check.sh", "bash scripts/check.sh", "sh ./scripts/check.sh",
                    "python3 tool.py", "source env.sh", ". ./env.sh", "make -f Makefile", "bash -- scripts/check.sh",
                    f"bash {self.repo}/scripts/check.sh", "cd scripts && ./check.sh", "cd scripts; bash check.sh",
                    "timeout 60 ./scripts/check.sh", "env A=1 ./scripts/check.sh"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.at(cmd), cmd)

    def test_untracked_modified_or_staged_scripts_are_refused_in_every_form(self):
        for name in ("new.sh", "mod.sh", "staged.sh", "missing.sh"):
            for form in ("./{}", "bash {}", "sh {}", "source {}", ". ./{}", "python3 {}", "zsh ./{}", "{}"[0:0] + "./{}"):
                cmd = form.format(name)
                with self.subTest(cmd=cmd):
                    self.assertTrue(self.at(cmd), cmd)
        self.assertTrue(self.at("bash --rcfile new.sh -i -c 'ls'"))
        self.assertTrue(self.at("bash --rcfile new.sh -c 'ls'"))
        self.assertIn("tracked unmodified at HEAD", self.at("bash new.sh"))
        self.assertIn("tracked unmodified at HEAD", self.at("./new.sh"))

    def test_a_relative_script_needs_a_known_directory(self):
        self.assertTrue(self.at("./scripts/check.sh", cwd=None))
        self.assertTrue(self.at("env -C scripts ./scripts/check.sh"))   # the command runs elsewhere
        self.assertTrue(self.at("cd \"$D\"/.. && ./scripts/check.sh"))
        self.assertTrue(self.at("(cd scripts) ; ./check.sh"))          # a subshell's cd does not last
        self.assertIsNone(self.at(f"{self.repo}/scripts/check.sh", cwd=None))

    @unittest.skipIf(os.name == "nt", "symlinks")
    def test_a_link_out_of_the_repository_and_a_renamed_shell_are_refused(self):
        os.symlink(os.path.join(self.repo, "new.sh"), os.path.join(self.repo, "scripts", "link.sh"))
        self.assertTrue(self.at("./scripts/link.sh"))
        sh = os.path.realpath(shutil.which("sh") or "/bin/sh")
        os.symlink(sh, os.path.join(self.repo, "x"))
        for cmd in (f"./x -c '{P}'", f"echo '{P}' | ./x", f"{self.repo}/x -c '{P}'", f"echo '{P}' | {self.repo}/x"):
            with self.subTest(cmd=cmd):
                self.assertTrue(self.at(cmd), cmd)
        bindir = os.path.join(self.repo, "bin")
        os.makedirs(bindir)
        os.symlink(sh, os.path.join(bindir, "mysh"))
        with mock.patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}):
            self.assertTrue(self.at(f"echo '{P}' | mysh"))
            self.assertTrue(self.at(f"mysh -c '{P}'"))
            self.assertIsNone(self.at("mysh -c 'ls'"))

    def test_files_the_seat_cannot_write_run(self):
        with mock.patch.object(shellguard.os, "access", return_value=False):
            self.assertIsNone(self.at("./new.sh"))
        self.assertFalse(shellguard.tracked_unmodified(os.path.join(self.repo, "new.sh")))
        self.assertTrue(shellguard.tracked_unmodified(os.path.join(self.repo, "scripts", "check.sh")))
        self.assertFalse(shellguard.tracked_unmodified(os.path.join(self.repo, "mod.sh")))
        self.assertFalse(shellguard.tracked_unmodified(os.path.join(self.repo, "staged.sh")))

    def test_the_swarm_launcher_runs(self):
        from swarm import paths
        launcher = os.path.join(self.repo, "new.sh")
        with mock.patch.object(paths, "agent_bin", return_value=shellguard.os.path.realpath(launcher)):
            self.assertIsNone(self.at(f"{launcher} post --job J hi"))
        self.assertTrue(self.at(f"{launcher} post --job J hi"))


class Splitting(unittest.TestCase):
    def test_split_commands_resolves_quotes_and_lists_substitutions(self):
        self.assertEqual(shellguard.split_commands("git push origin 'HEAD:main'"),
                         [["git", "push", "origin", "HEAD:main"]])
        self.assertEqual(shellguard.split_commands("a b; c | d && e || f\ng"),
                         [["a", "b"], ["c"], ["d"], ["e"], ["f"], ["g"]])
        self.assertEqual(shellguard.split_commands("echo $(git push) x"), [["git", "push"], ["echo", "$SUB", "x"]])
        self.assertEqual(shellguard.split_commands("echo `ls -l`"), [["ls", "-l"], ["echo", "$SUB"]])
        self.assertEqual(shellguard.split_commands("echo 'a;b' \"c|d\""), [["echo", "a;b", "c|d"]])
        self.assertEqual(shellguard.split_commands("cmd 2>&1 >out <in"), [["cmd"]])
        self.assertEqual(shellguard.split_commands("cmd # comment; other"), [["cmd"]])
        self.assertEqual(shellguard.split_commands("cmd '' x"), [["cmd", "", "x"]])

    def test_iter_commands_unwraps_wrappers_and_shells(self):
        got = [(p, a) for p, a, _ in shellguard.iter_commands("sudo -u x env A=1 timeout 5 nice -n 3 git push origin x")]
        self.assertEqual(got, [("git", ["push", "origin", "x"])])
        got = [p for p, _, _ in shellguard.iter_commands("bash -lc 'echo hi; git status'")]
        self.assertEqual(got, ["echo", "git", "bash"])
        got = [(p, w) for p, _, w in shellguard.iter_commands("xargs -n1 git push")]
        self.assertEqual(got, [("git", {"xargs"})])

    def test_nesting_depth_is_bounded_and_too_deep_is_refused(self):
        cmd = "git push origin feat/a"
        for _ in range(12):
            cmd = "sh -c " + shlex.quote(cmd)
        self.assertIn("too deeply", shellguard.drive_command_refusal(cmd, ["main"]))
        self.assertIn("too deeply", shellguard.readonly_write_reason(cmd))
        deep = "eval " * 3000 + "git push origin feat/a"
        self.assertIn("too deeply", shellguard.drive_command_refusal(deep, ["main"]))     # no RecursionError
        shallow = "sh -c " + shlex.quote("sh -c " + shlex.quote("git push origin feat/a"))
        self.assertIsNone(shellguard.drive_command_refusal(shallow, ["main"]))


class ReadOnlyWrites(unittest.TestCase):
    def test_writes_found_through_wrappers_git_options_and_sql(self):
        for cmd in ("sh -c 'echo x > f'", "bash -lc \"tee f\"", "eval 'rm -rf x'", "script -qc 'touch f' /dev/null",
                    "git -c a=b commit -am x", "git -C /w commit -m x", "git --git-dir=.git add -A",
                    "git -c k=v push origin x", "git worktree add ../x", "git clone u d",
                    "git update-ref refs/heads/x HEAD",
                    "sqlite3 board.db 'update seats set role=1'", "sqlite3 b.db \"insert into t values (1)\"",
                    "sqlite3 b.db < dump.sql", "sqlite3 b.db '.import f t'", "sqlite3 b.db 'PRAGMA user_version = 2'",
                    "echo $(touch f)", "watch 'rm f'"):
            with self.subTest(cmd=cmd):
                self.assertTrue(shellguard.readonly_write_reason(cmd), cmd)

    def test_sqlite_readonly_is_allowed_whatever_the_statement(self):
        self.assertIsNone(shellguard.readonly_write_reason("sqlite3 -readonly b.db 'update t set a=1'"))
        self.assertTrue(shellguard.readonly_write_reason("sqlite3 b.db 'update t set a=1'"))

    def test_reads_are_not_writes(self):
        for cmd in ("git log --oneline -3", "git diff HEAD~1", "git show HEAD:f", "git -C /w status", "git -c a=b log",
                    "cd tests && python3 -B -m unittest test_x", "ls -la", "sqlite3 -readonly b.db 'select 1'",
                    "sqlite3 b.db 'select * from seats'", "swarm status --job J", "cat f | grep x", "git branch",
                    "git fetch origin", "echo hi", "bash -lc 'git log'", "find . -name '*.py'", "wc -l f"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(shellguard.readonly_write_reason(cmd), cmd)

    def test_a_script_fed_to_a_shell_on_stdin_is_looked_at(self):
        self.assertTrue(shellguard.readonly_write_reason("bash <<'EOF'\ngit commit -m x\nEOF"))
        self.assertTrue(shellguard.readonly_write_reason("sh <<EOF\necho x > f\nEOF"))
        self.assertEqual(shellguard.heredoc_bodies("cat <<'A'\none\ntwo\nA\ncat <<'B'\nthree\nB"), ["one\ntwo", "three"])
        self.assertEqual(shellguard.heredoc_bodies("ls"), [])


class VerdictCalls(unittest.TestCase):
    def test_verdict_calls_but_not_show(self):
        for cmd in ("swarm verdict --job J met ok", "swarm verdict --job J --artifact x@1 not_met --reason r --next n",
                    "swarm verdict --job J unreachable --details -", "swarm --config c verdict --job J met x",
                    "bash -lc 'swarm verdict --job J met x'", "cd d && ~/bin/swarm verdict --job J --scope goal met x",
                    "swarm verdict", "swarm verdict --job J --details - met x"):
            with self.subTest(cmd=cmd):
                self.assertTrue(shellguard.swarm_verdict_call(cmd), cmd)
        for cmd in ("swarm verdict --job J show", "swarm verdict --job J --artifact x@1 show", "swarm status",
                    "echo swarm verdict met", "swarm post --job J verdict met", "ls", ""):
            with self.subTest(cmd=cmd):
                self.assertFalse(shellguard.swarm_verdict_call(cmd), cmd)


class McpTools(unittest.TestCase):
    def test_allowlist(self):
        for tool in ("mcp__a__get_x", "mcp__a__list_x", "mcp__a__search_x", "mcp__a__read_x", "mcp__a__find_x",
                     "mcp__a__count_x", "mcp__a__describe_x", "mcp__a__fetch_x", "mcp__a__view_x",
                     "mcp__a__x_get_y", "mcp__a__browser_snapshot", "mcp__a__unifi_tool_index"):
            self.assertTrue(shellguard.mcp_read_allowed(tool), tool)
        for tool in ("mcp__a__create_x", "mcp__a__x", "mcp__a__execute", "mcp__a__get_and_update", "mcp__a__list_and_send",
                     "mcp__a__render_template", "mcp__a__browser_click", "mcp__a__browser_take_screenshot",
                     "mcp__a__call_service", "mcp__a__restart_ha", "mcp__a__set_x",
                     "mcp__github__mark_all_notifications_read", "mcp__gmail__mark_as_read", "mcp__pg__query",
                     "mcp__fs__search_and_replace", "mcp__x__get_or_insert", "mcp__x__list_and_purge",
                     "mcp__x__get_fileAndDelete"):
            self.assertFalse(shellguard.mcp_read_allowed(tool), tool)


class PowerShell(unittest.TestCase):
    def test_powershell_writes(self):
        for cmd in ("Set-Content f.txt x", "Remove-Item f", "git commit -m x", "echo hi > out.txt",
                    "[System.IO.File]::WriteAllText('f','x')", "Out-File f"):
            self.assertTrue(shellguard.powershell_writes(cmd), cmd)
        for cmd in ("Get-Content f", "git log", "Get-ChildItem", ""):
            self.assertIsNone(shellguard.powershell_writes(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
