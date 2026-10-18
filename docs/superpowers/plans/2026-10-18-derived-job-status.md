# Derived job status implementation plan

> Execute inline with systematic debugging and TDD; parent supplies independent review.

Goal: A job with live agents is active; agents cannot invoke lifecycle status setters.
Spec: Francesco's 2026-10-18 instructions: running agents exclude waiting; Swarm alone derives status.
Architecture: Keep status derivation in board/base.py and align Postgres shown_status. Guard lifecycle CLI entry points before writes and enrolled-agent shell hooks independently of shell environment. Preserve owner terminal commands and internal fact-driven lifecycle automation.
Constraints: Base github/main 8fa3983. No deployment, pushes, live job mutations or plate-solve process changes. No memory files. No broad lifecycle rewrite.

1. Add failing regressions in test_waiting.py, test_blockers.py and test_watch_snapshot.py for started/running plus open questions, closed status precedence, and SQL/backend parity. Run them before implementation.
2. Add failing CLI/hook regressions for activate/deactivate/pause/resume/wait from agent sessions, including environment-clearing wrappers. Assert refusal and unchanged real job/blocker state. Owner commands remain usable.
3. In base.py test started/running before open_questions; align Postgres CASE (only questions cause waiting without live agents). CLI and watch consume the shared derivation.
4. Refuse agent lifecycle commands at CLI dispatch before initialization and hooks before execution, using existing HARNESS_SESSION_VARS and shellguard.iter_commands. Keep internal transition methods available to Swarm.
5. Run focused tests across memory/sqlite/file and Postgres if an isolated test database is available. Run repository-directed targeted checks locally; full suite belongs in CI per README. Record any unavailable checks and review lifecycle paths outside this patch.
6. Commit the change locally and hand SHA, diff and test evidence to parent for independent review. Do not deploy.
