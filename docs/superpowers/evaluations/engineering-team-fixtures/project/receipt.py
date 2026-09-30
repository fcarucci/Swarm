"""Unfinished synthetic receipt CLI."""

import json
import sys

from total import total_cents


def main():
    rows = json.load(sys.stdin)
    print(json.dumps({"total_cents": total_cents(rows), "currency": "USD"}))


if __name__ == "__main__":
    main()
