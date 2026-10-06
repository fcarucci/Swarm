"""Duplication via jscpd (needs node). Skips gracefully."""
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import nodetools
from .core import rnd

NO_NODE_ENV = "CXM_NO_NODE"

NOT_RUN = "jscpd not run (--no-dup)"


def run(work, mapping_prefix="prod", min_lines=5, min_tokens=50):
    """Run the pinned jscpd (skill-local npm prefix) over work/stage/prod. Skips gracefully without node tools."""
    ok, info = nodetools.available() if not os.environ.get(NO_NODE_ENV) else (False, nodetools.NO_NODE_REASON)
    if not ok:
        return {"available": False, "reason": "jscpd not run: " + str(info)}
    out = Path(work) / "jscpd"
    out.mkdir(exist_ok=True)
    stage = Path(work) / "stage" / mapping_prefix
    if not stage.is_dir():
        return {"available": False, "reason": "no production files"}
    cmd = [shutil.which("node"), str(nodetools.NODE_DIR / "node_modules" / "jscpd" / "run-jscpd.js"), "stage/" + mapping_prefix, "--reporters", "json",
           "--output", str(out), "--min-lines", str(min_lines), "--min-tokens", str(min_tokens),
           "--max-size", "100mb", "--max-lines", "1000000", "--silent", "--no-gitignore", "--no-colors", "--workers", "1"]
    try:
        r = subprocess.run(cmd, cwd=str(work), capture_output=True, timeout=900, env=nodetools.scratch_env())
    except Exception as exc:
        return {"available": False, "reason": f"jscpd failed to run: {exc}"}
    rep = out / "jscpd-report.json"
    if r.returncode != 0 or not rep.exists():
        return {"available": False,
                "reason": "jscpd failed: " + (r.stderr.decode("utf-8", "replace").strip()[-200:] or "no report")}
    return {"available": True, "raw": json.loads(rep.read_text(encoding="utf-8"))}


def summarize(raw_result, mapping):
    if not raw_result.get("available"):
        return {"available": False, "reason": raw_result.get("reason")}
    raw = raw_result["raw"]
    tot = raw["statistics"]["total"]

    def rel(name):
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        return mapping.get(base, base)

    clones = []
    for d in raw.get("duplicates", []):
        a, b = d["firstFile"], d["secondFile"]
        x = (rel(a["name"]), a["start"], a["end"])
        y = (rel(b["name"]), b["start"], b["end"])
        x, y = sorted([x, y])
        clones.append({"a": {"file": x[0], "start": x[1], "end": x[2]},
                       "b": {"file": y[0], "start": y[1], "end": y[2]}, "lines": d["lines"]})
    clones.sort(key=lambda c: (-c["lines"], c["a"]["file"], c["a"]["start"], c["b"]["file"], c["b"]["start"]))
    return {"available": True, "tool": "jscpd@" + (nodetools.pkg_version("jscpd") or "?"), "min_lines": 5, "min_tokens": 50,
            "percentage": rnd(tot["percentage"], 2), "duplicated_lines": tot["duplicatedLines"],
            "total_lines": tot["lines"], "clones_count": len(clones), "clones": clones}
