"""Detached bounded notification worker. No shell; no command text/output in error logs."""
import json
import os
from pathlib import Path
import subprocess
import sys


def run(payload, env, error_path):
    try:
        completed = subprocess.run(payload['argv'], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=payload['timeout'], shell=False)
        if completed.returncode:
            raise RuntimeError(f'exit {completed.returncode}')
    except Exception as exc:
        kind = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        try:
            error_path.parent.mkdir(parents=True, exist_ok=True)
            with error_path.open('a', encoding='utf-8') as f:
                f.write(f'Q{env.get("SWARM_ID", "?")} {env.get("SWARM_EVENT", "?")}: notification failed ({kind})\n')
        except OSError:
            pass

if __name__ == '__main__':
    env = os.environ.copy()
    payload = json.loads(env.pop('SWARM_QUESTION_NOTIFY_PAYLOAD'))
    run(payload, env, Path(sys.argv[1]))
