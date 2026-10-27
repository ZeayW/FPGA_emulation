"""Current partition policy constants and indexed-validation diagnostics."""
from typing import Any, Dict, Iterable, Mapping, Tuple



CLUSTERS_SCHEMA = "emuflow.clusters/v1"
PARTITION_ASSIGNMENT_SCHEMA = "emuflow.partition-assignment/v1"
PARTITION_CONSTRAINTS_SCHEMA = "emuflow.partition-constraints/v1"
# Balance overall logic while dedicated resources remain capacity-constrained.
DEFAULT_BALANCE_DIMENSIONS: Tuple[str, ...] = ("cells", "lut", "ff")
TRANSPORTED_CUT_CLASSES = {"register_output", "register_input"}
LEGAL_CUT_CLASSES = {*TRANSPORTED_CUT_CLASSES, "primary_input"}
REPLICATED_NET_CLASSES = {"clock", "reset", "primary_input"}
CUT_MODE_SEQUENTIAL_ONLY = "sequential-only"
CUT_MODE_STATIC_EXACT = "static-exact-combinational"
DEFAULT_PARTITION_CUT_MODE = CUT_MODE_STATIC_EXACT
DEFAULT_PATRON_ALGORITHM_VERSION = 14
HARD_MACRO_RESOURCES = {
    "bram",
    "dsp",
    "carry",
    "bram18k",
    "uram288",
    "dsp48",
    "carry8",
}
FPGA_SELECTION_POLICIES = {"all", "explicit", "minimum-capacity"}


def summarize_balance_observation(
    loads: Mapping[str, Mapping[str, int]],
    strict_dimensions: Iterable[str],
) -> Dict[str, Any]:
    """Describe the load distribution without imposing another upper bound."""
    fields = tuple(next(iter(loads.values()), {}))
    totals = {field: sum(row[field] for row in loads.values()) for field in fields}
    hard_dimensions = sorted(strict_dimensions)
    return {
        "diagnostic_only": not hard_dimensions,
        "hard_constraint_dimensions": hard_dimensions,
        "design_share_by_fpga": {
            fpga: {field: row[field] / totals[field] if totals[field] else 0.0
                   for field in fields}
            for fpga, row in loads.items()
        },
        "max_to_mean_by_dimension": {
            field: (len(loads) * max(row[field] for row in loads.values()) / totals[field]
                    if totals[field] else 0.0)
            for field in fields
        },
    }
