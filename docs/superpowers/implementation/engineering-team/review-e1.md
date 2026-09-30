# Independent review: Task E1 (engineering-team role/artifact contract)

**Reviewer:** Rayshelle Peyton (Swarm job `engineering-team-impl-20260929`, key `engineering-impl-review-e1`, role `reviewer`), fresh Claude context, not an author of E1.
**Scope:** `skills/engineering-team/SKILL.md`, `references/team-roles.md`, `references/artifacts.md` as delivered in this snapshot (new files; base `b9f8657` had none of them).
**Checked against:** original request map U1–U7 (`docs/superpowers/reviews/2026-09-29-engineering-team/initial-product-roles.md`), `product-baseline.md` (`product-r1`, PR-1..PR-10), approved design (`specs/2026-09-29-engineering-team-design.md`), approved plan Task 1 (`plans/2026-09-29-engineering-team.md`), and ER-1..ER-3 in `engineering-brief.md`. Existing Swarm semantics come from `skills/swarm/SKILL.md`.
**Method:** static document review only. I read the files directly. I did not rely on the author's `DONE` post. None of this is evidence of how either host behaves.

## Decision: CHANGES REQUIRED

One high and three medium findings affect ER-1 (stage order) and ER-3 (candidate identity and requirement history). The remaining findings are low. Most of the contract is sound:
- tier table;
- author, reviewer, QA and product separation;
- finding-disposition authority;
- scope escalation;
- evidence kept apart from the manifest;
- stale-verdict comparison;
- writer fencing.

## Static checks

- Frontmatter: `name: engineering-team` and the description are present and parse as simple YAML.
- Links: `references/hosts.md`, `references/team-roles.md`, `references/artifacts.md` from SKILL.md, and `artifacts.md` from team-roles.md all resolve. `hosts.md` exists in this snapshot as E2 work in progress. I did not review it except to confirm how it relates to E1.
- Whitespace: none of the three files has trailing whitespace.

## Findings

### E1-R1 — High — The entrypoint runs final QA and acceptance before the candidate is frozen (ER-1 order, PR-8)
**Evidence:**
- `SKILL.md` step 5 (line 18) has QA run checks, the product manager "directly check the delivered behavior against each criterion", and EL check ERs.
- Only step 6 (line 19) has "EL freezes an integrated candidate", followed by "Compare its ID ... with each final product, EL, and QA result".

This contradicts the references and the design:
- `artifacts.md:21`: "EL integrates reviewed tasks before final QA".
- `team-roles.md:12`: QA "sign off only on frozen candidate".
- `team-roles.md:34`: product "names the frozen candidate".
- Design D:53 and D:62, and the brief's dependency step 3 before step 4.

SKILL.md also defers reading `artifacts.md` until "before freezing a candidate", which by its own order happens after acceptance.

**Failure scenario:** a PM following the entrypoint runs the product, EL, and QA acceptances on an unfrozen integration. They can name no `candidate=<id>`. The freeze afterwards produces an ID that no acceptance references, so every final gate has to rerun, or the PM accepts results that are not bound to a candidate.

**Smallest correction:** open step 5 with "EL integrates reviewed tasks and freezes the candidate using the [source manifest](references/artifacts.md); final QA, product, and EL checks run on that candidate and name its ID and requirement revision." Step 6 then keeps only the comparison, the judge `candidate=<id> req=<revision>` check, and the report. QA may still write tests earlier, as design D:60 allows.

### E1-R2 — Medium — The candidate-ID serialization is not canonical, so two agents cannot reproduce the same ID (ER-3, PR-8)
**Evidence:** `artifacts.md:23`: "record its relative path and SHA-256 file hash. Sort by relative path and SHA-256 hash that canonical manifest to obtain `candidate=<id>`". The file never defines the canonical manifest bytes:
- line format and field order;
- separator and final newline;
- path normalization (a `./` prefix, POSIX separators);
- sort collation;
- whether the ID is the full hex digest.

