# Timing audit

Base: origin/main 0646bf6. Scope: tests only; test_autoinit.py and process-reference-race cleanup are owned by the Postgres engineer. No product changes or changelog entry.

The oracle is a clock value injected at the relevant boundary, a completed operation while a gate stays closed, observed lock contention, or exact stored data. 30–120 second waits are deadlock/fixture guards. Remaining sleeps poll explicit conditions, drive a stub wait_for_change, or simulate a deliberately live child; they do not establish an assumed ordering.

## Changed tests

### test_auto_close.py

Prove touch completion while locks remain held; record a single deadline for all markers using a fake clock.

- `AutoCloseContract.test_concurrent_sweeps_close_once`
- `AutoCloseCliTests.test_a_touch_during_a_slow_close_reverts_the_close`
- `AutoCloseCliTests.test_touches_of_several_busy_markers_share_one_deadline`

### test_board_contract.py

Use timestamps bounded by operation observations, exact stored timestamps, an advisory-lock wait condition, and a bounded notification worker.

- `BoardContract.test_now_is_timezone_aware`
- `BoardContract.test_post_updates_poster_last_post`
- `BoardContract.test_sync_state_defaults_and_records`
- `BoardContract.test_wait_for_change_after_post`
- `BoardContract.test_verdict_only_from_the_active_judge`
- `BoardContract.test_waiting_is_set_cleared_and_reset_by_open_and_close`
- `BoardContract.test_transcript_save_list_and_body`
- `BoardContract.test_transcript_totals`
- `PostgresSpecificTests.test_ids_become_visible_in_order`

### test_bootstrap.py

Use a generous FIFO deadlock guard.

- `NoticeAndStampSafetyTests.test_take_notices_reads_through_no_link_and_no_fifo`

### test_expiry.py

Bracket the constructed wait deadline with operation observations; compare queued deadlines as UTC datetimes to respect stored microsecond precision.

- `ExpiryCliTests.test_wait_for_bounds_the_wait`
- `ExpiryCliTests.test_a_queued_bounded_wait_is_delivered_with_its_bound`

### test_file_board.py

Wait for each writer’s first committed post before killing it; readers drain through an explicit writers_done event. Per-post progress bounds inactivity without a total workload cutoff. Join/terminate workers on failures.

- `FileBoardProcessTests.test_many_writers_unique_increasing_ids_and_readers_never_skip`
- `FileBoardProcessTests.test_killed_writers_never_leave_a_corrupt_board`
- `FileBoardChangeDetectionTests.test_a_post_from_another_process_wakes_tail_and_watch`
- `FileBoardChangeDetectionTests.test_an_agent_change_wakes_watch_but_not_tail`

### test_final_retry.py

Inject elapsed budget consumption at redaction/capture/open boundaries, including small-budget supervisor retries. Advance the fake clock during lock-poll sleeps. Preserve fairness, retries, secrets and failure-state assertions; use structural guards for held locks.

- `CaptureJobIsolationTests.test_a_slow_agent_does_not_cost_the_others_their_final`
- `CodexOrderingTests.test_codex_finals_no_longer_starve_behind_failing_ones`
- `BudgetTests.test_sweeps_are_never_unbounded`
- `BudgetTests.test_the_pass_bounds_its_sweep_and_gives_the_retries_what_is_left`
- `LockTests.test_a_held_lock_blocks_no_sweep_hook_or_pass`
- `OrderingTests.test_the_pass_takes_slow_finals_of_both_harnesses_oldest_first`
- `PassClockTests.test_the_pass_budget_counts_from_before_the_board_is_opened`
- `ZstdBombTests.test_zstd_tool_fallback_is_capped_and_timed`

### test_hardening.py

Inject clocks for flush budgets and operation timeouts; gate stalled resolvers/connects; wait for actual retain completion and actual lock contention.

