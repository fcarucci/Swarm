# Third-party software

Swarm does not vendor third-party code. It installs or downloads the following at run time; each
keeps its own licence.

| Component | Licence | How Swarm gets it |
|---|---|---|
| [psycopg](https://www.psycopg.org/) (`psycopg[binary]`, bundles libpq) | LGPL-3.0 (libpq: PostgreSQL Licence) | pip, into Swarm's venv on first run |
| [zstandard](https://github.com/indygreg/python-zstandard) | BSD-3-Clause | pip, into Swarm's venv on first run |
| [rust-code-analysis](https://github.com/mozilla/rust-code-analysis) | MPL-2.0 | complexity-analyzer: downloaded on demand, sha256-verified |
| eslint, eslint-plugin-sonarjs, @typescript-eslint/parser, typescript, dependency-cruiser, jscpd, knip | each package's own licence | complexity-analyzer: npm, exact versions in `skills/complexity-analyzer/node-tools/package-lock.json`, on demand |
| tree-sitter, tree-sitter-rust, radon, vulture (Python) | each package's own licence | complexity-analyzer scripts: exact versions in their inline script metadata, installed by the script runner |
| Clippy | MIT or Apache-2.0 | complexity-analyzer: used from your Rust toolchain |