`hosts.md:240` has the PM "recompute/compare" the ID under these rules.

**Failure scenario:** EL builds `path<TAB>hash` lines while PM recomputes with `sha256sum` output (`hash  path`) or with a locale-dependent sort. The IDs differ for identical content. The PM then either reopens every gate for no reason, or stops recomputing and trusts the ID EL recorded, which defeats the staleness check.

**Smallest correction:** state the exact form. For example: "one line per file, `<64-hex sha256>␠␠<relative POSIX path without ./>\n` (`sha256sum` format), sorted bytewise by path (`LC_ALL=C`). `candidate` is the full lowercase SHA-256 of those bytes. Store the manifest file with the evidence records, outside the deliverable list." A one-line portable command example would also help.

### E1-R3 — Medium — Rehashing the frozen list cannot detect deliverable files added after the freeze (ER-3)
**Evidence:** `artifacts.md:23` says "An addition or deletion changes the path list and candidate". `artifacts.md:25` has PM compare the "current manifest ID". Nothing says how the current path list is derived at comparison time.

**Failure scenario:** after freeze, an engineer adds a new operative module or test file and does not edit the list. Recomputing over the recorded paths gives the old ID. PM and the judge accept a candidate that differs from what was reviewed. A deletion is caught, because a listed file is missing. An addition is not.

**Smallest correction:** add to `artifacts.md:21/25`: "EL records the deliverable roots or the base used to derive the list, e.g. `git diff --name-only <base>` plus untracked files under the deliverable roots. At every comparison PM re-derives the path list the same way and confirms it equals the recorded list before comparing hashes."

### E1-R4 — Medium — Requirement revisions do not preserve prior text or the approver (ER-3 "versioned PR/ER trace", PR-8)
**Evidence:** the product baseline row (`artifacts.md:10`) requires only "revision `product-rN`". The engineering brief row (`artifacts.md:11`) requires only "revision". Only the original request is preserved (`artifacts.md:9`).

The sources all require the history of each revision:
- Design D:51: "Changes to a requirement keep the earlier text or diff and note who approved the revision".
- Plan Task 1 Step 4: "Preserve request and approval history; version PR IDs/criteria and ER-to-PR map".
- `product-baseline.md` acceptance method: "earlier text or diff and its approver preserved".

**Failure scenario:** the product manager publishes `product-r2` and overwrites r1 in place. A fresh role or the final report can no longer show what changed, or whether the user approved a scope change via the PM. That undermines the scope authority in `team-roles.md:31`.

**Smallest correction:** add to the product baseline and engineering brief rows: "each new revision keeps the prior revision's text or diff, the reason, and the approver (the user via PM for any scope change); IDs are never reused."

### E1-R5 — Low — A PM-found unsupported inference has no return route (PR-2)
**Evidence:**
- `SKILL.md:15` says "Compare its first baseline to the original request yourself". `team-roles.md:30` says PM "records each consequential inference" and routes to the user only an ambiguity "a product manager can flag".
- Neither says what the PM does when its own comparison finds an unsupported or wrong inference or exclusion.
- Design D:58 has the PM return errors to the product manager. The PR-2 criterion reads: "PM returns the baseline for correction or routes a consequential ambiguity to the user before dependent work."

**Smallest correction:** in the `team-roles.md` Baseline bullet, add: "PM returns an unsupported inference or unapproved exclusion to the product manager for a corrected revision, or routes it to the user if it is consequential, before EL work depends on it."

### E1-R6 — Low — User-facing performance thresholds have no field in the product-owned baseline
**Evidence:**
- `team-roles.md:33` makes the product manager the owner of user-facing thresholds.
- `artifacts.md:11` puts "user-facing and technical performance thresholds with owners" only in the EL-owned engineering brief. The product baseline row (`artifacts.md:10`) has no threshold field.
- Plan Task 1 Step 4 requires a "predeclared performance threshold owner/value".

