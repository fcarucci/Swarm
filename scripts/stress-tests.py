#!/usr/bin/env python3
"""Repeat the full unittest suite under CPU load, using only private databases.

Example: .venv/bin/python scripts/stress-tests.py --runs 8 --postgres-runs 4 \
    --pg-bin /usr/lib/postgresql/17/bin --output ~/work/swarm-stress

The output directory must be new and outside /tmp. Logs, incremental failures.jsonl,
run outcomes and a flake table are retained there; the private PG cluster is removed.
No existing SWARM_TEST_CONFIG or live database is used. Requires initdb/pg_ctl for PG.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def suite_child(failure_file, backend, run):
    sys.path.insert(0, str(ROOT / 'tests'))
    class RecordedResult(unittest.TextTestResult):
        def record(self, test, err):
            trace = self._exc_info_to_string(err, test)
            with open(failure_file, 'a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(backend=backend, run=run,
                    test_id=test.id(), traceback_tail='\n'.join(trace.splitlines()[-30:]))) + '\n')
        def addFailure(self, test, err):
            super().addFailure(test, err)
            self.record(test, err)
        def addError(self, test, err):
            super().addError(test, err)
            self.record(test, err)
        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                self.record(subtest, err)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'))
    print(f'Discovered {suite.countTestCases()} tests for {backend} run {run}', flush=True)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordedResult).run(suite)
    summary = dict(tests_run=result.testsRun, failures=len(result.failures),
                   errors=len(result.errors), skipped=len(result.skipped))
    Path(failure_file).with_name(f'{backend}-{run:02d}-result.json').write_text(
        json.dumps(summary, indent=2))
    return 0 if result.wasSuccessful() else 1


def table(output, outcomes):
    failures = [json.loads(line) for line in (output / 'failures.jsonl').read_text().splitlines()]
    # Multiple subtest failures in one run count once for the same test id.
    counts = Counter((row['backend'], row['test_id']) for row in
                     { (f['backend'], f['run'], f['test_id']): f for f in failures }.values())
    runs = Counter(row['backend'] for row in outcomes)
    lines = ['| Backend | Test id | Failed runs / completed runs |', '| --- | --- | --- |']
    lines.extend(f'| {backend} | {test_id} | {count}/{runs[backend]} |'
                 for (backend, test_id), count in sorted(counts.items()))
    if not counts:
        lines.append('| all | No failures recorded | 0 |')
    for backend, count in sorted(runs.items()):
        passing = sum(row['returncode'] == 0 for row in outcomes if row['backend'] == backend)
        lines.append(f'\n{backend}: {passing}/{count} full runs passed.')
    rendered = '\n'.join(lines) + '\n'
    (output / 'flakes.md').write_text(rendered)
    (output / 'outcomes.json').write_text(json.dumps(outcomes, indent=2))
    return rendered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, default=15)
    parser.add_argument('--postgres-runs', type=int, default=8)
    parser.add_argument('--backends', nargs='+', choices=['memory', 'file', 'sqlite', 'postgres'],
                        default=['memory', 'file', 'sqlite', 'postgres'])
    parser.add_argument('--load-workers', type=int, default=min(4, 2 * (os.cpu_count() or 1)))
    parser.add_argument('--timeout', type=int, default=900, help='seconds per suite before recording runner error')
    parser.add_argument('--pg-bin', type=Path, help='directory containing initdb and pg_ctl')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.load_workers <= 4 or args.runs < 1 or args.postgres_runs < 1:
        parser.error('runs must be positive and load-workers between zero and four')
    output = args.output.expanduser().resolve()
    if output == Path('/tmp') or Path('/tmp') in output.parents:
        parser.error('output must be on disk, outside /tmp')
    output.mkdir(parents=True, exist_ok=False)
    failures = output / 'failures.jsonl'
    failures.touch()
    env = os.environ.copy()
    for key in ('SWARM_TEST_CONFIG', 'SWARM_TEST_BACKEND', 'PGPASSWORD', 'PGSERVICE', 'PGSERVICEFILE',
                'SWARM_TEST_SANDBOX'):
        env.pop(key, None)
    scratch = output / 'tmp'
    scratch.mkdir()
    env['TMPDIR'] = str(scratch)
    pg_ctl = None
    pgdata = output / 'pg-data'
    workers = []
    outcomes = []
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        if 'postgres' in args.backends:
            def binary(name):
                path = str(args.pg_bin / name) if args.pg_bin else shutil.which(name)
                if not path:
                    parser.error(f'{name} missing; provide --pg-bin')
                return path
            initdb, pg_ctl = binary('initdb'), binary('pg_ctl')
            subprocess.run([initdb, '-D', str(pgdata), '-U', 'swarm_stress', '-A', 'trust',
                            '--encoding=UTF8', '--locale=C'], check=True, stdout=subprocess.DEVNULL)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            subprocess.run([pg_ctl, '-D', str(pgdata), '-l', str(output / 'postgres.log'),
                            '-o', f'-h 127.0.0.1 -p {port} -k {scratch}', '-w', 'start'], check=True,
                           stdout=subprocess.DEVNULL)
            config = output / 'postgres.toml'
            config.write_text(f'''[database]\nhost = "127.0.0.1"\nport = {port}\nuser = "swarm_stress"\ndbname = "swarm_stress_test"\nadmin_dbname = "postgres"\nconnect_timeout = 5\nsslmode = "disable"\nquery_timeout_seconds = 8\nprepared_statements = false\n[board]\nbackend = "postgres"\n''')
        workers = [subprocess.Popen([sys.executable, '-c', 'while True: pass'])
                   for _ in range(args.load_workers)]
        metadata = dict(commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                        text=True).strip(), load_workers=args.load_workers,
                        load_pids=[worker.pid for worker in workers], backends=args.backends,
                        runs=args.runs, postgres_runs=args.postgres_runs)
        (output / 'metadata.json').write_text(json.dumps(metadata, indent=2))
        for backend in args.backends:
            for run in range(1, (args.postgres_runs if backend == 'postgres' else args.runs) + 1):
                runenv = env.copy()
                if backend == 'postgres':
                    runenv['SWARM_TEST_CONFIG'] = str(config)
                else:
                    runenv['SWARM_TEST_BACKEND'] = backend
                log = output / f'{backend}-{run:02d}.log'
                command = [sys.executable, '-B', str(Path(__file__).resolve()), '--suite-child',
                           str(failures), backend, str(run)]
                with log.open('w') as stream:
                    child = subprocess.Popen(command, cwd=ROOT, env=runenv, stdout=stream,
                                             stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        code = child.wait(timeout=args.timeout)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt):
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(child.pid, signal.SIGKILL)
                            child.wait()
                        if sys.exc_info()[0] is KeyboardInterrupt:
                            raise
                        code = 124
                recorded = [json.loads(line) for line in failures.read_text().splitlines()]
                if code != 0 and not any(row['backend'] == backend and row['run'] == run
                                         for row in recorded):
                    with failures.open('a') as stream:
                        stream.write(json.dumps(dict(backend=backend, run=run,
                            test_id='<suite-runner>', traceback_tail=f'exit {code}\n' +
                            '\n'.join(log.read_text(errors='replace').splitlines()[-30:]))) + '\n')
                outcome = dict(backend=backend, run=run, returncode=code)
                summary = output / f'{backend}-{run:02d}-result.json'
                if summary.exists():
                    outcome.update(json.loads(summary.read_text()))
                outcomes.append(outcome)
                print(f'{backend} run {run}: exit {code}; {log}', flush=True)
                print(table(output, outcomes), flush=True)
    finally:
        for worker in workers:
            worker.terminate()
        for worker in workers:
            worker.wait()
        if pg_ctl and (pgdata / 'postmaster.pid').exists():
            subprocess.run([pg_ctl, '-D', str(pgdata), '-m', 'immediate', '-w', 'stop'], check=True,
                           stdout=subprocess.DEVNULL)
        if pgdata.exists():
            shutil.rmtree(pgdata)
        table(output, outcomes)
    return int(any(row['returncode'] for row in outcomes))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--suite-child':
        sys.exit(suite_child(sys.argv[2], sys.argv[3], int(sys.argv[4])))
    sys.exit(main())
