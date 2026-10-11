"""Drive-seat command refusals (spec 3.4, 4.0, 4.4): target pushes in every recognisable spelling, force pushes,
headless sessions, merges through gh, and the shell-aware splitting that finds them inside sh -c, bash -lc, eval,
script -c, $(...) and backticks. A guardrail, not a boundary: what decides an outcome is `swarm land` and the
driver's mirror audit."""
from __future__ import annotations

import shlex
import unittest

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
                    "git push --repo=origin -o main feat/a", "git push -o main origin feat/a"):
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
        self.assertIsNone(refuse("python3 - <<'EOF'\nprint(1)\nEOF", ["main"]))

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
                    "python3 -c 'print(1)'", "python3 -c \"print('mainline')\"", "python3 -m unittest x", "node app.js",
                    "python3 - <<'EOF'\nprint('git push origin feat/a')\nEOF", "perl -e 'print 1'"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refuse(cmd, ["main"], branch_of=on("feat/a")), cmd)

    def test_quoting_and_assignments(self):
        self.assertTrue(refuse("GIT_DIR=x git push origin main", ["main"]))
        self.assertTrue(refuse("git push 'origin' \"main\"", ["main"]))
        self.assertTrue(refuse("git\tpush\torigin\tmain", ["main"]))
        self.assertTrue(refuse("git push origin ma\\in", ["main"]))
        self.assertTrue(refuse("git push \\\norigin main", ["main"]))


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
                     "mcp__a__count_x", "mcp__a__describe_x", "mcp__a__query_x", "mcp__a__fetch_x", "mcp__a__view_x",
                     "mcp__a__x_get_y", "mcp__a__browser_snapshot", "mcp__a__unifi_tool_index"):
            self.assertTrue(shellguard.mcp_read_allowed(tool), tool)
        for tool in ("mcp__a__create_x", "mcp__a__x", "mcp__a__execute", "mcp__a__get_and_update", "mcp__a__list_and_send",
                     "mcp__a__render_template", "mcp__a__browser_click", "mcp__a__browser_take_screenshot",
                     "mcp__a__call_service", "mcp__a__restart_ha", "mcp__a__set_x"):
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