**Failure scenario:** a user-facing threshold changes without a new `product-rN`, or the product manager has to edit an EL-owned file. Either way, which requirement revision a QA measurement binds to becomes unclear.

**Smallest correction:** add "user-facing performance thresholds with value, or `none required` with reason" to the product baseline row. Keep technical thresholds with their value in the engineering brief.

### E1-R7 — Low — `artifacts.md` limits the first `BRIEF:` post to Codex
**Evidence:**
- `artifacts.md:17` says "On Codex, its first board post is `BRIEF: ...`".
- Plan Task 2 Step 2 says "Every bounded role invocation posts `BRIEF: <durable path> tasks: <IDs>` first".
- PR-7 lists the first `BRIEF:` post as a host instruction.
- The E2 draft uses it for the Claude preflight too (`hosts.md:32`, `hosts.md:112`).

A Claude role that reads only the artifact contract may skip the post that the role-enrolment preflight waits for.

**Smallest correction:** "Every role's first board post is `BRIEF: <path> tasks: <IDs>`; on Codex it is also the only recovery route because the spawn message is encrypted."

### E1-R8 — Low — The judge gate is only conditional; the design's recommendation for moderate/complex jobs is missing (ER-1)
**Evidence:**
- `SKILL.md:19` says "If a goal job has a judge ...".
- `team-roles.md:14` says "when a goal is active".
- Design D:36 lists the judge as "recommended for moderate and complex jobs".

ER-1 lists the judge gate as a stage. With the current text a PM can skip `--goal` on a moderate or complex job and has no prompt to record that choice.

**Smallest correction:** add to SKILL step 6 or the tier table: "For moderate and complex jobs, activate with `--goal` and schedule one root judge (recommended); record the reason if omitted."

## Checked and not raised

- **Tiny tier:** three contexts, and the author gives neither review nor product acceptance. Reviewer-written tests go to PM/EL or another engineer for review. Matches design D:19 and PR-6.
- **Finding authority:**
  - EL plus reviewer may disposition medium/low findings.
  - An unfixed high/critical finding needs the user's decision via PM with EL's recommendation.
  - A finding that changes scope goes to the user regardless of severity.
  - The author never selects the reviewer or dispositions its own finding.
  - The same reviewer rechecks the fix.

  Matches D:61 and PR-4 (`team-roles.md:10`, `:31–32`).
- **Custom roles:** described as workflow evidence, not code enforcement. A custom `reviewer` is not a verifier. Consistent with `skills/swarm/SKILL.md`.
- **Capacity:** the child-spawn cap is not described as a team cap, and there is no capacity probe (`SKILL.md:16`, `team-roles.md:26`).
- **Stale judge `met`:** covered by `artifacts.md:25` and `SKILL.md:19`. Freshness depends on E1-R2 and E1-R3 being fixed.
- **Delegated `project_manager` limits (PR-1):** absent from the E1 role table but stated in E2's `hosts.md:6`. Acceptable at the integrated level, so not raised.
- **Research dispositions:** `not applicable` with PM acknowledgment versus `not performed` with the blocker is covered (`artifacts.md:10`, `SKILL.md:21`).

## Recheck request

The author fixes E1-R1..R4, and ideally R5..R8, then posts the corrected diff against `b9f8657`. This reviewer rechecks the same IDs.

---

# Recheck, 2026-09-29 (same reviewer, Rayshelle Peyton)

**Snapshot checked:** I computed `sha256sum` on each file myself and all three match the expected values:
- `SKILL.md` `0af2f0a38a6565fdcbf4d7d7bdefba461321a186743eef4b9fb2ec35177f8448`
- `references/team-roles.md` `c972b5894c60f4002bd60f777487985a1c26ee885a30ad8405c78911fff6c9de`
- `references/artifacts.md` `f0fd704e9c6cb18c2f4e45594ea0fa73d13d988e39e2438215cf7f011ea57263`

