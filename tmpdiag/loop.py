import sys, unittest
sys.path.insert(0, "tests"); sys.path.insert(0, "lib")
def main():
    n = int(sys.argv[1]); bad = 0
    for i in range(n):
        s = unittest.defaultTestLoader.loadTestsFromName("test_file_board.FileBoardChangeDetectionTests")
        r = unittest.TextTestRunner(stream=open("nul" if sys.platform == "win32" else "/dev/null", "w")).run(s)
        if not r.wasSuccessful():
            bad += 1
            for _, tb in r.failures + r.errors: print("ITER", i, tb, flush=True)
    print("failures", bad, "of", n); sys.exit(1 if bad else 0)
if __name__ == "__main__":
    main()
