# Lead brief: toy goal (live acceptance, MET run)

You are the lead of a drive-mode job. The goal is small and real: add a `--version` flag to the toy CLI
(`toy.py`) that prints `toy 0.1.0`, with a unit test, so that `make test` passes.

- Request one `simple` engineer launch (Haiku) and one reviewer launch with `swarm launch`; give the
  engineer a work tree under the job's work root and a brief naming the branch `feat/<launch>`.
- Do not write code yourself and do not spawn subagents. Wait for hand-offs through the board.
- When the integrator has landed the change (an `INTEGRATED` post) declare the goal ready with
  `swarm goal-ready --job <job> --candidate main@<landed sha>`.
- Ask a human nothing: this run has nobody to answer. If something is ambiguous pick the simplest reading
  and say so in a post.
