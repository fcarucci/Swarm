"""`swarm transcript list|show|export`. Kept out of cli.py, which must stay under the Codex directory
edition's 256 KiB per-file limit. The functions use cli.py's helpers: cli binds them in with _bind()
when it finishes loading (its own names win only where this module defines none)."""
from __future__ import annotations


def _bind(names: dict) -> None:
    g = globals()
    for k, v in names.items():
        if not k.startswith("__") and k not in g:
            g[k] = v


def _transcript_list(board, cfg: dict, args) -> int:
    rows = sorted(board.transcripts(job=args.job, agent_name=args.agent),
                  key=lambda r: (r.job, r.captured_at))
    if not rows:
        print(f"no transcripts (kept {_keep_note(cfg)})")
        return 0
    from swarm.board.base import transcript_totals_of
    replaces: dict = {}   # (job, key) -> the key a supervisor replacement took over from
    for job in {r.job for r in rows}:
        replaces.update({(job, a.agent_key): a.resume_of or "" for a in board.agents(job)})
    table = [[r.job, r.agent_name or "", r.role, r.harness or "", _size(r.raw_bytes), _size(r.stored_bytes),
              _ratio(r.raw_bytes, r.stored_bytes), str(len(r.images)), str(r.redactions),
              CAPTURE_FAILED if r.failed else "yes" if r.final else "no", _when(r.captured_at),
              replaces.get((r.job, r.agent_key), ""),
              r.agent_key] for r in rows]
    print(_table(["JOB", "AGENT", "ROLE", "HOST", "RAW", "STORED", "RATIO", "IMAGES", "REDACTED", "FINAL",
                  "CAPTURED", "REPLACES", "KEY"], table, _transcript_use_color(args), bold_header=True,
                 paint={1: _paint_name_cell, 3: _paint_host, 4: _paint_dim, 5: _paint_dim,
                        8: _paint_redacted, 9: _paint_final}))
    t = transcript_totals_of(rows)   # images counted once however many transcripts show them
    print(f"total: {len(rows)} transcript{'s' if len(rows) != 1 else ''}, {_size(t.stored)} stored "
          f"({_size(t.raw)} raw, ratio {_ratio(t.raw, t.stored)}{_images_note(t.images, t.image_bytes)})")
    return 0


def _transcript_show(board, cfg: dict, args) -> int:
    import re
    from swarm.board import BoardError
    from swarm.board.base import bodiless
    if args.grep:
        try:
            re.compile(args.grep)
        except re.error as exc:
            print(f"bad --grep pattern: {term_safe(exc)}", file=sys.stderr)
            return 2
    if getattr(args, "memory", None) is not None:
        both = next((flag for flag, v in (("--job", args.job), ("--agent", args.agent), ("--key", args.key),
                                          ("--orchestrator", args.orchestrator)) if v), None)
        if both:
            print(f"--memory can't be combined with {both}: a memory names its own transcript", file=sys.stderr)
            return 2
        return _memory_show(board, cfg, args)
    if not (args.agent or args.orchestrator or args.key):
        print("say which transcript: --agent NAME, --orchestrator (with --job) or --key AGENT_KEY",
              file=sys.stderr)
        return 2
    if args.orchestrator and not args.job:
        print("--orchestrator needs --job", file=sys.stderr)
        return 2
    rows = board.transcripts(job=args.job, agent_name=args.agent, agent_key=args.key,
                             role="orchestrator" if args.orchestrator else None)
    who = ("the orchestrator" if args.orchestrator and not args.agent and not args.key
           else args.agent or f"key {args.key}")
    missing = f"no transcript for {who}" + (f" in {args.job}" if args.job else "") + \
        f"; transcripts are kept {_keep_note(cfg)}"
    if len(rows) > 1 and len({r.job for r in rows}) > 1:
        print(f"{len(rows)} transcripts for {who}; pick one with --job or --key:", file=sys.stderr)
        for r in sorted(rows, key=lambda r: r.captured_at):
            print(f"  {term_safe(r.job)}  {term_safe(r.role)}  {_when(r.captured_at)}  {_size(r.stored_bytes)}  "
                  f"key {term_safe(r.agent_key)}",
                  file=sys.stderr)
        return 1
    if len(rows) > 1:   # one job: the original agent and its supervisor replacements, oldest first
        replaces = {a.agent_key: a.resume_of for a in board.agents(rows[0].job)}
        runs = sorted(rows, key=lambda r: (_restart_depth(r.agent_key, replaces), r.captured_at))
        chunks = []
        for i, r in enumerate(runs, 1):
            try:
                body = None if bodiless(r) else board.transcript_body(r.job, r.agent_key)
            except BoardError as exc:
                body, unreadable = None, f"--- full transcript: unreadable ({term_safe(exc)}) ---"
            else:
                unreadable = _failed_note(r) if bodiless(r) else None
            if body is None and unreadable is None:
                continue
            head = (f"=== {term_safe(r.agent_name)}, run {i} of {len(runs)}: key {term_safe(r.agent_key)}, "
                    f"captured {_when(r.captured_at)}"
                    + (f", replaces {term_safe(replaces[r.agent_key])}" if replaces.get(r.agent_key) else "")
                    + ("\n" + _snapshot_label(r) if r.failed and body is not None else ""))
            chunks.append(head + "\n" + (unreadable if body is None
                                          else _render_transcript(board, body, args, _refs_of(board, r))))
        if not chunks:
            print(missing, file=sys.stderr)
            return 1
        return _emit_transcript("\n".join(chunks), args)
    if rows and bodiless(rows[0]):
        print(_failed_note(rows[0]).strip("- "), file=sys.stderr)
        return 1
    try:
        body = board.transcript_body(rows[0].job, rows[0].agent_key) if rows else None
    except BoardError as exc:   # corrupt or over TRANSCRIPT_MAX_RAW (a forged or damaged row)
        print(f"full transcript: unreadable ({term_safe(exc)})", file=sys.stderr)
        return 1
    if body is None:
        print(missing, file=sys.stderr)
        return 1
    out = _render_transcript(board, body, args, _refs_of(board, rows[0]))
    if rows[0].failed:   # the final capture failed: this is the last redacted snapshot
        if args.format == "jsonl":   # the output stays JSONL: the label goes to stderr
            print(_snapshot_label(rows[0]).strip("- "), file=sys.stderr)
        else:
            out = _snapshot_label(rows[0]) + "\n" + out
    return _emit_transcript(out, args)


