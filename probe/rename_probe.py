import os, sys, tempfile, threading, collections
sys.path.insert(0, "lib")
from swarm import compat
for variant in ("os.rename", "link+unlink", "create-excl"):
    tot = collections.Counter()
    for rnd in range(5):
        d = tempfile.mkdtemp()
        n = 40
        for i in range(n):
            open(os.path.join(d, f"f{i}.json"), "w").write("x")
        dfd = compat.open_dir(d)
        wins = collections.Counter()
        errs = collections.Counter()
        barrier = threading.Barrier(6)
        def run():
            barrier.wait()
            for i in range(n):
                s, t = f"f{i}.json", f"f{i}.sending-1"
                try:
                    if variant == "os.rename":
                        os.rename(os.path.join(d, s), os.path.join(d, t))
                    elif variant == "link+unlink":
                        os.link(os.path.join(d, s), os.path.join(d, t))
                        try:
                            os.unlink(os.path.join(d, s))
                        except FileNotFoundError:
                            pass
                    else:
                        fd = os.open(os.path.join(d, t + ".lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.close(fd)
                    wins[i] += 1
                    if variant == "os.rename" and os.path.exists(os.path.join(d, s)):
                        tot["src-still-there-after-ok"] += 1
                except Exception as e:
                    errs[type(e).__name__ + str(getattr(e, "winerror", ""))] += 1
        ts = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        multi = sum(1 for i in range(n) if wins[i] > 1)
        none = sum(1 for i in range(n) if wins[i] == 0)
        tot["multi"] += multi; tot["none"] += none
        for k, v in errs.items(): tot["err:" + k] += v
    print(variant, dict(tot), flush=True)
    if variant == "os.rename":
        d = tempfile.mkdtemp(); open(os.path.join(d, "a"), "w").write("x")
        os.rename(os.path.join(d, "a"), os.path.join(d, "b"))
        try:
            os.rename(os.path.join(d, "a"), os.path.join(d, "b")); print("SECOND rename of missing src onto existing dst: OK(!)")
        except Exception as e:
            print("second rename:", type(e).__name__, getattr(e, "winerror", ""))
