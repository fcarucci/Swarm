"""Optional project memory in Hindsight (https://github.com/vectorize-io/hindsight).

Only used when `[hindsight] url` is set; with no url nothing imports this module. Stdlib only
(urllib), and imported lazily by the hooks and the CLI.

A job's *project* (`swarm activate --project`, default the job name) names the Hindsight bank
its memories go to, so several jobs can share one project's memory. Endpoints used (Hindsight
HTTP API 0.8.6 and 0.10.2, /openapi.json):

  GET    /v1/default/banks/{bank}/profile          does the bank exist (0.8: 404 if not; 0.10 removed
                                                   it: 410)
  GET    /v1/default/banks/{bank}/config           the same question on 0.10 (404 if not); 0.8 answers
                                                   200 for any bank, so it is only asked after a 410
  PUT    /v1/default/banks/{bank}                  create it (only when the GET said 404)
  POST   /v1/default/banks/{bank}/memories         retain (async: returns once queued)
  POST   /v1/default/banks/{bank}/memories/recall  recall (budget "low")
  DELETE /v1/default/banks/{bank}                  tests only (throwaway banks)
  GET    /v1/default/banks/{bank}/documents/{id}   provenance checks (None only on 404 "Document not found")
  GET    /openapi.json                             capability probe (CLI only, cached for CAPS_TTL)
  PATCH  /v1/default/banks/{bank}/documents/{id}   only when the schema advertises `metadata` in
                                                   UpdateDocumentRequest (0.8.6: only `tags`, never sent)

Failure handling: every call has one short timeout (timeout_seconds). Only a sign that
Hindsight itself can't be reached, meaning a connection failure, a timeout or a 502/503/504
from a proxy in front of it, raises HindsightUnavailable and marks all of Hindsight unreachable
for retry_after_seconds (a marker file in the host-only dir ~/.local/share/swarm/host, which the
hooks write and a sandboxed CLI only reads), so an outage costs one timeout, not one per tool call. Any other error answer
(another 5xx, which is about one bank, or a 4xx, which is about one request) raises
HindsightError with the HTTP status and the server's `detail`, and leaves the other banks
alone. The API key (api_key_file) is sent as a Bearer token and never printed or logged.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BANK_ID_MAX = 64
QUERY_MAX = 1000
ITEM_MAX = 300   # characters of one memory shown to an agent
DETAIL_MAX = 200  # characters of a server's error detail kept in messages
GATEWAY_DOWN = (502, 503, 504)  # a proxy saying the Hindsight behind it is down
CAPS_TTL = 86400   # seconds a capability probe is trusted
CAPS_MAX = 4096    # bytes of a capability cache file read at most
DOCUMENT_NOT_FOUND = "Document not found"   # 0.8.6's detail on GET /documents/{id} of a missing
                                            # document
PATCH_STALE_SECONDS = 600   # a document last written longer than this before the write the
                            # hook saw is not the one it wrote (patch_document_metadata)


def _parse_time(value) -> _dt.datetime | None:
    """An ISO 8601 time from the server (naive: UTC), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=_dt.timezone.utc)


class HindsightUnavailable(Exception):
    """Hindsight can't be reached right now (network, timeout, 502/503/504, or recently marked
    so). Trips the circuit breaker for every bank."""


class HindsightError(Exception):
    """Hindsight answered with an error about one bank (5xx) or one request (4xx). `status` is
    the HTTP status (None when not from the server, e.g. memory switched off), `detail` the
    server's explanation from the error body (or "")."""

    def __init__(self, message: str, status: int | None = None, detail: str = ""):
        super().__init__(f"{message}: {detail}" if detail else message)
        self.status = status
        self.detail = detail

    @property
    def bank_scoped(self) -> bool:
        """A server-side error with the bank (5xx): the bank's other requests would fail too."""
        return self.status is not None and self.status >= 500


def _detail(exc: urllib.error.HTTPError) -> str:
    """The `detail` of an error body ({"detail": "..."} or FastAPI's list of validation errors),
    else the body's text; whitespace collapsed, capped at DETAIL_MAX."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        detail = json.loads(raw).get("detail", raw)
    except (ValueError, AttributeError):
        detail = raw
    if isinstance(detail, list):  # [{"loc": [...], "msg": "..."}]
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    text = " ".join(str(detail).split())
    return text if len(text) <= DETAIL_MAX else text[: DETAIL_MAX - 1] + "…"


def enabled(cfg: dict) -> bool:
    return bool(str((cfg.get("hindsight") or {}).get("url") or "").strip())


def bank_id(project: str) -> str:
    """A bank id for a project: lower case, runs of anything but [a-z0-9_-] become "-", at most
    64 characters. Hindsight's spec puts no pattern on bank ids; this keeps them safe in a URL
    path and readable."""
    bank = re.sub(r"[^a-z0-9_-]+", "-", project.lower()).strip("-")[:BANK_ID_MAX].strip("-")
    return bank or "swarm"


def project_of(job_status, job: str) -> str:
    """The job's project, or the job name when it has none."""
    return (getattr(job_status, "project", None) or job) if job_status else job