I rechecked each finding against the file text, not the author's summary. None of the three files has trailing whitespace. The original findings above are kept unchanged.

## Recheck decision: CHANGES REQUIRED (one new medium finding, E1-R9; E1-R1..R8 resolved)

| ID | Result | Evidence in the current files |
|---|---|---|
| E1-R1 | **Resolved** | `SKILL.md:18` step 5: "EL integrates reviewed tasks and freezes the candidate ... **before final checks**". QA, product, and EL then check "that candidate", and "each final result names the candidate ID and requirement revision". A rejection requires "a new freeze". Step 6 (`:19`) recomputes and compares. `SKILL.md:10` now points to `artifacts.md` "before creating baselines". Consistent with `artifacts.md:21` and `team-roles.md:12,36`. |
| E1-R2 | **Resolved** | `artifacts.md:23` fixes the manifest bytes exactly: <ul><li>project-relative POSIX UTF-8 path with no `./` prefix;</li><li>raw-byte SHA-256 as a lowercase 64-hex digest;</li><li>sorted by unsigned path bytes, independent of locale;</li><li>each entry `path + NUL + hash + LF`, no header;</li><li>the ID is the full lowercase SHA-256 of those bytes.</li></ul>The manifest bytes and rules are stored as evidence outside the deliverable set. Symlinks and non-UTF-8 paths stop the freeze (`:21`). |
| E1-R3 | **Resolved** | `artifacts.md:23`: "At **every** freshness comparison, PM re-enumerates roots ... including untracked files; compares the newly derived path set with the frozen list ... Do not merely rehash the frozen list." `artifacts.md:27` adds the path set to the reopen trigger and to PM's comparison. |
| E1-R4 | **Resolved** | `artifacts.md:10`: each later product revision "preserves prior text or diff, reason, and approver (user via PM for a scope change); never reuse a PR ID". `artifacts.md:11` requires the same for ERs. |
| E1-R5 | **Resolved** | `SKILL.md:15` and `team-roles.md:32`: PM returns an unsupported inference or unapproved exclusion to the product manager for a corrected revision, or routes a consequential ambiguity to the user, before dependent EL work. |
| E1-R6 | **Resolved** | `artifacts.md:10` gives the product baseline a user-facing threshold value, or `none required` with a reason. `artifacts.md:11` gives the engineering brief a technical threshold with value or `none required`, reason, and owner. |
| E1-R7 | **Resolved** | `artifacts.md:17`: "On **both hosts**, each role's first board post is `BRIEF: <path> tasks: <IDs>`". It keeps the Codex recovery rationale. |
| E1-R8 | **Resolved** | `SKILL.md:14`: for moderate and complex jobs, activate with a goal and schedule one root judge as recommended, recording the reason if omitted. `team-roles.md:28` says the same and requires PM to check all the evidence itself when the judge is omitted. |

## New finding introduced by the E1-R3 fix

### E1-R9 — Medium — Enumerating the whole root can change the candidate when only evidence or VCS metadata changes (PR-8)
**Evidence:** `artifacts.md:21` says:
- "Prefer the whole project root";
- enumerate "regular files under those roots whether Git-tracked or untracked";
- record "exact evidence-only and generated-output exclusions".

`artifacts.md:25`: "a new excluded path requires EL and reviewer confirmation". `artifacts.md:23` makes PM re-enumerate at every comparison, and a changed path set invalidates the candidate (`:27`). No file excludes VCS metadata; `grep` finds no `.git` rule in `artifacts.md` or `hosts.md`. None of the three files says an exclusion may be a directory or prefix rather than a literal path.

