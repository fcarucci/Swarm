# E3 recovery review — custom roles restored onto e3cd14b

- Reviewer: Snake (fresh Claude reviewer, Opus 5.5; did not author the patch)
- Candidate: `3d9e560` (parent `e3cd14b`), branch `fix/restore-custom-roles`; original `634aece`
- Author: Herman Hermann · PM: Rabbi Hyman Krustofsky
- **Verdict: CHANGES_REQUIRED** (1 Medium, 3 Low; no High)

## Verification basis

1. **Snapshot identity.** No `.git` exists in the snapshot, so I could not check the hash `3d9e560`
   directly. `patch -p1 -R --dry-run < .review/recovery.patch` reverses cleanly on all 21 files
   with no offset or fuzz, so the tree holds the post-patch content. The commit hash itself is
   taken from the author's report.
2. **Patch equivalence.** With `index`/`@@` lines and the commit header removed,
   `recovery.patch` and `original-custom-roles.patch` are byte-identical. The recovery is a
   verbatim reapply of `634aece` with only line offsets changed. So every finding below is about
   upstream code that `634aece` never saw and that the reapply did not adapt.
3. **Focused offline tests.** From `tests/`, I ran `python3 -m unittest test_roles test_models test_board_contract
   test_routing test_spawn test_codex_hooks test_supervise_launch test_respawn
   test_board_read_only test_file_board`: **781 ran, OK, 152 skipped**. The skips are Postgres,
   which needs `SWARM_TEST_CONFIG`. The new contract test passes on Memory, SQLite and **File**.
   It was **not run on Postgres**, so that is unverified: the SQL reads correctly, but it was not executed.
   I ran no installer, e2e, Docker or Hermes tests.
4. I read these runtime paths: `roles.py`, `hooks.py` (`_role`, `_verify_route`, `_spawn_refusal`,
   `_gate_spawn`, `_orchestrator_spawn`, `_enrol`, the resume welcome, `_unchecked_verifier_write`),
   `hosts/codex.py` (`role_hint`/`spawn_role_hint` via the last `agent_path` segment),
   `models.py`, `supervisor/launch.py` + `supervisor/command.py` (restart), `board/{base,memory,
   file,sqlite,postgres}.py` (`set_agent_role`, `claim_resume`, `WRITE_METHODS`) and `respawn.py`.

## What is correct (preserved upstream behavior)

- **Default file backend.** `FileBoard(MemoryBoard)` inherits `set_agent_role`. Its
  `with s.lock` is the FileStore `_Transaction`, which persists across processes. The contract
  test passes on the file backend.
- **Schema.** `SCHEMA_VERSION = 10` is unchanged, and the change needs no migration: `agents.role` is
  free text in SQLite and Postgres. The only `CHECK (role IN ...)` constraints are on the transcripts table.
- **Same-role supervisor restart.** `claim_resume` copies `old["role"]`, `judge` and `verifier`
  (memory.py:672-681). `replacement_model(cfg, harness, a.role, ...)` then picks the custom role's
  `[models]` entry. `agents()` projects the judge/verifier flags over the label (memory.py:777),
  so a judge still restarts on the judge model.
- **Reserved roles.** `custom_role()` rejects judge and verifier. `set_agent_role` raises on them
  in all three backends. The spawn gate refuses a `judge` from either the prompt or the Codex task
  name, including `judge__x` (test_codex_hooks). The Claude spawn gate and child enrolment use the
  same parser, `roles.from_prompt`, so the gate and the claim agree.
- **Codex naming.** Explicit `role__task` is parsed before the legacy `verifier*`/`judge*`
  prefixes, the same way at spawn and at rollout (`agent_path` last segment).
- **Caps.** Custom-role children go through the same `reserve_spawn` per-agent and per-job caps
  and the same depth limits (tests assert "limit 2 per agent" and "depth" denials).
- **Examples and CI.** Examples are public-only. `.github/` is untouched.

## Findings

### R1 — Medium — respawn brief for Codex contradicts the restored grammar
`lib/swarm/respawn.py:74`: `"Give the new judge a task name starting with `judge`."`

`respawn.py` is newer than `634aece`, and its guidance was written for the prefix-only rule.
Under the restored `roles.from_task_name`, a name that starts with `judge` but contains `__`
is a **custom worker role**. Verified directly:
`judge2__final -> judge2`, `judge_round2__final -> judge_round2`, `judge__ -> None`.

The docs now teach `<role>__<task>`, so a Codex orchestrator that follows this brief can
reasonably write `judge2__verdict` or `judge_r2__final`. The next-round agent then enrols as an
ordinary worker, nobody takes the judge seat, and `deactivate --status completed` stays gated
until someone intervenes. This is the not_met → next-round path the brief asked about.

**Fix:** say "a task name `judge__<task>` (e.g. `judge__round2`)" and add a `test_respawn` assertion on the Codex text.

### R2 — Low — `set_agent_role` missing from the read-only write list
`lib/swarm/board/base.py:795-804` (`WRITE_METHODS`).

