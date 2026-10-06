---
name: swarm-ask
description: Ask and answer structured decisions in a Swarm job when the human or a role must choose, while continuing independent work.
---

Ask when a decision belongs to the human or a role holder, or guessing wrong would cause meaningful rework, cost, downtime, or an unauthorized action. State the question, the cost of guessing wrong, and what work it blocks. Decide routine, reversible implementation details within your authorized scope yourself.

Use the stable `swarm` CLI available on the host:

```sh
swarm ask --job JOB --to human "Which approach should we use, and why does it matter?" --options a,b --default a --expires 2h --blocks "the dependent change"
swarm ask --job JOB --to @EL "Which interface should this feature expose?"
swarm questions --job JOB --open --to me
swarm answer ID --option b --comment "reason"
```

Defaults and expiry are optional. Give a default only when taking it after expiry is acceptable and already authorized; a default never grants permission. Without a default, an overdue question stays open. Keep working on anything the decision does not block.

Only the addressee, the current role holder, or the human can answer. Other agents use `swarm answer ID --comment "suggestion"`. Agents cannot answer for the human. To correct an answered question or an applied default, use `swarm answer ID --reopen "replacement answer"`; the asker receives the correction before its next tool call.

The orchestrator should forward open human questions concisely and record the human's answer with `swarm answer`; do not infer an answer from silence. The full question and answer remain queryable with `swarm questions --job JOB --all` after compaction.