def recall_query(job_status, job: str) -> str:
    """What to recall with: the job's task, else its description, else its name."""
    text = (job_status.task or job_status.description) if job_status else None
    return " ".join((text or job).split())[:QUERY_MAX]


def format_memories(items: list[dict], project: str, cfg: dict, heading: str) -> tuple[str | None, list[str]]:
    """Render recalled memories under a `[swarm memory]` heading, capped at recall_max_items
    and recall_max_chars. Returns (text or None if nothing fits, ids shown)."""
    h = cfg["hindsight"]
    budget, lines, shown = int(h["recall_max_chars"]), [], []
    for item in items[: int(h["recall_max_items"])]:
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        line = "- " + (text if len(text) <= ITEM_MAX else text[: ITEM_MAX - 1] + "…")
        if len(line) > budget:
            break
        budget -= len(line)
        lines.append(line)
        shown.append(str(item["id"]))
    if not lines:
        return None, []
    return f"[swarm memory] {heading} (project \"{project}\"):\n" + "\n".join(lines), shown


def metadata_patch_in(openapi: dict) -> bool:
    """Whether this server's PATCH /documents/{id} accepts `metadata` (0.8.6: only tags)."""
    try:
        props = openapi["components"]["schemas"]["UpdateDocumentRequest"]["properties"]
    except (KeyError, TypeError):
        return False
    return isinstance(props, dict) and "metadata" in props


def caps_path(cfg: dict) -> Path:
    """The capability cache for this config's Hindsight url, in the host-only dir (the hooks
    trust it, so a sandboxed agent must not be able to write it)."""
    import hashlib
    from swarm import paths
    url = str((cfg.get("hindsight") or {}).get("url") or "")
    return paths.host_dir() / f"hindsight-caps-{hashlib.sha1(url.encode()).hexdigest()[:12]}.json"


def metadata_patch_supported(cfg: dict) -> bool:
    """From the cache refresh_caps wrote (never the network: the hooks call this). A missing,
    unsafe (link, another user's), unreadable or stale cache counts as False."""
    from swarm import safefs
    p = caps_path(cfg)
    try:
        d = safefs.open_base(p.parent, create=False, strict_mode=0o700)
    except (OSError, ValueError):
        return False
    try:
        fd = safefs.open_existing(d, p.name, os.O_RDONLY)
        try:
            st = os.fstat(fd)
            if time.time() - st.st_mtime > CAPS_TTL or st.st_size > CAPS_MAX:
                return False
            data = os.read(fd, CAPS_MAX + 1)
        finally:
            os.close(fd)
        return json.loads(data).get("metadata_patch") is True
    except (OSError, ValueError, AttributeError):
        return False
    finally:
        os.close(d)


def refresh_caps(cfg: dict, client: "Client | None" = None) -> bool:
    """Ask the server what it supports and cache it (0600, host-only dir). CLI only. Raises
    what Client raises when the server can't be asked."""
    from swarm import safefs
    supported = metadata_patch_in((client or Client(cfg)).openapi() or {})
    p = caps_path(cfg)
    with safefs.dir_fd(p.parent, strict_mode=0o700) as d:
        safefs.write_atomic(d, p.name, json.dumps({"metadata_patch": supported, "checked": time.time()}))
    return supported