- `SpoolFlushBoundsTests.test_per_tool_hooks_deliver_a_couple_the_agent_hooks_the_backlog`
- `SpoolFlushBoundsTests.test_deadline_stops_a_slow_flush`
- `SpoolFlushBoundsTests.test_a_blocking_delivery_is_cut_to_the_budget`
- `SpoolFlushBoundsTests.test_a_memory_delivery_stays_within_the_budget_over_several_calls`
- `SpoolFlushBoundsTests.test_a_stalled_resolver_is_cut_off_within_the_budget`
- `SpoolFlushBoundsTests.test_proxy_path_with_a_stalled_resolver_is_cut_off`
- `SpoolFlushBoundsTests.test_slow_connect_after_fast_resolution_is_cut_off`
- `SpoolFlushBoundsTests.test_a_resent_memory_keeps_its_document_id`
- `SpoolFlushBoundsTests.test_every_board_call_of_a_delivery_shares_the_budget`
- `SpoolFlushBoundsTests.test_board_op_timeout_bounds_a_wait_on_the_store`
- `MarkerClaimTests.test_a_held_lock_does_not_block_past_the_deadline`
- `MarkerClaimTests.test_a_lock_released_in_time_is_taken`
- `MarkerClaimTests.test_a_marker_replaced_while_waiting_is_read_again`
- `MarkerRemovalTests.test_removal_during_a_claim_wins`
- `MarkerRemovalTests.test_claim_during_a_removal_finds_nothing`
- `MarkerRemovalTests.test_removal_waits_for_a_short_lock_and_skips_a_missing_marker`
- `MarkerRemovalTests.test_removal_never_unlinks_without_the_lock`
- `DeactivateMarkerLockTests.test_deactivate_waits_for_a_claim_in_flight`
- `BoundedRecallTests.test_a_stalled_resolver_during_a_per_tool_recall_is_cut_off`

### test_hindsight.py

Inject recall budget consumption and inspect transport budget arguments; gate real late recalls and assign queue mtimes directly.

- `MemoryTests.test_hindsight_down_start_is_quick_and_logged`
- `MemoryTests.test_slow_hindsight_is_cut_off_by_the_recall_budget`
- `ColdRecallTests.test_start_recall_waits_for_a_slow_cold_recall`
- `ColdRecallTests.test_start_recall_that_runs_out_of_time_is_retried_on_the_next_turn`
- `SpoolRetryTests.test_a_failing_bank_does_not_block_memories_for_healthy_banks`

### test_hooks_cli.py

Bound concurrency barriers and FIFO guards; set activation mtime explicitly instead of sleeping.

- `SpoolTests.test_concurrent_flushers_deliver_exactly_once`
- `EnrolmentTests.test_orchestrator_hook_writes_the_job_record_once_per_activation`

### test_launchers.py

Prove detached launch with a bootstrap release file; poll completion; inspect stamped launcher execution instead of sleeping.

- `LauncherTests.test_hook_without_venv_exits_fast_and_silently`
- `LauncherTests.test_session_start_spawns_bootstrap_detached`
- `LauncherTests.test_session_start_skips_when_stamped`

### test_migrate.py

Use a generous FIFO deadlock guard.

- `MoveOldDefaultsTests.test_old_spool_links_and_fifos_not_followed`

### test_provenance_excerpt.py

Retain size/content/refusal assertions under a generous completion guard.

- `ExcerptTests.test_read_tail_reads_only_the_tail`
- `ExcerptFixTests.test_read_tail_refuses_a_fifo_without_blocking`

### test_provenance_hooks.py

Keep cancellation and one-request/one-log assertions under structural guards; inject redaction budget exhaustion and hold the clock fixed for hanging-server setup.

- `ProvenanceHookTests.test_the_20_id_cap_holds_through_the_hook`
- `ProvenanceHookTests.test_hook_budget_on_huge_transcript`
- `ProvenanceHookTests.test_a_slow_excerpt_stops_at_the_deadline`
- `PatchTests.test_hindsight_down_leaves_the_ref_unpatched`
- `HangingPatchTests.test_one_line_and_within_budget_when_hindsight_hangs`

### test_provenance_purge.py

Hold document responses behind release events and assert deadline cancellation while they remain held.

- `PurgeTests.test_prune_client_keeps_the_deadline`
- `PurgeTests.test_statuses_client_keeps_the_deadline`

### test_redact_budget.py

Run original adversarial shapes with a deterministic advancing clock and require deadline exceptions; retain anchor/fallback content checks with fixed clocks.

