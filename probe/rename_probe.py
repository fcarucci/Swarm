import os, sys, tempfile, threading, collections
sys.path.insert(0, "lib")
from swarm import compat
for variant in ("os.rename", "compat.rename_new", "compat.rename"):
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
                    elif variant == "compat.rename_new":
                        compat.rename_new(s, t, src_dir_fd=dfd, dst_dir_fd=dfd)
                    else:
                        compat.rename(s, t, src_dir_fd=dfd, dst_dir_fd=dfd)
                    wins[i] += 1
                except Exception as e:
                    errs[type(e).__name__ + str(getattr(e, "winerror", ""))] += 1
        ts = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        multi = sum(1 for i in range(n) if wins[i] > 1)
        none = sum(1 for i in range(n) if wins[i] == 0)
        tot["multi"] += multi; tot["none"] += none
        for k, v in errs.items(): tot["err:" + k] += v
    print(variant, dict(tot), flush=True)
