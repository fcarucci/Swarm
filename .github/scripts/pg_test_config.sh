#!/usr/bin/env bash
# Write a throwaway SWARM_TEST_CONFIG for the CI Postgres service container and export it.
# dbname deliberately not the default "swarm_board": PostgresHarness refuses to run against the
# database of the live config (tests/support.py), so a throwaway database can never collide with a
# real board. admin_dbname is postgres (POSTGRES_DB in the workflow); the CI role is the cluster's
# initdb superuser, which has CREATEDB (run_tests.py gives each parallel slot its own dbname).
set -euo pipefail
dir="$RUNNER_TEMP/swarm-pg"
mkdir -p "$dir"
printf 'PGPASSWORD=swarm_test_pw\n' > "$dir/pg.env"
chmod 600 "$dir/pg.env"
cat > "$dir/test-config.toml" <<EOF
[database]
host = "127.0.0.1"
port = 5432
user = "swarm_test"
dbname = "swarm_ci_test"
admin_dbname = "postgres"
password_env_file = "$dir/pg.env"
connect_timeout = 5
sslmode = "prefer"
query_timeout_seconds = 8
prepared_statements = false

[board]
backend = "postgres"
EOF
echo "SWARM_TEST_CONFIG=$dir/test-config.toml" >> "$GITHUB_ENV"
