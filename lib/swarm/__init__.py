"""swarm: agents that coordinate through a shared message board (Claude Code and Codex plugin).

Deliberately empty: `swarm.hooks` is imported on every hooked tool call and must stay
stdlib-only until a job is active, so nothing here may import the board or a backend."""
