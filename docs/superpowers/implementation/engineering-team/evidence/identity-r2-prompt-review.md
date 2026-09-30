# Identity R2 launch prompt review

Decision: **READY for the bounded fresh identity regression**. This is a prompt review, not a behavioral pass or final technical acceptance.

Reviewed the proposed, unlaunched templates in the old worktree:

| Input | SHA-256 |
| --- | --- |
| `.superpowers/sdd/2026-09-29-engineering-team/runs/identity-r2/executor-template.txt` | `943f0fc6bd68f5f7ac045cb751ee25576a7c5359169380e6e69ca94971122329` |
| `.superpowers/sdd/2026-09-29-engineering-team/runs/identity-r2/resume-template.txt` | `ad40af69267763fca843b71395f2bc61269634ef705ea09b237c73d162567540` |

The executor gives a distinct job and source CLI location, authorizes native Agent use, and controls evaluation permissions and reading scope. It does not state a PM key, expected board result, owner/direct-return/read procedure, role tier, grading rule, or defect location. The resume prompt asks for the first implementation draft before review without suggesting how to resolve the identity defect. `--no-supervise` is a supported Swarm job option (`lib/swarm/cli.py`); it controls unattended job supervision rather than the PM key or board behavior. The prohibition on attaching to other workspaces is normal evaluation isolation, not an identity algorithm hint. These prompts do not reuse the prompt-assisted F04/F05 operator instructions.

The initial planning return is an added pacing checkpoint before the fixture's first task-review checkpoint. Treat this as a **post-finding targeted regression**, not as an original preregistered F01/F02 behavioral run. The second return is draft-ready before the review starts. A root may later continue the representative receipt workflow, but neither pacing return proves the peer review, QA, product, or final delivery gates.

At launch, substitute `{{PLUGIN}}`, `{{JOB}}`, and `{{COMMIT}}` with the new frozen source package and each root's unique job. Preserve separate seed/request copies, roots, sessions, and trace capture. The operator should record the expanded prompt bytes and package ID outside the executor workspace. The regression result must come from actual role invocations, PM enrollment/read behavior in both live jobs, and each root's resume; the prompt review alone makes no such claim.

Source candidate at review: `85cd76518f3b448f67a36c061d92cadd8a18c4b5f0c8ce62d1549a9a1139087d`; four-skill package digest `341592f67b533be925e844178777ff9a6a9b640021e893cdc298721903541ad8`. No launch or grading was performed in this review.