**Failure scenarios:** all of these contradict `artifacts.md:25` ("Writing evidence alone must leave the candidate ID stable") and PR-8 ("changing evidence-only metadata does not" reopen gates).
1. EL freezes with the whole root and does not list `.git/`. Committing an evidence file, or any `git fetch`, rewrites `.git/` files, so the candidate ID changes and every final gate reopens.
2. The final QA, product, or EL acceptance record is a new file written after the freeze. For example, this job's own `review-e1.md` sits at the snapshot root. Unless its exact path was excluded in advance, re-enumeration sees a new path and invalidates the candidate. Each new evidence file then needs an EL-plus-reviewer exclusion confirmation, or it churns the gates.

The failure makes the check fail closed: it never accepts a stale candidate. But the evaluation's negative control ("an evidence-only metadata edit must not reopen them") would fail.

**Smallest correction:** add to `artifacts.md:21` or `:25`:
- "Always exclude VCS metadata such as `.git/`."
- "An exclusion may be a recorded literal path or directory prefix."
- "Before freeze, EL records one or more evidence directories. Review, test-result, acceptance, status, manifest, and evaluation records written after freeze go there, so a new evidence file needs no new exclusion or confirmation."

The existing rule that an exclusion must never hide an operative file stays as it is.

## Scope note
This recheck is a static reading of the documents. It is not evidence of Claude or Codex host behavior.

---

# Final recheck of E1-R9 (same reviewer, Rayshelle Peyton)

**Snapshot checked:** `sha256sum` output, computed by me:
- `SKILL.md` `0af2f0a38a6565fdcbf4d7d7bdefba461321a186743eef4b9fb2ec35177f8448`, unchanged since the R1–R8 recheck.
- `references/team-roles.md` `c972b5894c60f4002bd60f777487985a1c26ee885a30ad8405c78911fff6c9de`, unchanged.
- `references/artifacts.md` `a85160970fea23f2542a90341f34af604ad79a43f1b166c0cd3995f213a4e076`, which matches the expected value.

`artifacts.md` has no trailing whitespace. This recheck covers E1-R9 only and adds no new scope.

**E1-R9 evidence:** `artifacts.md:21` now says: "Exclude VCS metadata such as `.git/`; declare evidence-only directory prefixes before freeze so later evidence files under them remain excluded, and never place operative code, tests, configuration, skill instructions, or requirements in those prefixes."
- **Failure scenario 1 closed:** `.git/` is excluded, so a commit or fetch no longer changes the candidate.
- **Failure scenario 2 closed:** acceptance, review, and status records written after the freeze go under a directory prefix declared before the freeze. They stay excluded without a new exclusion, so line 25 ("Writing evidence alone must leave the candidate ID stable") now holds.
- **Operative files stay covered:** the ban on operative files in evidence prefixes keeps the rule that an exclusion must never hide an operative file. Line 25's EL-plus-reviewer confirmation now applies only to a genuinely new exclusion rule, which does not conflict with the added sentence.

## Final decision: READY (E1 static document review)

| ID | Severity | Disposition |
|---|---|---|
| E1-R1 | High | Resolved (`SKILL.md:18–19`) |
| E1-R2 | Medium | Resolved (`artifacts.md:23`) |
| E1-R3 | Medium | Resolved (`artifacts.md:23,27`) |
| E1-R4 | Medium | Resolved (`artifacts.md:10–11`) |
| E1-R5 | Low | Resolved (`SKILL.md:15`, `team-roles.md:32`) |
| E1-R6 | Low | Resolved (`artifacts.md:10–11`) |
| E1-R7 | Low | Resolved (`artifacts.md:17`) |
| E1-R8 | Low | Resolved (`SKILL.md:14`, `team-roles.md:28`) |
| E1-R9 | Medium | Resolved (`artifacts.md:21`) |

No findings remain open. READY applies only to the three E1 files at the hashes above. It is static acceptance: it is not evidence of Claude or Codex host behavior, and it does not replace QA, product, or EL acceptance or the judge's gate on a frozen candidate. Any change to these files reopens this review.
