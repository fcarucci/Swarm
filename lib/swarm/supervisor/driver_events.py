"""The canonical names of the driver's board notices (spec drive mode). Code posts these
constants, never a literal, and tests/test_driver_events.py checks that the docs and skills
mention no DRIVER-* name outside this list."""
from __future__ import annotations

DRIVER_AUDIT = "DRIVER-AUDIT"              # close-time target audit flagged a commit (spec 3.8 [F24])
DRIVER_BUG = "DRIVER-BUG"                  # queued launches stranded across consecutive passes
DRIVER_CONFIG = "DRIVER-CONFIG"            # owner-record recipe fields missing: job held
DRIVER_ESCALATE = "DRIVER-ESCALATE"          # repeated judge runs omitted their mandatory verdict
DRIVER_ERROR = "DRIVER-ERROR"              # driver step raised on driver_error_passes passes
DRIVER_INVARIANT = "DRIVER-INVARIANT"      # a pass ended with nobody responsible (spec 3.3)
DRIVER_LAND_PARTIAL = "DRIVER-LAND-PARTIAL"  # a landing left destinations partially landed ([RR5])
DRIVER_LEDGER_BAD = "DRIVER-LEDGER-BAD"    # unreadable ledger: job held ([F2])
DRIVER_LOCK_ORPHAN = "DRIVER-LOCK-ORPHAN"  # a drive lock matching no restart or intent ([F19])
DRIVER_ORPHAN = "DRIVER-ORPHAN"            # board drive claim without a ledger past the grace ([F2])
DRIVER_CLOSE_REJECTED = "DRIVER-CLOSE-REJECTED"  # the closing gate refused a pending MET (the goal judge runs again)
DRIVER_MECHANICAL_UNREACHABLE = "DRIVER-MECHANICAL-UNREACHABLE"  # a deadline passed: UNREACHABLE recorded by the supervisor itself
DRIVER_ROLLOVER = "DRIVER-ROLLOVER"        # a generation rollover could not build the new lead
DRIVER_TERMINAL_STUCK = "DRIVER-TERMINAL-STUCK"  # sessions not confirmed gone at the close deadline
DRIVER_UNHALT = "DRIVER-UNHALT"            # a halt ended: deadlines shifted by the halted time
DRIVER_STALLED = "DRIVER-STALLED"          # events check: no responsible actor for >2 stall periods
DRIVER_TAMPER = "DRIVER-TAMPER"            # board row changed without a trusted record (spec 3.2)

ALL = (DRIVER_ESCALATE, DRIVER_AUDIT, DRIVER_BUG, DRIVER_CLOSE_REJECTED, DRIVER_MECHANICAL_UNREACHABLE, DRIVER_ROLLOVER, DRIVER_TERMINAL_STUCK,
       DRIVER_UNHALT, DRIVER_CONFIG, DRIVER_ERROR, DRIVER_INVARIANT, DRIVER_LAND_PARTIAL,
       DRIVER_LEDGER_BAD, DRIVER_LOCK_ORPHAN, DRIVER_ORPHAN, DRIVER_STALLED, DRIVER_TAMPER)
