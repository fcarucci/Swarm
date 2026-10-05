# CLI plugins

Core swarm discovers command plugins when it parses a command line. A plugin adds a subcommand,
adds options to or hooks around a core command, or adds lines to `swarm status --job J`. Core works
with none installed, `swarm plugins` lists what was found and why one failed, and a broken plugin
never breaks a core command. The hooks (`swarm hook ...`) load no plugins.

## Where plugins are found

In this order; the first plugin of a name wins (the rest are listed as shadowed):

1. `<config dir>/plugins/<name>.py` or `<name>/__init__.py`, next to the swarm config
   (`~/.config/swarm/plugins/`), then each directory of `$SWARM_PLUGIN_PATH` (os.pathsep separated).
2. Plugins shipped inside the swarm plugin's skills: `skills/<skill>/swarm_plugin.py`
   (the engineering-team skill's is `skills/engineering-team/swarm_plugin.py`).
3. Python entry points in the group `swarm.plugins`; the entry point is a module with
   `register`, or the `register` function itself.

`[plugins] disabled = ["name"]` in the swarm config skips plugins by name. `swarm plugins` prints
one line per plugin (name, `loaded`, `disabled` or `ERROR`, what it adds, where it came from) and
the error under a failed one.

A plugin that fails to import, or whose `register` raises or exits, is recorded and skipped, and
whatever it had registered is dropped. A plugin command that raises prints one line on stderr and
exits 1; a hook that raises is a warning on stderr and the core command goes on.

## The API

A plugin is a module with `register(api)`:

| `api` | |
|---|---|
| `api.name` | the plugin's name (its file or directory name) |
| `api.api_version` | 1 |
| `api.config_dir` | the directory of the swarm config, a `pathlib.Path` |
| `api.add_command(name, run, setup=None, help=None)` | a new subcommand. `setup(parser)` adds its arguments (an `argparse` parser); `run(ctx, args) -> int \| None` runs it (None is 0). A name that is a core command or another plugin's is an error |
| `api.extend_command(name, setup=None, before=None, after=None)` | add arguments to a core command, and run `before(ctx, args) -> int \| None` ahead of it (a non-zero status stops the command and is its exit status: how a plugin rejects an argument) and `after(ctx, args)` once it succeeded |
| `api.add_status_lines(fn)` | `fn(ctx, job) -> list[str]`: lines `swarm status --job J` prints after the job's own details |

`ctx` (`PluginContext`) is what a command or hook receives:

| `ctx` | |
|---|---|
| `ctx.cfg` | the loaded swarm config (a dict) |
| `ctx.config_dir`, `ctx.config_path` | where the config is |
| `ctx.plugin` | the plugin's name |
| `ctx.open_board()` | the board, as a context manager: `with ctx.open_board() as board:` (inside a status hook, the board `status` already holds open) |
| `ctx.job_data(board, job)` / `ctx.set_job_data(board, job, key, value)` | settings the plugin keeps with a job, under the plugin's own prefix; `value=None` removes a key. Stored on the board (`Board.job_data` / `set_job_data`, schema 15), so they follow the job to every host and survive re-activation. Keys are 1-64 of `a-z 0-9 _ . -`, values at most 2000 characters |

The board methods a plugin may call are those of `swarm.board.Board`; stay with reads and
`job_data` unless the plugin's purpose needs more. The API is versioned: a plugin can check
`api.api_version`.

## Example

```python
# ~/.config/swarm/plugins/hello.py
def run(ctx, args):
    with ctx.open_board() as board:
        js = board.job_status(args.job)
    print(f"{args.job}: {js.status if js else 'no such job'}")

def setup(parser):
    parser.add_argument("--job", required=True)

def register(api):
    api.add_command("hello", run, setup=setup, help="say how a job is")
```

`swarm hello --job J` now works, and `swarm plugins` lists `hello`.

## The engineering-team plugin

It adds `swarm team`, `swarm activate --team` and a `team` line in `swarm status --job J`, and reads
`team.toml` (`$SWARM_TEAM_CONFIG`, else next to the swarm config; see `team.example.toml`). It is
the reference use of this API: `skills/engineering-team/swarm_plugin.py`.
