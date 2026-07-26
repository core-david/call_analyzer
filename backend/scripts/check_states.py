"""Exhaustive check of the call state machine — every (from, to) pair."""

import sys
from itertools import product
from pathlib import Path

# Put backend/ (this file's parent's parent) on sys.path so `app` imports
# regardless of how the script is launched (module, direct, or IDE Run button).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.states import (
    TRANSITIONS,
    CallStatus,
    InvalidTransition,
    assert_transition,
    can_transition,
)

# The full truth table, written out by hand and independent of TRANSITIONS,
# so a typo in the real table can't hide behind a matching typo in the test.
EXPECTED_ALLOWED = {
    (CallStatus.UPLOADED, CallStatus.TRANSCRIBING),
    (CallStatus.TRANSCRIBING, CallStatus.ANALYZING),
    (CallStatus.TRANSCRIBING, CallStatus.FAILED),
    (CallStatus.ANALYZING, CallStatus.COMPLETED),
    (CallStatus.ANALYZING, CallStatus.FAILED),
    (CallStatus.FAILED, CallStatus.TRANSCRIBING),
}

failures = []
for src, dst in product(CallStatus, CallStatus):
    expected = (src, dst) in EXPECTED_ALLOWED
    actual = can_transition(src, dst)
    if actual != expected:
        failures.append(f"  {src} -> {dst}: expected {expected}, got {actual}")

    # assert_transition must agree with can_transition on every pair
    try:
        assert_transition(src, dst)
        raised = False
    except InvalidTransition:
        raised = True
    if raised == actual:  # raised when allowed, or didn't raise when forbidden
        failures.append(f"  assert_transition mismatch on {src} -> {dst}")

# Terminal state has no way out
if TRANSITIONS[CallStatus.COMPLETED]:
    failures.append("  COMPLETED should be terminal (empty set)")

if failures:
    print("FAILED:")
    print("\n".join(failures))
    raise SystemExit(1)
print(f"OK — all {len(list(product(CallStatus, CallStatus)))} pairs correct")
