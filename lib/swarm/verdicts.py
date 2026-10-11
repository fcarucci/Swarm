"""The judge's verdict values that settle a goal. Kept free of imports: the hooks import the readers
that use it, and the hook module must stay stdlib-only (it must not import the board package)."""

SETTLED_VERDICTS = ("met", "unreachable")   # no more work is wanted for the goal (met), or it cannot be met
