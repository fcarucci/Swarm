"""The Postgres board backend (production).

The SQL here is the SQL swarm.py and swarm_hooks.py ran before the board package existed,
moved over verbatim: a live database already carries this schema, its views and triggers, and
running `swarm tail`/`swarm watch` processes LISTEN on the swarm_board/swarm_state channels.
Do not change a statement here without a migration story for existing databases.

psycopg is imported only by this module; board/__init__.py imports it lazily, so hooks and
CLI paths that never reach Postgres never load psycopg.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import getpass
import json
import os
import random
import re
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

import psycopg
from psycopg import sql

from .base import (check_job_data, merged_job_data, parse_job_data, LEFT_PAUSED, PauseRecord, build_manifest, database_hosts, MOVED_PREFIX, NAME_PATTERN, check_images, check_name, decompress_capped, decompress_transcript, MemoryRef, valid_pool, restart_over_limits, STUCK_PREFIX, CloseGuard, CapExceeded, configured_message_cap, AUTO_CLOSED_BY, MEMORY_SEEN_MAX, NAME_SOURCES, RESTART_OUTCOMES, ROUTE_STATES, TOOL_NAME_MAX,
