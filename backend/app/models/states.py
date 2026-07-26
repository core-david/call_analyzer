"""Call lifecycle state machine.

The TRANSITIONS table is the single source of truth for which status
changes are legal. Everything that mutates `Call.status` must go through
assert_transition() — illegal transitions raise instead of corrupting data.
"""
# The whole point (as the task doc says) is what it forbids. Instead of scattering 
# if status == ... checks all over your codebase, you have one table that is the 
# single source of truth. Any illegal move — like a bug trying to set uploaded → completed — blows up loudly 
# at the exact line that attempted it, rather than silently corrupting a row.

from enum import StrEnum


class CallStatus(StrEnum):
    UPLOADED = "uploaded"
    TRANSCRIBING = "transcribing"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"


# - Each key is a current status
# - Each value is the set of statuses you're allowed to move to from there
TRANSITIONS: dict[CallStatus, frozenset[CallStatus]] = {
    CallStatus.UPLOADED: frozenset({CallStatus.TRANSCRIBING}),
    CallStatus.TRANSCRIBING: frozenset({CallStatus.ANALYZING, CallStatus.FAILED}),
    CallStatus.ANALYZING: frozenset({CallStatus.COMPLETED, CallStatus.FAILED}),
    CallStatus.COMPLETED: frozenset(),  # terminal
    # A failed call can go back to transcribing — that's the retry path (a user clicks "retry" in a later milestone). Note it goes back to the start of the pipeline, not to wherever it failed
    CallStatus.FAILED: frozenset({CallStatus.TRANSCRIBING}),  # user-triggered retry (M2)
}

# Why frozenset instead of a normal set or a list?
# A frozenset is an immutable set — once created, you can't add or remove items. 
# This is a constant rulebook. You never want code to accidentally do 
# TRANSITIONS[...].add(something) at runtime and quietly change the rules. 
# frozenset makes that impossible — it would raise an error.

class InvalidTransition(Exception):
    """Raised when a status change violates the TRANSITIONS table."""

    def __init__(self, from_status: CallStatus, to_status: CallStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(f"illegal transition: {from_status} -> {to_status}")


def can_transition(from_status: CallStatus, to_status: CallStatus) -> bool:
    return to_status in TRANSITIONS[from_status]


def assert_transition(from_status: CallStatus, to_status: CallStatus) -> None:
    if not can_transition(from_status, to_status):
        raise InvalidTransition(from_status, to_status)