def _fetch(req, timeout: float) -> bytes:
    """One HTTP request, reply read in full (raises what urlopen raises)."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_within(req, timeout: float, seconds: float) -> bytes:
    """_fetch in a daemon thread given at most `seconds`, all of it: name resolution (of the
    host or a proxy), connect, send and read. Out of time: TimeoutError, and the thread is left
    behind (it ends with the process; a request it completes meanwhile is harmless because
    retains carry a stable document_id)."""
    box: dict = {}

    def run():
        try:
            box["data"] = _fetch(req, timeout)
        except BaseException as exc:   # handed to the caller
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True, name="hindsight-request")
    t.start()
    t.join(max(0.0, seconds))
    if t.is_alive():
        raise TimeoutError(f"no complete reply within {seconds:.2f}s")
    if "error" in box:
        raise box["error"]
    return box["data"]


class Client:
    def __init__(self, cfg: dict):
        h = cfg["hindsight"]
        self.url = str(h["url"]).rstrip("/")
        self.timeout = float(h.get("timeout_seconds", 3))
        # time.monotonic() by which every call of this client must be done (the spool's bound on
        # one delivery; None: each call just has `timeout`). Running out of it is not an outage.
        self.deadline = h.get("deadline")
        self.retry_after = float(h.get("retry_after_seconds", 60))
        self.key_file = str(h.get("api_key_file") or "")
        self.max_tokens = int(h.get("recall_max_tokens", 1024))
        # the circuit breaker's marker: host-only, not in the spool, which a
        # sandboxed agent can write; reached through safefs (never a link, never blocking)
        self.marker_name = "hindsight-unreachable"
        self._profile_gone = False   # the server answered 410 to GET .../profile (0.10+)

    # ---- the circuit breaker
    def _marked_down(self) -> bool:
        from swarm import paths, safefs
        try:
            d = safefs.open_base(paths.host_dir(), create=False, strict_mode=0o700)
        except (OSError, ValueError):
            return False
        try:
            fd = safefs.open_existing(d, self.marker_name, os.O_RDONLY)
            try:
                return time.time() - os.fstat(fd).st_mtime < self.retry_after
            finally:
                os.close(fd)
        except OSError:
            return False
        finally:
            os.close(d)

    def _mark_down(self) -> None:
        from swarm import paths, safefs
        try:
            with safefs.dir_fd(paths.host_dir(), strict_mode=0o700) as d:
                safefs.touch(d, self.marker_name)
        except (OSError, ValueError):
            pass   # e.g. inside a sandbox: only this process doesn't remember the outage

    def _mark_up(self) -> None:
        from swarm import paths, safefs
        try:
            with safefs.dir_fd(paths.host_dir(), create=False, strict_mode=0o700) as d:
                safefs.unlink(d, self.marker_name)
        except (OSError, ValueError):
            pass

    def _key(self) -> str | None:
        if not self.key_file:
            return None
        text = Path(self.key_file).expanduser().read_text(encoding="utf-8").strip()
        return text.split("=", 1)[1].strip().strip("'\"") if "=" in text.splitlines()[0] else text.splitlines()[0]

    def _call(self, method: str, path: str, body=None, missing_ok: bool = False):
        if self._marked_down():
            raise HindsightUnavailable(f"marked unreachable within the last {self.retry_after:g}s")
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        key = self._key()
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        timeout = self.timeout
        if self.deadline is not None:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise HindsightUnavailable(f"out of time for {method} {path} (tried again later)")
            timeout = min(timeout, left)
        try:
            data = _fetch(req, timeout) if self.deadline is None else \
                _fetch_within(req, timeout, self.deadline - time.monotonic())
        except urllib.error.HTTPError as exc:
            detail = _detail(exc)
            exc.close()
            if exc.code in GATEWAY_DOWN:
                self._mark_down()
                raise HindsightUnavailable(f"HTTP {exc.code} from {method} {path}") from None
            self._mark_up()  # it answered: reachable; the error is this bank's or this request's
            if exc.code == 404 and missing_ok:
                return None
            raise HindsightError(f"HTTP {exc.code} from {method} {path}", exc.code, detail) from None
        except (urllib.error.URLError, OSError) as exc:  # refused, DNS, timeout
            reason = getattr(exc, "reason", exc)
            if self._out_of_time(reason):   # our deadline, not Hindsight's fault: no circuit breaker
                raise HindsightUnavailable(f"out of time for {method} {path} (tried again later)") from None
            self._mark_down()
            raise HindsightUnavailable(f"{type(reason).__name__}: {reason}") from None
        self._mark_up()
        return json.loads(data) if data else None

    def _out_of_time(self, reason) -> bool:
        """A timeout that came from this client's deadline (the call had less than `timeout`)."""
        return (self.deadline is not None and isinstance(reason, TimeoutError)
                and self.deadline - time.monotonic() < self.timeout)

    @staticmethod
    def _bank_path(bank: str) -> str:
        return "/v1/default/banks/" + urllib.parse.quote(bank, safe="")

    def _document_path(self, bank: str, document_id: str) -> str:
        return self._bank_path(bank) + "/documents/" + urllib.parse.quote(document_id, safe="")

    # ---- the API
    def bank_exists(self, bank: str) -> bool:
        """Whether the bank exists, on 0.8.6 and 0.10.x alike. 0.8.6 answers GET .../profile
        (404 for a missing bank). 0.10 removed that (410 Gone) and answers GET .../config with
        404 for a missing bank; 0.8.6's config answers 200 for any bank, so config is only asked
        after a 410. Decided by the answer, not by a version string."""
        if not self._profile_gone:
            try:
                return self._call("GET", self._bank_path(bank) + "/profile", missing_ok=True) is not None
            except HindsightError as exc:
                if exc.status != 410:
                    raise
                self._profile_gone = True
        return self._call("GET", self._bank_path(bank) + "/config", missing_ok=True) is not None

    def ensure_bank(self, bank: str) -> None:
        """Create the bank if it doesn't exist. Never PUTs an existing bank (PUT would reset
        fields it doesn't carry to their defaults)."""
        if not self.bank_exists(bank):
            self._call("PUT", self._bank_path(bank), {})

    def retain(self, bank: str, content: str, tags: list[str], metadata: dict[str, str],
               context: str | None = None, document_id: str | None = None) -> None:
        """Store one memory. With `document_id`, sending it again replaces that document rather
        than adding a second copy (a retry after a reply that never came)."""
        self.ensure_bank(bank)
        item = {"content": content, "tags": tags, "metadata": metadata}
        if context:
            item["context"] = context
        if document_id:
            item["document_id"] = document_id
        self._call("POST", self._bank_path(bank) + "/memories", {"items": [item], "async": True})

    def recall(self, bank: str, query: str) -> list[dict]:
        """[{"id", "text"}] most relevant first; [] if the bank doesn't exist yet."""
        body = {"query": query[:QUERY_MAX], "budget": "low", "max_tokens": self.max_tokens}
        reply = self._call("POST", self._bank_path(bank) + "/memories/recall", body, missing_ok=True)
        return [{"id": str(r["id"]), "text": r.get("text", "")} for r in (reply or {}).get("results", [])]

    def document(self, bank: str, document_id: str) -> dict | None:
        """The document (Hindsight's DocumentResponse), or None when Hindsight itself says it is
        gone: a 404 whose detail is DOCUMENT_NOT_FOUND (0.8.6 answers that for a document in a
        missing bank too). Any other 404 (a proxy, a wrong url path, another server: "Not
        Found") is no proof and raises HindsightError, like any other error answer."""
        try:
            return self._call("GET", self._document_path(bank, document_id))
        except HindsightError as exc:
            if exc.status == 404 and exc.detail == DOCUMENT_NOT_FOUND:
                return None
            raise

    def openapi(self) -> dict:
        return self._call("GET", "/openapi.json") or {}

    def patch_document_metadata(self, bank: str, document_id: str, metadata: dict[str, str],
                                written_after: _dt.datetime | None = None) -> None:
        """Merge `metadata` over the document's own metadata and PATCH it. Only call this when
        metadata_patch_supported() says the server takes it. HindsightError(404) if
        the document is missing. `written_after` (the hook's time of the write it pins): a
        HindsightError, and no PATCH, when the document's updated_at is missing or more than
        PATCH_STALE_SECONDS before it, so a forged success line naming an old document never
        rewrites that document's provenance."""
        current = self.document(bank, document_id)
        if current is None:
            raise HindsightError(f"document {document_id} not found in bank {bank}", 404)
        if written_after is not None:
            updated = _parse_time(current.get("updated_at"))
            if updated is None or updated < written_after - _dt.timedelta(seconds=PATCH_STALE_SECONDS):
                raise HindsightError(f"document {document_id} in bank {bank} was last written "
                                     f"{current.get('updated_at')!r}, long before this write "
                                     f"({written_after.isoformat(timespec='seconds')}): not patched")
        merged = {**(current.get("document_metadata") or {}), **metadata}
        self._call("PATCH", self._document_path(bank, document_id), {"metadata": merged})

    def delete_bank(self, bank: str) -> None:
        self._call("DELETE", self._bank_path(bank), missing_ok=True)


def remember(board, cfg: dict, job: str, name: str, text: str, project: str | None = None,
             document_id: str | None = None, metadata: dict[str, str] | None = None) -> str:
    """Store one fact in the job's project bank, tagged with job and agent; marks the agent as
    having stored something. Returns the project. Raises HindsightUnavailable/HindsightError.
    `document_id`: stable across retries of the same fact (the spool passes its record's).
    `metadata`: provenance the caller knows (provenance.cli_metadata), merged under the four
    keys source, job, agent and project, which always win."""
    project = project or project_of(board.job_status(job), job)
    Client(cfg).retain(bank_id(project), text, tags=["swarm", f"job:{job}", f"agent:{name}"],
                       metadata={**(metadata or {}), "source": "swarm", "job": job, "agent": name,
                                 "project": project},
                       context=f"swarm job {job}", document_id=document_id)
    board.record_remembered(name)
    return project


__all__ = ["CAPS_TTL", "Client", "HindsightError", "HindsightUnavailable", "bank_id", "caps_path",
           "enabled", "format_memories", "metadata_patch_in", "metadata_patch_supported",
           "project_of", "recall_query", "refresh_caps", "remember"]