- `RedactDeadlineTest.test_redact_stops_near_its_deadline`
- `RedactDeadlineTest.test_one_huge_line_is_checked_too`
- `WalkerBudgetTest.test_begin_header_lines_finish_or_stop_near_the_deadline`
- `WalkerBudgetTest.test_key_lines_across_json_strings_stop_near_the_deadline`
- `WalkerBudgetTest.test_anchor_on_64_kb_of_them_keeps_its_budget`
- `CaptureBudgetTest.test_capture_respects_the_hook_budget`
- `ExcerptBudgetTest.test_excerpt_degrades_to_a_smaller_one_within_budget`
- `ExcerptBudgetTest.test_a_small_excerpt_is_not_degraded`

### test_safefs.py

Check that touch updates the deliberately old timestamp rather than comparing with a later wall-clock observation.

- `OperationTests.test_touch`

### test_spool.py

Use generous FIFO guards and a bounded condition poll for cross-process startup.

- Shared fixture/helper changed; its test callers retain their behavioral assertions.

### test_sqlite.py

Signal posts without a guessed delay; readers drain all pages after writer completion. Use exact parsed stored timestamps and shared process cleanup deadlines.

- `SqliteProcessConcurrencyTests.test_concurrent_posts_have_unique_ordered_ids_and_readers_skip_nothing`
- `SqliteBackendTests.test_timestamps_are_stored_utc_and_returned_aware`
- `SqliteBackendTests.test_wait_for_change_sees_a_post_from_another_process`

### test_stall.py

Use structural completion/exception checks for stalled queries; assert a failed connection executes no further query; generously bound recovery polling.

- `PostgresDeadlineTests.test_after_a_stall_the_board_fails_fast`
- `PostgresDeadlineTests.test_wait_for_change_is_bounded`

### test_supervise_markers.py

Synchronize create/remove through observed lock contention; bound FIFO worker completion generously.

- `ResumeMarkerTests.test_remove_racing_a_create_never_leaves_a_marker`

### test_supervise_runner.py

Enroll at the injected process launch boundary; inject timeout clocks; wait for real PID/token/scope conditions; gate token completion and expire orphan records explicitly.

- `ProjectConfigTests.test_config_that_appears_before_the_launch_is_held_until_approved`
- `ProjectConfigTests.test_a_hold_not_approved_in_time_is_refused`
- `ProjectConfigTests.test_a_hold_ends_when_the_job_closes`
- `RunnerTests.test_completed_claude`
- `RunnerTests.test_timeout_kills_process_group`
- `RunnerTests.test_max_turns`
- `RunnerTests.test_codex_binds_thread_id`
- `RunnerTests.test_closed_by_sweep_meanwhile_is_stuck`
- `RunnerTests.test_reap_enforces_wall_clock_of_orphaned_child`
- `RunnerTests.test_big_brief_to_a_child_that_never_reads_still_times_out`
- `RunnerTests.test_codex_gets_a_token_its_hooks_can_bind_by`
- `SurvivorFixtureTests.test_survivor_ends_by_itself_when_its_test_never_cleans_up`
- `RunnerFixTests.test_a_started_run_is_owned_before_its_run_file_exists_and_until_its_runner_ends`
- `RunnerFixTests.test_reap_stops_group_members_after_the_leader_exited`
- `RunnerFixTests.test_leftovers_of_a_completed_session_are_stopped`
- `RunnerFixTests.test_reap_kills_an_orphaned_group_member_that_ignores_sigterm`
- `ReusedGroupTests.test_member_without_the_runs_tag_is_never_signalled`
- `ReusedGroupTests.test_member_with_another_runs_tag_is_never_signalled`
- `ReusedGroupTests.test_member_older_than_the_session_is_never_signalled`
- `ReusedGroupTests.test_a_run_recorded_without_a_tag_signals_nothing`
- `ScopeTests.test_a_session_runs_in_its_own_scope`
- `ScopeTests.test_reap_stops_a_dead_runners_scope_by_its_recorded_unit`
- `ScopeTests.test_unseen_scope_within_the_launch_grace_is_left_alone_then_appears_and_is_stopped`
- `ScopeTests.test_the_runner_records_the_scope_seen_active`
- `PidfdFallbackTests.test_member_signals_go_through_a_checked_pidfd`
- `PidfdFallbackTests.test_no_pidfd_support_signals_nothing`

