"""Turn a failed test log into GitHub annotations, so failures are readable from the public
checks API without downloading job logs. Usage: annotate.py LOGFILE"""
import re, sys

text = open(sys.argv[1], errors="replace").read()
fails = [l for l in text.splitlines() if "E2E FAIL" in l]
if fails:
    print("::error title=E2E FAIL::" + "%0A".join(l.replace("%", "%25") for l in fails[:10])[:3500])
blocks = re.split(r"\n(?==+\n(?:ERROR|FAIL): )", text)
count = 0
for b in blocks:
    m = re.search(r"^(ERROR|FAIL): (.+)$", b, re.M)
    if not m or count >= 40:
        continue
    count += 1
    body = b.strip().splitlines()[-25:]
    msg = "%0A".join(line.replace("%", "%25").replace("\r", "") for line in body)
    print(f"::error title={m.group(1)} {m.group(2)[:150]}::{msg[:3500]}")
if not count:
    tail = text.strip().splitlines()[-40:]
    print("::error title=log tail::" + "%0A".join(l.replace("%", "%25") for l in tail)[:3500])
