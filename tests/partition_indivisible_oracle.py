"""Small independent integer feasibility oracle for partition tests."""
from typing import Iterable, Mapping, Optional, Tuple
from emuflow.errors import ValidationError

def validate_indivisible_limits(
    records: Iterable[Tuple[str, Mapping[str, int], Optional[str]]],
    limits: Mapping[str, Mapping[str, int]],
) -> None:
    """Reject necessary infeasibility without changing any configured bound."""
    fields = tuple(next(iter(limits.values())))
    totals = {field: 0 for field in fields}
    fixed = {fpga: {field: 0 for field in fields} for fpga in limits}
    for identity, weights, fixed_fpga in records:
        candidates = (fixed_fpga,) if fixed_fpga is not None else limits
        if not any(all(weights.get(field, 0) <= limits[fpga][field] for field in fields)
                   for fpga in candidates):
            raise ValidationError(
                f"strict balance/capacity infeasible: indivisible cluster {identity!r}, "
                f"fixed_fpga={fixed_fpga!r}, resources={dict(weights)!r}; "
                "automatic relaxation is forbidden"
            )
        for field in fields:
            totals[field] += weights.get(field, 0)
            if fixed_fpga is not None:
                fixed[fixed_fpga][field] += weights.get(field, 0)
    for field in fields:
        if totals[field] > sum(bound[field] for bound in limits.values()):
            raise ValidationError(f"strict balance/capacity infeasible: total {field} demand {totals[field]} exceeds integer bounds; automatic relaxation is forbidden")
        for fpga in limits:
            if fixed[fpga][field] > limits[fpga][field]:
                raise ValidationError(f"strict balance/capacity infeasible: fixed {fpga} {field} demand {fixed[fpga][field]} exceeds {limits[fpga][field]}; automatic relaxation is forbidden")
