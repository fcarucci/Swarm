# Privacy

Swarm has no telemetry and sends nothing to its author.

## What Swarm stores

- Board messages, job and agent metadata, verdicts, and project-memory facts, in the backend
  you configure: plain files or SQLite under `~/.local/state/swarm`, or your own Postgres server.
- Session transcripts, only when you set `[transcripts] enabled = true` (off by default).
  Redaction is best effort, and anyone who can read the board can read them.
- Local logs and stamps under `~/.local/share/swarm` and `~/.local/state/swarm`.

## Network calls

Swarm's first-session setup installs its Python dependencies from PyPI. Every other call is to a
service you configure or a command you run: `swarm upgrade` (GitHub), `swarm ci` (your forge), the
events listener (off by default, bound to 127.0.0.1), Hindsight memory (`[hindsight] url`), a
Postgres board, and the complexity-analyzer tool downloads (sha256-verified). See
[What Swarm changes on your machine](README.md#what-swarm-changes-on-your-machine).

## Secrets

Secrets (the webhook secret, the Hindsight key, the Postgres password) are read from files you own,
never logged and never put on command lines. Keep them mode 600. Use `sslmode=require` for a remote
Postgres server.

## Deleting your data

Remove the board directory or database, and `~/.local/share/swarm` and `~/.local/state/swarm`; see
[Uninstall](README.md#uninstall). Transcript retention and purging are described in the
[transcript archive reference](docs/REFERENCE.md#transcript-archive-optional).

Contact: Francesco Carucci <francesco@carucci.org>.
