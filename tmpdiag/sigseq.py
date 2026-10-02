import sys, os, time, tempfile, multiprocessing
sys.path.insert(0, "tests"); sys.path.insert(0, "lib")
from support import FileHarness
import test_file_board as t
from swarm.board import file as fb
def main(n):
    CTX = multiprocessing.get_context("spawn")
    multi = 0; seqs = {}
    for i in range(n):
        h = FileHarness(); h.reset(); path = h.cfg["file"]["path"]
        with h.board() as b:
            b.subscribe(messages_only=True)
            st = b._store
            p = CTX.Process(target=t._post_once, args=(path,)); p.start()
            seq = []; t0 = time.perf_counter(); last = st.signature(True)
            while p.is_alive() or time.perf_counter() - t0 < 0.3:
                s = st.signature(True)
                if s != last:
                    seq.append((round((time.perf_counter()-t0)*1000,1), s)); last = s
                time.sleep(0.001)
            p.join()
            # second post: append to a NON-empty log
            p = CTX.Process(target=t._post_once, args=(path,)); p.start()
            seq2 = []; t0 = time.perf_counter()
            while p.is_alive() or time.perf_counter() - t0 < 0.3:
                s = st.signature(True)
                if s != last:
                    seq2.append((round((time.perf_counter()-t0)*1000,1), s)); last = s
                time.sleep(0.001)
            p.join()
            if len(seq2) != 1: print("APPEND-NONEMPTY", i, seq2, flush=True)
        h.close()
        if len(seq) > 1:
            multi += 1
            if multi <= 5: print("MULTI", i, seq, flush=True)
        key = len(seq); seqs[key] = seqs.get(key, 0) + 1
    print("changes-per-post histogram", seqs, "multi", multi, "of", n)
if __name__ == "__main__":
    main(int(sys.argv[1]))