def _transcript_export(board, cfg: dict, args) -> int:
    """Every transcript of the job as <agent>.jsonl, its images under images/, and index.tsv;
    the job's memory excerpts as memory/<doc id>.jsonl (their images under images/ too) and
    memory.tsv. The files are written relative to the target dir's fd, opened
    without following a link anywhere on its path (safefs.open_base); file names come from
    board data, so a sha256 that isn't 64 hex digits and a document id that fails
    provenance.valid_document_id are skipped."""
    import re
    from swarm import safefs, transcripts
    from swarm.board import BoardError
    global SHA256_HEX
    SHA256_HEX = SHA256_HEX or re.compile(r"[0-9a-f]{64}")
    rows = sorted(board.transcripts(job=args.job), key=lambda r: (r.role, r.agent_name or "", r.agent_key))
    refs = board.memory_refs(job=args.job)
    if not rows and not refs:
        print(f"no transcripts for {term_safe(args.job)}; transcripts are kept {_keep_note(cfg)}", file=sys.stderr)
        return 1
    target = Path(os.path.abspath(Path(args.dir).expanduser() if args.dir
                                  else Path.cwd() / f"transcripts-{safe_job(args.job)}"))
    root = _inside_writable_root(cfg, target)
    if root is not None and not getattr(args, "force", False):
        print(f"swarm transcript export: refusing {term_safe(target)}: it is inside {term_safe(root)}, which sandboxed agents "
              f"can write (they could plant links there). Pick another directory, or pass --force.",
              file=sys.stderr)
        return 1
    try:
        d = safefs.open_base(str(target), create=True)
    except (OSError, ValueError) as exc:
        print(f"swarm transcript export: refusing {term_safe(target)}: {term_safe(exc)}", file=sys.stderr)
        return 1
    images_fd = memory_fd = None
    try:
        used, index, skipped = set(), ["\t".join(INDEX_COLUMNS)], []
        for r in rows:
            base = "orchestrator" if r.role == "orchestrator" else safe_job(r.agent_name or r.agent_key)
            if base in used:
                base = f"{base}-{safe_job(r.agent_key)[:40]}"
            used.add(base)
            name = base + ".jsonl"
            try:
                body = board.transcript_body(r.job, r.agent_key) or b""
            except BoardError as exc:   # corrupt or over TRANSCRIPT_MAX_RAW (a forged row): not written
                body = b""
                print(f"{term_safe(name)}: transcript unreadable ({term_safe(exc)})", file=sys.stderr)
            _export_write(d, name, body)   # images stay placeholders; the files are in images/
            files = []
            for img in board.transcript_images(job=r.job, agent_key=r.agent_key):
                if not isinstance(img.sha256, str) or not SHA256_HEX.fullmatch(img.sha256):
                    skipped.append(img.sha256)
                    continue
                file = f"{img.sha256}.{transcripts.IMAGE_EXT.get(img.mime, 'bin')}"
                if images_fd is None:
                    images_fd = safefs.open_sub(d, "images", create=True)
                if not safefs.exists(images_fd, file):
                    full = board.transcript_image(img.sha256)
                    if full is None:
                        continue
                    _export_write(images_fd, file, full.data)
                elif not _plain_file(images_fd, file):
                    raise ExportRefused(f"images/{file}")
                files.append(f"images/{file}")
            cells = (name, r.agent_name or "", r.agent_key, r.role, r.host or "", getattr(r, "session_id", "") or "",
                     r.captured_at.isoformat() if r.captured_at else "",
                     CAPTURE_FAILED if r.failed else "yes" if r.final else "no",
                     r.raw_bytes, r.stored_bytes, r.redactions, ",".join(files), r.harness or "")
            index.append("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in cells))
        if rows:
            _export_write(d, "index.tsv", ("\n".join(index) + "\n").encode())
        exported, bad_ids = 0, []
        if refs:
            from swarm import provenance
            mindex, mused = ["\t".join(MEMORY_INDEX_COLUMNS)], set()
            for r in refs:
                if not provenance.valid_document_id(r.document_id):   # a forged row: never a file name
                    bad_ids.append(r.document_id)
                    continue
                base = _unused_name(safe_job(r.document_id), r.document_id, mused)
                mused.add(base)
                name = f"{base}.jsonl"
                try:
                    body = board.memory_ref_excerpt(r.document_id) or b""
                except BoardError as exc:   # over EXCERPT_MAX_RAW or corrupt: refused, not written
                    body = b""
                    print(f"memory {r.document_id}: excerpt unreadable ({term_safe(exc)})", file=sys.stderr)
                if memory_fd is None:
                    memory_fd = safefs.open_sub(d, "memory", create=True)
                _export_write(memory_fd, name, body)
                files = []
                for img in r.images:
                    if not isinstance(img.sha256, str) or not SHA256_HEX.fullmatch(img.sha256):
                        skipped.append(img.sha256)
                        continue
                    file = f"{img.sha256}.{transcripts.IMAGE_EXT.get(img.mime, 'bin')}"
                    if images_fd is None:
                        images_fd = safefs.open_sub(d, "images", create=True)
                    if not safefs.exists(images_fd, file):
                        full = board.transcript_image(img.sha256)
                        if full is None:
                            continue
                        _export_write(images_fd, file, full.data)
                    elif not _plain_file(images_fd, file):
                        raise ExportRefused(f"images/{file}")
                    files.append(f"images/{file}")
                cells = (f"memory/{name}", r.document_id, r.bank, r.writer, r.agent_name, r.agent_key,
                         r.harness or "", r.tool_call_id or "", r.created_at.isoformat() if r.created_at else "",
                         r.raw_bytes, r.redactions, ",".join(files))
                mindex.append("\t".join(str(c).replace("\t", " ").replace("\n", " ") for c in cells))
                exported += 1
            _export_write(d, "memory.tsv", ("\n".join(mindex) + "\n").encode())
    except ExportRefused as exc:
        print(f"swarm transcript export: refusing to write {term_safe(target / str(exc))}: something other than a "
              f"file of yours is already there (a link?); nothing was written through it", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:   # safefs.open_sub: a planted link or foreign dir
        print(f"swarm transcript export: refusing {term_safe(target)}: {term_safe(exc)}", file=sys.stderr)
        return 1
    finally:
        for fd in (images_fd, memory_fd):
            if fd is not None:
                os.close(fd)
        os.close(d)
    for sha in skipped:
        print(f"skipped an image with an invalid sha256 ({term_safe(sha)[:80]})", file=sys.stderr)
    for doc in bad_ids:
        print(f"skipped a memory ref with an invalid document id ({term_safe(repr(doc))[:80]})", file=sys.stderr)
    print(f"exported {len(rows)} transcript{'s' if len(rows) != 1 else ''}"
          + (f" and {exported} memory excerpt{'s' if exported != 1 else ''}" if refs else "")
          + f" of {term_safe(args.job)} to {term_safe(target)}")
    return 0
