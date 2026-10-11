"""toy: a tiny command line used as the target of the drive-mode live acceptance run."""
import sys

VERSION_TEXT = None   # the live goal: print "toy <version>" for --version (with a test)


def greet(name: str) -> str:
    return f"hello, {name}"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    print(greet(argv[0] if argv else "world"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
