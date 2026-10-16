# Lead brief: toy goal, with one Codex engineer (live acceptance, MET run on a host with codex)

Same as LEAD-met.md, plus: request the engineer launch with `--harness codex` (a real Codex engineer, plan [F18]) and a
reviewer launch on Claude. Do not write code yourself and do not spawn subagents. When the integrator has landed the change
(an `INTEGRATED` post) run `swarm goal-ready --job <job> --candidate main@<landed sha>`. Ask a human nothing.
