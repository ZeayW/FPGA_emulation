"""Capture-bit bounds in the current Static Exact binary contract."""

STATIC_EXACT_CAPTURE_BIT_MAX_BITS = 8192
STATIC_EXACT_CAPTURE_BIT_MAX_BYTES = STATIC_EXACT_CAPTURE_BIT_MAX_BITS // 8
STATIC_EXACT_CAPTURE_BIT_ERROR = (
    "Static Exact capture bit must be a non-negative integer of at most "
    f"{STATIC_EXACT_CAPTURE_BIT_MAX_BITS} bits"
)
