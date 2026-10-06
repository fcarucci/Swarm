"""JS/TS structural facts via regexes (no parser dependency; deliberately shallow)."""
import re

IMPORT_RES = [
    re.compile(r"""^\s*import\s+(?:[\w*{}\s,$]+?\s+from\s+)?['"]([^'"]+)['"]""", re.M),
    re.compile(r"""^\s*export\s+(?:[\w*{}\s,$]+?\s+)?from\s+['"]([^'"]+)['"]""", re.M),
    re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)"""),
]
INCLUDE_RE = re.compile(r"""^\s*#include\s+["<]([^">]+)[">]""", re.M)
EXPORT_RE = re.compile(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:abstract\s+)?"
                       r"(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)", re.M)
IFACE_RE = re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(interface|abstract\s+class)\s+([A-Za-z_$][\w$]*)", re.M)
IMPL_RE = re.compile(r"\b(?:class|interface)\s+[\w$]+[^{\n]*?\b(?:implements|extends)\s+([\w$.,\s<>]+?)\s*\{")
TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*")


def analyze(rel, raw_text):
    """raw_text must be the unstripped source (to see #include)."""
    imports = []
    for rx in IMPORT_RES:
        imports += rx.findall(raw_text)
    includes = INCLUDE_RE.findall(raw_text)
    exports = sorted(set(EXPORT_RE.findall(raw_text)))
    ifaces = [{"name": n, "kind": k.split()[0]} for k, n in IFACE_RE.findall(raw_text)]
    implements = []
    for m in IMPL_RE.findall(raw_text):
        implements += [re.split(r"[<.]", x.strip())[-1] if "." in x else x.strip().split("<")[0]
                       for x in m.split(",") if x.strip()]
    return {"file": rel, "imports": imports, "includes": includes, "exports": exports,
            "abstractions": ifaces, "implements": implements, "idents": _count(raw_text)}


def _count(text):
    d = {}
    for t in TOKEN_RE.findall(text):
        d[t] = d.get(t, 0) + 1
    return d