### test_transcript_fixes.py

Compare the protected victim’s unchanged timestamp exactly with the timestamp planted by the test.

- `HostFileSafetyTest.test_snapshot_stamp_symlink_not_followed`

### test_transcripts_capture.py

Verify a snapshot updates the explicitly old stamp, with no wall-clock freshness threshold.

- `HookCaptureTests.test_snapshot_round_from_the_start_sweep_when_due`

### test_transcripts_codex.py

Backdate the stored row without touching the transcript file, then verify stop refreshes it.

- `CodexCaptureTests.test_stop_hook_on_unchanged_untouched_file_refreshes_captured_at`

### test_watch_keys.py

Wait for query entry before typing; assert render/quit completes while the gate remains held; synchronize loop tests on frame/error readiness.

- `PtyWatch.test_key_shifts_the_view_at_once_while_a_query_is_in_flight`
- `PtyWatch.test_quit_does_not_wait_for_a_query_in_flight`

## Message-cap branch patch

`msglen-race.patch` applies to origin/feat/configurable-message-length. The poster sets first_post after a successful post; the test waits up to 60 seconds before changing caps. Cleanup retains stop/join and now asserts the worker exited. No change was pushed to that branch.

## Validation

Final focused validation ran under the shared four-process busy-loop load:

- Follow-up: 20 tests passed in 455.102 seconds, including the real file/SQLite many-writer readers, killed writers, replacement scope/runner cases, expiry precision, concurrent flushers, and injected retry exhaustion.
- Final cleanup: 70 tests passed in 53.321 seconds, covering hanging-server cleanup, purge cancellation, excerpts, transcript safety, and changed timestamp contracts against memory/file/SQLite.
- Earlier 204-test batch completed with three replacement-related failures (scope observation and datetime rounding); all three were corrected and passed the follow-up. Earlier 242-test batch found one floating point equality issue, corrected with assertAlmostEqual; preliminary 77-test batch found missing helper imports, corrected. The fixed budget/redaction/known CI tests passed the 204-test batch.
- `python3 -m compileall -q tests` and `git diff --check` passed. The supplied message-cap patch passes `git apply --check` against the exact branch file.

No full-suite stress runs were performed by this branch. Finder and integration own the repeated per-backend matrix. The Postgres-only advisory-lock/stall checks require the integration scratch cluster and were not executed here. No scratch Postgres instance was started by this engineer.

The baseline also reproduced the file-reader 60-second cutoff: r0 read 256 of 1000 committed IDs. The completion-event protocol removes that workload cutoff.

No tests are deleted or skipped by these changes. Existing platform-specific skips remain.

## Shared fixture callers

The method list above identifies direct test edits. Shared fixtures also affect unchanged caller methods: `HookOutOfTimeTests`, `StopFirstTests`, `LockHeldForeverTests`, and retry tests use deterministic failed-capture setup; `LoopTests` use frame/error readiness; the contract mixin runs against each concrete backend. Process helper changes apply to all `FileBoardProcessTests` and `SqliteProcessConcurrencyTests`. No fixture reduces a result-count, secret-removal, deadline-sharing, ordering, or failure-state oracle.

## Audit disposition

Searched all of `tests/` for time.time/monotonic/perf_counter, duration assertions, sleeps, joins, waits, barriers, and timers. Remaining waits have generous fixture/deadlock bounds; remaining sleeps poll actual conditions, simulate wait_for_change, or await eventual consistency in the opt-in live Hindsight smoke test. Constants compared with configured hook/systemd budgets are configuration invariants. `test_autoinit.py` and `test_memory_refs_race.py` are assigned to the Postgres engineer. Its contract barrier cleanup overlaps this branch only in the shared `test_board_contract.py`; integration must preserve both edits.

A late finder baseline also reproduced an implicit timing assumption in the per-tool/agent backlog test (9 queued instead of1 on file IO). Its exact item-cap oracle now runs under a fixed clock; separate deadline tests cover exhaustion. This final edit was compiled/reviewed but could not be executed here because all three test slots had transferred to baseline; integration must run it against memory/file/SQLite.
