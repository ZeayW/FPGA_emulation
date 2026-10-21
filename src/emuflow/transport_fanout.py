"""Deterministic buffer trees for synthesized transport-only high-fanout nets."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Tuple

from .errors import ValidationError
from .ir import EmuIR
from .resources import ResourceVector


TRANSPORT_FANOUT_BUFFERING_SCHEMA = "emuflow.transport-fanout-buffering/v1"
DEFAULT_TRANSPORT_MAX_FANOUT = 512


def _endpoint_key(endpoint: Mapping[str, Any]) -> Tuple[str, str, int]:
    instance = endpoint.get("instance")
    return (
        "" if instance is None else str(instance),
        str(endpoint.get("port")),
        int(endpoint.get("bit", 0)),
    )


def _buffer_instance(name: str, *, owner: str, level: int) -> Dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "type": "$lut",
        "resources": ResourceVector(lut=1).to_dict(),
        "parameters": {
            "LUT": "10",
            "WIDTH": "00000000000000000000000000000001",
        },
        "attributes": {
            "emuflow_high_fanout_buffer": "transport-balanced-tree-v1",
            "emuflow_high_fanout_owner": owner,
            "emuflow_high_fanout_level": str(level),
        },
        "constant_connections": [],
    }


def buffer_transport_high_fanout_nets(
    transport_ir: EmuIR,
    *,
    max_fanout: int = DEFAULT_TRANSPORT_MAX_FANOUT,
) -> Dict[str, Any]:
    """Insert balanced LUT1 trees into an owned transport EmuIR.

    The transform runs before packing and placement, so the placer sees every
    buffer and can co-locate each branch with its sink group.  It is limited to
    the separately synthesized transport design; original DUT topology and
    timing-path identity remain unchanged.  Clock and reset nets keep their
    dedicated handling.

    The input object is mutated deliberately.  Transport synthesis already
    owns this graph, and avoiding a second graph-sized copy keeps Phase 7's
    memory bounded.
    """

    if isinstance(max_fanout, bool) or not isinstance(max_fanout, int) or max_fanout < 2:
        raise ValidationError("transport fanout maximum must be an integer >= 2")

    value = transport_ir.value
    instances = value["instances"]
    nets = value["nets"]
    occupied_instances = {str(instance["id"]) for instance in instances}
    occupied_nets = {str(net["id"]) for net in nets}
    candidates = [
        (index, net)
        for index, net in enumerate(nets)
        if len(net["sinks"]) > max_fanout
        and net.get("cut_class") not in {"clock", "reset"}
        and len(net["drivers"]) == 1
    ]

    added_instances = []
    added_nets = []
    records = []
    for candidate_index, (net_index, net) in enumerate(candidates):
        original_sinks = sorted(
            (dict(endpoint) for endpoint in net["sinks"]),
            key=_endpoint_key,
        )
        owner = str(net["id"])
        downstream_sinks = original_sinks
        level = 0
        buffer_count = 0
        level_widths = []
        candidate_net_start = len(added_nets)
        while len(downstream_sinks) > max_fanout:
            upstream_sinks = []
            width = 0
            for group_index, offset in enumerate(
                range(0, len(downstream_sinks), max_fanout)
            ):
                group = downstream_sinks[offset : offset + max_fanout]
                stem = (
                    f"$emuflow_transport_fanout${candidate_index:06d}"
                    f"$l{level:02d}$g{group_index:06d}"
                )
                cell_id = stem + "$buf"
                output_net_id = stem + "$net"
                if cell_id in occupied_instances or output_net_id in occupied_nets:
                    raise ValidationError(
                        f"transport fanout buffer identifier collision for {owner!r}"
                    )
                occupied_instances.add(cell_id)
                occupied_nets.add(output_net_id)
                added_instances.append(
                    _buffer_instance(cell_id, owner=owner, level=level)
                )
                added_nets.append(
                    {
                        "id": output_net_id,
                        "name": output_net_id,
                        "yosys_bit": None,
                        "bus_index": 0,
                        "aliases": [],
                        "drivers": [
                            {"instance": cell_id, "port": "Y", "bit": 0}
                        ],
                        "sinks": group,
                        "fanout": len(group),
                        "cut_class": "combinational",
                        "fanout_buffer_owner": owner,
                    }
                )
                upstream_sinks.append(
                    {"instance": cell_id, "port": "A", "bit": 0}
                )
                buffer_count += 1
                width += 1
            level_widths.append(width)
            downstream_sinks = upstream_sinks
            level += 1

        net["sinks"] = downstream_sinks
        net["fanout"] = len(downstream_sinks)
        # The first level alone owns the original sinks and is the exact
        # equivalence boundary used for the audit.
        leaf_width = level_widths[0]
        leaf_nets = added_nets[
            candidate_net_start : candidate_net_start + leaf_width
        ]
        reconstructed = sorted(
            (endpoint for branch in leaf_nets for endpoint in branch["sinks"]),
            key=_endpoint_key,
        )
        if reconstructed != original_sinks:
            raise ValidationError(
                f"transport fanout buffering changed endpoints of {owner!r}"
            )
        records.append(
            {
                "net": owner,
                "net_index": net_index,
                "original_fanout": len(original_sinks),
                "root_fanout": len(downstream_sinks),
                "levels": level,
                "level_buffer_counts": level_widths,
                "buffer_cells": buffer_count,
            }
        )

    instances.extend(added_instances)
    nets.extend(added_nets)
    transport_ir.validate()

    return {
        "schema": TRANSPORT_FANOUT_BUFFERING_SCHEMA,
        "status": "pass",
        "policy": {
            "scope": "synthesized-transport-only",
            "topology": "balanced-lut1-tree",
            "maximum_fanout": max_fanout,
            "excluded_cut_classes": ["clock", "reset"],
            "placement": "before-pack-and-place",
        },
        "summary": {
            "buffered_nets": len(records),
            "buffer_cells": len(added_instances),
            "added_nets": len(added_nets),
            "maximum_original_fanout": max(
                (record["original_fanout"] for record in records), default=0
            ),
            "maximum_result_fanout": max(
                (
                    len(net["sinks"])
                    for net in nets
                    if net.get("cut_class") not in {"clock", "reset"}
                ),
                default=0,
            ),
        },
        "nets": records,
    }
