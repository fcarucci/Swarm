# Inputs of the thresholds 2.1.0 calibration (2026-10-06)

Command (both passes, limits from thresholds.json 2.1.0):

    python3 bin/calibrate.py rust=~/work/cxv-bench2/metrics/rust python=~/work/cxv-bench2/metrics/python \
        js=~/work/cav2-langtools-out/bench2-js

Result: limits Rust 3/7/16, Python 11/17/31, JS/TS 8/13/26; p95 over 32 systems R 2.4355, V 0.0386, so profile_cap 4.0592
(rounded 4.06) and tail_cap 0.0643 (rounded 0.064). `cxv-bench2/metrics/*` are bin/metrics outputs of the validate-scores
review-fix round (exact code-line partition) on the pinned commits of benchmark.tsv; `bench2-js` is the same JS/TS repos
measured with the new tools (ESLint + SonarJS) by `bin/metrics REPO --lang js --no-dup`.

The older Rust/Python outputs `~/work/cxv-out/bench` and `~/work/cxv-out/bench-py` (before that fix round) with the same JS
directory give R 2.4339, profile_cap 4.0565, V and tail_cap unchanged. Both round to profile_cap 4.06; thresholds use the
first set. These directories are on the author's box only and are not committed; rerun `bin/bench.py` to regenerate.
