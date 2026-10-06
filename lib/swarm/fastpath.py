"""Advisory hook leases. Missing/stale state always falls back to the ordinary hook.

Files live in the host-only directory: sandbox-writable markers never authorize skipping
policy checks. A single notifier per config relays cross-machine posts; a live deadline
makes a crashed/disconnected notifier fail open to cursor reads, not lose messages.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from swarm import paths, safefs

SAFE = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
CLOCK = 'uptime-v1'


def boot_id():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        return ''


def now():
    # lxcfs can virtualize uptime while CLOCK_MONOTONIC still uses the host clock.
    # Match the shell hook's /proc/uptime deadlines inside containers.
    try:
        return float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def interval(cfg):
    # Idle/dead detection must be comfortably larger than the sampling window.
    requested = max(0, float(cfg.get('hook', {}).get('hook_min_interval_s', 15)))
    thresholds = [float(cfg['board'][k]) * 60 for k in ('idle_minutes', 'dead_minutes')]
    return min(requested, max(0, min(thresholds) / 10))


def directory():
    config = paths.config_path()
    stamp = config.stat().st_mtime_ns if config.exists() else 0
    identity = f'{config.absolute()}:{paths.PLUGIN_ROOT}:{stamp}:{CLOCK}:{boot_id()}'
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return paths.host_dir() / ('hook-fastpath-' + key)


@contextlib.contextmanager
def opened():
    base = safefs.open_base(paths.host_dir(), strict_mode=0o700)
    try:
        fd = safefs.open_sub(base, directory().name, strict_mode=0o700)
    finally:
        os.close(base)
    try:
        yield fd
    finally:
        os.close(fd)


def read(name):
    try:
        with opened() as fd:
            return (safefs.read(fd, name, limit=4096) or b"").decode().splitlines()
    except (OSError, ValueError):
        return []


def write(name, lines):
    with opened() as fd:
        safefs.write_atomic(fd, name, '\n'.join(map(str, lines)) + '\n')


def changed(job):
    if SAFE.fullmatch(job):
        try:
            write('board-' + job, [uuid.uuid4().hex])
        except (OSError, ValueError):
            pass


def invalidate(payload):
    key = payload.get('agent_id') or ('session-' + str(payload.get('session_id', '')))
    if isinstance(key, str) and SAFE.fullmatch(key):
        try:
            write('agent-' + key, [0, '-', '-'])
        except (OSError, ValueError):
            pass


def cache_config(cfg, host):
    if not SAFE.fullmatch(host):
        return
    path = paths.config_path()
    # mtime equality lets the shell detect replacements with older mtimes as well.
    stamp = path.stat() if path.exists() else None
    with opened():
        pass
    body = [str(path), str(paths.PLUGIN_ROOT), str(directory()),
            str(Path(cfg['hook']['marker_dir']).expanduser()), int(stamp is not None), CLOCK, boot_id()]
    if any('\n' in x for x in map(str, body)):
        return
    base = safefs.open_base(paths.host_dir(), strict_mode=0o700)
    try:
        name = 'hook-config-' + host
        safefs.write_atomic(base, name, '\n'.join(map(str, body)) + '\n')
        if stamp:
            os.utime(name, ns=(stamp.st_mtime_ns, stamp.st_mtime_ns), dir_fd=base, follow_symlinks=False)
    finally:
        os.close(base)


class Lease:
    def __init__(self, cfg, host, payload):
        self.cfg, self.host = cfg, host
        self.event = os.environ.get('SWARM_HOOK_EVENT', 'turn')
        self.key = payload.get('agent_id') or ('session-' + str(payload.get('session_id', '')))
        self.name = 'agent-' + self.key
        self.enabled = bool(os.environ.get('SWARM_HOOK_FASTPATH')) and bool(SAFE.fullmatch(self.key)) and interval(cfg) > 0
        self.now = now()
        self.contact_name = 'contact-' + self.key
        old = read(self.contact_name) if self.enabled else []
        self.contacted = False
        self.lockfd = None
        try:
            self.due = not old or self.now >= int(old[0])
        except (ValueError, IndexError):
            old = []
            self.due = True
        self.deadline = math.ceil(self.now + interval(cfg)) if self.due else int(old[0])
        if self.enabled and self.due:
            try:
                with opened() as fd:
                    self.lockfd = safefs.lock(fd, 'contact-lock-' + self.key, blocking=False)
                current = read(self.contact_name)
                if current and self.now < int(current[0]):
                    self.due, self.deadline = False, int(current[0])
            except BlockingIOError:
                self.due = False  # another hook owns this contact window
            except (OSError, ValueError):
                self.enabled = False
        self.generations = {}
        self.allowed = False
        self.eligible = False
        self.backlog = False
        self.job = None

    def capture(self, job):
        self.job = job
        generation = read('board-' + job)
        if not generation:
            changed(job)
            generation = read('board-' + job)
        self.generations[job] = generation

    def finish(self):
        if not self.enabled:
            if self.lockfd is not None:
                os.close(self.lockfd)
            return
        try:
            cache_config(self.cfg, self.host)
            if self.due and self.contacted:
                write(self.contact_name, [self.deadline])
            if self.cfg.get('provenance', {}).get('enabled'):
                write(self.name, [0, '-', '-'])
                return
            if self.event == 'done':
                old = read(self.name)
                if len(old) == 3:
                    write(self.name, [self.deadline, *old[1:]])
                ensure_notifier(self.cfg)
                return
            if self.allowed and not self.backlog and self.job and interval(self.cfg) > 0:
                generation = self.generations.get(self.job) or ['missing']
                write(self.name, [self.deadline, self.job, generation[0]])
            else:
                write(self.name, [0, '-', '-'])
            ensure_notifier(self.cfg)
        except (OSError, ValueError):
            pass
        finally:
            if self.lockfd is not None:
                os.close(self.lockfd)
                self.lockfd = None


def ensure_notifier(cfg):
    if cfg['board'].get('backend') == 'memory':
        return
    live = read('live')
    if live and int(live[0]) > now():
        return
    # flock inside the child guarantees at most one persistent notifier per config.
    subprocess.Popen([sys.executable, '-B', '-m', 'swarm.fastpath'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                     env={**os.environ, 'PYTHONPATH': str(paths.LIB_DIR)})


def notifier():
    from swarm.cli import load_config, watcher_config
    from swarm.board import open_board
    cfg = load_config(paths.config_path())
    original_directory = directory()
    with opened() as fd:
        with safefs.locked(fd, 'notifier.lock', blocking=False):
            try:
                # Exit after the marker set disappears; no permanent daemon outside a job.
                md = Path(cfg['hook']['marker_dir']).expanduser()
                with open_board(watcher_config(cfg)) as board:
                    board.subscribe(messages_only=True)
                    if board.degraded or getattr(board, '_polling', False):
                        return
                    # Close the read-to-LISTEN startup gap before advertising a healthy relay.
                    from swarm.hooks import _markers
                    for marker in _markers(cfg):
                        changed(marker['job'])
                    while directory() == original_directory and list(md.glob('*.json')):
                        write('live', [int(now()) + 5])
                        if board.wait_for_change(2):
                            # Payload is only an id on Postgres. Invalidating local jobs needs
                            # no board query; spurious reads are safe, lost notifications aren't.
                            from swarm.hooks import _markers
                            for marker in _markers(cfg):
                                changed(marker['job'])
                        # Non-LISTEN backends cheaply report their trigger counter changes.
            finally:
                # Clear the directory whose lock we own, even if config mtime changed.
                safefs.write_atomic(fd, 'live', '0\n')


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == '--cache':
            from swarm.cli import load_config
            cfg = load_config(paths.config_path())
            cache_config(cfg, sys.argv[2])
            print(Path(cfg['hook']['marker_dir']).expanduser())
        else:
            try:
                notifier()
            except Exception:
                pass
    except (OSError, ValueError):
        pass


if __name__ == '__main__':
    main()
