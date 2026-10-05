"""Exact lookup-table construction for rectangular SLR grids."""


def _manhattan_mst_cost(nodes, num_slr_y):
    """Return the rectilinear MST length for a non-empty SLR subset."""

    if len(nodes) < 2:
        return 0
    connected = {nodes[0]}
    remaining = set(nodes[1:])
    cost = 0
    while remaining:
        distance, selected = min(
            (
                min(
                    abs(node // num_slr_y - source // num_slr_y)
                    + abs(node % num_slr_y - source % num_slr_y)
                    for source in connected
                ),
                node,
            )
            for node in remaining
        )
        cost += distance
        connected.add(selected)
        remaining.remove(selected)
    return cost


def build_sll_counts_table_values(num_slr_x, num_slr_y):
    """Build the table indexed by the bit layout used by the native kernels.

    The SLL objective is the rectilinear minimum-spanning-tree length of the
    SLRs touched by a net.  This reproduces the upstream 1x4 and 2x2 tables and
    also defines common 1x2/2x1 devices.  The explicit size bound prevents an
    accidental exponential allocation for an unreviewed platform topology.
    """

    if not isinstance(num_slr_x, int) or not isinstance(num_slr_y, int):
        raise TypeError("SLR grid dimensions must be integers")
    if num_slr_x <= 0 or num_slr_y <= 0:
        raise ValueError("SLR grid dimensions must be positive")
    num_slrs = num_slr_x * num_slr_y
    if num_slrs > 12:
        raise ValueError(
            "SLR lookup-table generation supports at most 12 regions"
        )

    values = []
    for mask in range(1 << num_slrs):
        nodes = [
            index
            for index in range(num_slrs)
            if mask & (1 << (num_slrs - index - 1))
        ]
        values.append(_manhattan_mst_cost(nodes, num_slr_y))
    return values
