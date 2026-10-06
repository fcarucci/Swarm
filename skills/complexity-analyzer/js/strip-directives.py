"""Strip C-style preprocessor directives so a JS parser can read the file.

PixInsight PJSR scripts carry `#include`, `#define`, `#feature-id` etc. which
are not JavaScript. Blank them (preserving line numbers, and following
backslash continuations) rather than deleting them, so every line number in
the ESLint report still matches the original file.
"""
import sys

src, dst = sys.argv[1], sys.argv[2]
out, skip = [], False
for line in open(src, encoding="utf8", errors="replace").read().split("\n"):
    if skip:
        skip = line.rstrip().endswith("\\")
        out.append("")
        continue
    if line.lstrip().startswith("#"):
        skip = line.rstrip().endswith("\\")
        out.append("")
        continue
    out.append(line)
open(dst, "w").write("\n".join(out))