The patch adds a mutating abstract method (base.py `set_agent_role`) but does not register it.
`refuse_writes` therefore does not wrap it on `open_read_only` boards. The backends still refuse
at commit (file `_check_unchanged`, SQLite `mode=ro`), so no data is written. But the documented
contract ("every Board method that can change the store") is broken, and the error type and
timing differ. `tests/test_board_read_only.py:198` only checks the reverse direction, so this
went unnoticed. No hook opens a read-only board today.

**Fix:** add `"set_agent_role"` to `WRITE_METHODS`, and ideally a test that every
mutating abstract method is listed.

### R3 — Low — stale Codex role docs outside the patched hunks
- `docs/REFERENCE.md:885-886` still says "a task name starting with `verifier` or `judge` sets the role".
- `docs/REFERENCE.md:1291` still says "(starting with `verifier`)".

Both contradict the new `Custom roles` section in the same file, where `judge_assistant__research` is a worker.

**Fix:** align them with the `<role>__<task>` wording.

### R4 — Low / informational — carried over from `634aece`, not introduced by the recovery
- `lib/swarm/supervisor/launch.py:78` now accepts **any** valid identifier as the role. For an
  untagged Claude agent, the role column holds its `agent_type`, for example a custom agent
  type called `engineer`. The first spawn uses `models.role_of` → `worker`, but a supervisor
  restart would use `[models.claude] engineer`. So an agent can come back on a different model
  from the one it started on. This only happens when an agent_type name matches a `[models]`
  key, which is an edge case.
- `lib/swarm/roles.py:31-33`: a legacy-style name that contains `__` but has an invalid role
  part, such as `Verifier__check` or `verifier-api__x`, used to resolve to a read-only verifier.
  It now resolves to `None`, which means an unrestricted worker. This is documented ("explicit form
  first; malformed selects no custom role") and was accepted at `634aece`. I list it so the PM
  can see that the behavior changed. No fix is required.

## Test quality

The new tests check behavior someone outside the code would see: hook `updatedInput.model`,
deny decisions and reasons, the `status` CLI output, the `verdict` exit code and roster roles
after a stop/resume. `test_custom_role_on_goal_job_cannot_verdict_but_can_write` only checks
`rc != 0`, not the refusal reason. That is weak but acceptable. No test covers the Codex
respawn wording (R1).

## Recheck

When the author has fixed R1 (required) and R2/R3 (requested), the same reviewer rechecks the
new candidate commit.

---

## Recheck 1 — candidate `62460c5ed9f65005e0c1587d55bc7edefefc9f80` (parent `3d9e560`, base `e3cd14b`)

The initial verdict above (CHANGES_REQUIRED on `3d9e560`) stays on record. This section is the recheck.

**Recheck verdict: READY**

### Verification basis
- `sha256sum -c .review/source.sha256`: all 212 tracked-file hashes match the snapshot (manifest
  sha256 `d0515fc902771e06a45286919b9bbcaffc4e50661eabadaf0317666990343c62`).
- `patch -p1 -R --dry-run` reverses cleanly with no fuzz for both patches:
  - `.review/review-fixes.patch`, `3d9e560`→`62460c5` (sha256 `92de9290068207285ea2bd89b912ffdb59910df536da2010985ef772a4f213ea`);
  - `.review/recovery.patch`, `e3cd14b`→`62460c5` (sha256 `05c66ec6677d168a8dbbcbeb07142a30d4e5d2ad748eb741b048d7a537a8c8df`).

  There is no `.git` in the snapshot, so the commit id itself is the author's and PM's value; the file hashes are what I verified.
- Affected tests only: `python3 -m unittest test_respawn test_board_read_only test_roles` → **40 ran, OK**.
- `roles.from_task_name("judge__round2") == "judge"`, checked directly, so the new guidance produces a real judge.
- A grep of lib, skills, docs, README and config.example finds no remaining "starting with `judge`/`verifier`" wording.

### Findings
- **R1 (Medium): RESOLVED.** `lib/swarm/respawn.py:74-76` now says ``task name `judge__<task>` (for example, `judge__round2`; the task suffix must be nonempty)``.
  - `tests/test_respawn.py` pins this wording in the unit `BriefTests`.
  - It also pins it in the Codex hook-context test, which replaced the old assertion.
- **R2 (Low): RESOLVED.** `set_agent_role` is now in `WRITE_METHODS` (`lib/swarm/board/base.py:799`).
  - `tests/test_board_read_only.py` asserts that a read-only board raises `ReadOnlyBoard` with the text "set_agent_role refused". Without the fix this call would still raise `ReadOnlyBoard`, but at commit time and with a different message, so the new assertion is a real regression check.
  - The same test also asserts the tree is unchanged on disk.
- **R3 (Low): RESOLVED.** `docs/REFERENCE.md:885-887` and `:1291-1293` now describe `<role>__<task>`, with legacy prefixes applying only when the name has no `__`.
- **R4 (informational): ACCEPTED as-is.** This is inherited `634aece` behavior. Not expanding scope is the right call.

### Residual unknowns (not blocking, not claimed as success)
- The Postgres `set_agent_role` path was not executed: no throwaway `SWARM_TEST_CONFIG` was available.
- I did not rerun the broad suite; I relied on my first pass (781 OK) plus this affected-module run.
