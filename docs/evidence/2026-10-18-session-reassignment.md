# Existing job session reassignment

The native command `swarm session --job JOB --session SESSION` changes an existing job's coordinator binding without calling activation, reopening the job, or clearing a verdict. Unknown jobs fail. A compare-and-set refuses a concurrent board binding change.

`--no-routing` reassigns pane and existing local coordinator metadata while leaving every hook marker untouched. This supports several jobs in one session watch without making Codex child routing ambiguous. If another job already routes the target session, the default routing operation refuses before any change and points to this option.

`swarm session --job JOB --clear` clears only the pane binding. Existing local ownership records, worker sessions and hook routing remain unchanged. It can remove an unrelated job from a shared pane without cancelling work.

The command changes only the `session_id` field of existing host-private coordinator job and owner records. It preserves the owner record's harness, creation timestamp, working directory, roots, remote pins, credentials, requirements and landing target. Missing local owner proof is reported; no local owner proof or execution capability is created. Corrupt or unsafe local coordinator proof refuses the operation before the board change. A known enrolled worker's session cannot run this command or become its target; a user-authorized root session can. Existing launch and landing owner gates remain in force.

Default routing changes only the primary non-attached, non-resume job marker. Its former session may differ from the pane binding, allowing a routing update after an earlier binding-only operation. Worker and child attachments remain bound to their original sessions. `--no-routing` makes this distinction explicit when only pane/coordinator metadata should move. Remote-host markers and owner records cannot be updated by a local command; it reports the local owner absence and never invents those capabilities.

Board and host files do not share a crash-atomic transaction. Ordinary local write failures compensate the board and local records; an interrupted process may require repeating the explicit reassignment. Capability payloads and worker records are never reconstructed from board data.

## Before and after evidence

Before implementation, `python3 -m unittest discover -s tests -p test_session_reassignment.py` failed seven behavior assertions because `session` was not a native command (exit 2). The owner fixture was corrected to carry the required nonempty goal before recording that RED result.

The initial implementation passed 13 tests on each backend:

- memory: 13 passed;
- `SWARM_TEST_BACKEND=sqlite`: 13 passed;
- `SWARM_TEST_BACKEND=file`: 13 passed.

`python3 -m unittest discover -s tests -p test_watch_session.py`: 20 passed.

The deterministic compact-watch test starts three jobs under an old session, reassigns exactly two to a new session with `--no-routing`, asserts `session_shown_jobs` is exactly those two, renders a compact frame, and asserts that the unrelated job is absent. Other assertions preserve a closed job's complete status record except its session id, its accepted verdict, agents and message history; preserve worker proof and owner pins; reject an enrolled worker, invalid ids, corrupt ownership proof and routing collision; and reject stale compare-and-set requests.

No live session reassignment, deployment or lifecycle mutation was performed to obtain this evidence.

Independent review added regressions for read-only mutation refusal, worker target rejection, routing repair after a binding-only update, concurrent coordinator operations, stale compensation and a worker target known only to the board. The current focused module has 20 cases. Existing coordinator proof writers and session operations share one nonblocking host-private lock per host user; contention refuses with a retry message and avoids board/proof lock inversion. Compensation restores old metadata only if the board compare-and-set back to the old binding succeeds. The hook separately checks trusted event payload identity, so shell environment wrappers cannot erase an enrolled worker's identity.

Retention and explicit coordinator-proof deletion use the same lock. Retention defers a busy proof until the next sweep, and rereads under the lock so an updated proof cannot be pruned using a stale snapshot. The deletion race regression passed after its initial RED failure.

Final session verification: 20 passed on each of memory, SQLite and file; 24 existing enrolment tests, 18 read-only tests and 20 session-watch tests passed. The preceding 19-case version passed against a throwaway PostgreSQL database; the final added case exercises only host-file retention locking. PostgreSQL CLI tests need the configured test credential loaded into the test process environment before the Env harness replaces HOME; an initial run without that preparation failed authentication, then the prepared run passed all 19. Independent review passed the combined 47 session/hook/enrolment cases and three real leased-shell wrapper-denial checks.
