"""A fake Hindsight HTTP API for the tests: the few endpoints the skill uses, same paths and
payload shapes as the real one (Hindsight 0.8 /openapi.json). Runs on 127.0.0.1 in a thread."""
from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BANK = re.compile(r"^/v1/default/banks/([^/]+)(/.*)?$")


class FakeHindsight:
    def __init__(self):
        self.banks: dict[str, list[dict]] = {}
        self.requests: list[dict] = []   # {"method", "path", "body", "auth"}
        self.delay = 0.0                 # seconds to stall every request
        self.delays: dict = {}           # (method, path suffix) -> seconds, for some requests only
        self.faults: list[dict] = []     # see fail()
        self.lock = threading.Lock()     # requests are served in parallel; the banks change under it
        # documents per bank: {bank: {document_id: {"id", "bank_id", "original_text", "tags",
        # "document_metadata", "created_at", "updated_at"}}} (Hindsight 0.8.6 DocumentResponse)
        self.documents: dict[str, dict[str, dict]] = {}
        # whether /openapi.json advertises `metadata` in UpdateDocumentRequest (0.8.6: it doesn't)
        self.metadata_patch = False
        self.api = "0.8"   # "0.10": profile answers 410, config 404s for a missing bank, no create-on-read
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code: int, body) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.requests.append({"method": method, "path": self.path, "body": body,
                                      "auth": self.headers.get("Authorization")})
                if fake.delay:
                    time.sleep(fake.delay)
                for (m, suffix), seconds in list(fake.delays.items()):   # a test may change delays meanwhile
                    if m == method and self.path.endswith(suffix):
                        time.sleep(seconds)
                with fake.lock:   # e.g. a late retain and its retry: replace-then-append is one step
                    code, reply = fake.route(method, self.path, body)
                try:
                    self._reply(code, reply)
                except OSError:  # the client gave up (timeout)
                    pass

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

            def do_PATCH(self):
                self._handle("PATCH")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---- state helpers
    def add_memory(self, bank: str, text: str) -> str:
        mid = str(uuid.uuid4())
        self.banks.setdefault(bank, []).append({"id": mid, "text": text, "type": "world"})
        return mid

    def fail(self, code: int, detail, bank: str | None = None, method: str | None = None,
             content: str | None = None, suffix: str | None = None) -> None:
        """Answer `code` with {"detail": detail} to every request matching all the given filters:
        the bank, the HTTP method, (for retains) an item whose content contains `content`, and
        a path ending with `suffix`."""
        self.faults.append({"code": code, "detail": detail, "bank": bank, "method": method,
                            "content": content, "suffix": suffix})

    def heal(self) -> None:
        self.faults.clear()

    def _fault(self, method: str, bank: str | None, body, path: str = ""):
        contents = " ".join(i.get("content", "") for i in (body or {}).get("items", [])) \
            if isinstance(body, dict) else ""
        for f in self.faults:
            if f["bank"] not in (None, bank) or f["method"] not in (None, method):
                continue
            if f.get("suffix") is not None and not path.endswith(f["suffix"]):
                continue
            if f["content"] is not None and f["content"] not in contents:
                continue
            return f["code"], {"detail": f["detail"]}
        return None

    def set_document_time(self, bank: str, doc: str, iso: str) -> None:
        """Pretend the document was created and last written at `iso`."""
        entry = self.documents[bank][doc]
        entry["created_at"] = entry["updated_at"] = iso

    def calls(self, method: str, suffix: str = "") -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["path"].endswith(suffix)]

    # ---- the API
    def route(self, method: str, path: str, body):
        if method == "GET" and path == "/openapi.json":
            props = {"tags": {}, **({"metadata": {}} if self.metadata_patch else {})}
            return 200, {"info": {"version": "fake"},
                         "components": {"schemas": {"UpdateDocumentRequest": {"properties": props}}}}
        if method == "GET" and path == "/v1/default/banks":
            return 200, {"banks": [{"bank_id": b} for b in sorted(self.banks)]}
        m = BANK.match(path)
        if not m:
            return 404, {"detail": "Not Found"}
        bank, rest = m.group(1), m.group(2) or ""
        fault = self._fault(method, bank, body, path)
        if fault:
            return fault
        if rest in ("/profile", "/background") and self.api == "0.10":
            return 410, {"detail": "The bank profile endpoints have been removed. Read /config instead."}
        if method == "GET" and rest == "/config":
            if self.api == "0.10" and bank not in self.banks:
                return 404, {"detail": f"Bank '{bank}' not found"}
            return 200, {"bank_id": bank, "config": {}, "overrides": {}}   # 0.8.6: 200 even for a missing bank
        if method == "GET" and rest == "/profile":
            if bank not in self.banks:
                return 404, {"detail": f"Bank '{bank}' not found"}   # 0.8.6's wording
            return 200, {"bank_id": bank, "name": bank, "disposition": {}, "mission": ""}
        if method == "PUT" and rest == "":
            self.banks.setdefault(bank, [])
            return 200, {"bank_id": bank, "name": bank, "disposition": {}, "mission": ""}
        if method == "DELETE" and rest == "":
            self.banks.pop(bank, None)
            self.documents.pop(bank, None)
            return 200, {"success": True}
        if rest.startswith("/documents/") and method in ("GET", "PATCH") and bank not in self.banks:
            return 404, {"detail": "Document not found"}   # 0.8.6: the same answer as for a missing document
        if bank not in self.banks:
            return 404, {"detail": f"Bank '{bank}' not found"}
        if method == "POST" and rest == "/memories":
            for item in body["items"]:
                doc = item.get("document_id")
                if doc:   # Hindsight: retaining a document again replaces it
                    self.banks[bank] = [m for m in self.banks[bank] if m.get("document_id") != doc]
                self.banks[bank].append({"id": str(uuid.uuid4()), "text": item["content"], "type": "world",
                                         "tags": item.get("tags"), "metadata": item.get("metadata"),
                                         "document_id": doc})
                if doc:
                    now = datetime.now(timezone.utc).isoformat()
                    old = self.documents.get(bank, {}).get(doc) or {}
                    self.documents.setdefault(bank, {})[doc] = {
                        "id": doc, "bank_id": bank, "original_text": item["content"],
                        "tags": item.get("tags") or [], "document_metadata": item.get("metadata") or {},
                        "created_at": old.get("created_at", now), "updated_at": now}
            return 200, {"success": True, "bank_id": bank, "items_count": len(body["items"]),
                         "async": bool(body.get("async"))}
        if method == "POST" and rest == "/memories/recall":
            return 200, {"results": [{k: v for k, v in mem.items() if k in ("id", "text", "type")}
                                     for mem in self.banks[bank]]}
        if rest.startswith("/documents/") and method in ("GET", "PATCH"):
            doc = urllib.parse.unquote(rest[len("/documents/"):])
            entry = self.documents.get(bank, {}).get(doc)
            if entry is None:
                return 404, {"detail": "Document not found"}
            if method == "GET":
                return 200, entry
            applied = False
            if isinstance(body, dict) and "metadata" in body and self.metadata_patch:
                entry["document_metadata"] = body["metadata"]
                applied = True
            if isinstance(body, dict) and "tags" in body:
                entry["tags"] = body["tags"]
                applied = True
            if not applied:
                return 422, {"detail": "At least one field must be provided"}
            return 200, {"success": True}
        return 404, {"detail": "Not Found"}


def dead_url() -> str:
    """A URL on a port nothing listens on (connection refused)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"
