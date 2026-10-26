"""Native common-reference sampling costs, separate from routed data delay.

The timing provider owns the measured cost. The signed clock/check adjustment
is derived from it and data delay; it is not another stored timing arc. Global
event consumers must retain that sign instead of clamping clock credit away.
"""
import math

from .errors import ValidationError
from .timing_disposition import is_optimized_away


SAMPLING_COST_POLICY = "vivado-clock-aware-reference-v1"


def require_sampling_cost_policy(actual, expected):
    """Assert a consumer's expected policy without changing producer selection."""
    if expected not in (None, SAMPLING_COST_POLICY) or (expected is not None and actual != expected):
        raise ValidationError("physical sampling cost policy differs from the required measurement policy")


def validate_sampling_cost(record, policy):
    if policy not in (None, SAMPLING_COST_POLICY):
        raise ValidationError("unsupported physical sampling cost policy")
    present = "sampling_cost_ns" in record
    expected = policy is not None and not is_optimized_away(record)
    if present != expected:
        raise ValidationError("physical sampling cost coverage/policy disagrees")
    if present:
        cost = record["sampling_cost_ns"]
        try:
            valid = (not isinstance(cost, bool) and isinstance(cost, (int, float))
                     and math.isfinite(float(cost)))
        except OverflowError:
            valid = False
        if not valid:
            raise ValidationError("physical sampling cost must be finite")


def parse_reference_cost_fields(fields, delay_ns, *, eliminated):
    """Validate the two provider columns, keeping only the canonical cost."""
    if len(fields) != 2:
        raise ValidationError("physical sampling cost TSV width differs")
    if eliminated:
        if fields != ["", ""]:
            raise ValidationError("optimized-away path carries a sampling cost")
        return None
    try:
        cost, adjustment = (float(value) for value in fields)
    except (TypeError, ValueError) as error:
        raise ValidationError("physical sampling cost TSV value is invalid") from error
    if (not all(math.isfinite(value) for value in (cost, adjustment, delay_ns))
            or not math.isclose(cost, delay_ns + adjustment, rel_tol=0., abs_tol=1e-8)):
        raise ValidationError("physical sampling cost and signed adjustment disagree")
    return cost
