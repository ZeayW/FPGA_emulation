from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, Mapping, Set, Tuple

from emuflow.common.errors import ValidationError
from emuflow.platform.platform import Platform


LOGICAL_LANE_MAP_SCHEMA = "emuflow.logical-lane-map/v1"
TRANSPORT_ENDPOINTS_SCHEMA = "emuflow.transport-endpoints/v1"
VIRTUAL_IO_ANCHORS_SCHEMA = "emuflow.virtual-io-anchors/v1"
SPLIT_MANIFEST_SCHEMA = "emuflow.split-manifest/v1"


def _sv_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not name or name[0].isdigit():
        name = f"n_{name}"
    return name


def transport_endpoint_port(endpoint: Mapping[str, Any]) -> str:
    """Return the stable physical interface port for one TDM endpoint."""
    kind = endpoint.get("kind")
    if kind not in {"tx", "rx"}:
        raise ValidationError("transport endpoint kind must be tx or rx")
    link = endpoint.get("link")
    peer = endpoint.get("peer")
    if not isinstance(link, str) or not isinstance(peer, str):
        raise ValidationError("transport endpoint link/peer is invalid")
    return f"{kind}_{_sv_name(link)}_{_sv_name(peer)}"


def transport_to_systemverilog(
    transport: Mapping[str, Any], platform: Platform
) -> str:
    fpga_id = transport["fpga"]
    module_name = f"emuflow_transport_{_sv_name(fpga_id)}"
    links = {link.id: link for link in platform.links}
    groups = sorted(
        {
            (endpoint["link"], endpoint["peer"])
            for endpoint in transport["endpoints"]
        }
    )
    virtual_clock_order = transport.get("virtual_clock_order", ["clk"])
    if (
        not isinstance(virtual_clock_order, list)
        or not virtual_clock_order
        or any(not isinstance(clock, str) or not clock for clock in virtual_clock_order)
        or len(set(virtual_clock_order)) != len(virtual_clock_order)
    ):
        raise ValidationError("transport virtual clock order is invalid")
    virtual_clock_count = len(virtual_clock_order)
    ports = [
        "  input  logic fabric_clk",
        "  input  logic reset",
        "  input  logic links_ready",
        "  input  logic [SOURCE_COUNT-1:0] source_values",
        "  output logic [SHADOW_COUNT-1:0] shadow_values",
        f"  output logic [{virtual_clock_count - 1}:0] virtual_clock_enable",
        "  output logic [SLOT_BITS-1:0] slot_debug",
    ]
    kinds_by_group: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for endpoint in transport["endpoints"]:
        kinds_by_group[(endpoint["link"], endpoint["peer"])].add(
            endpoint["kind"]
        )
    bus_name: Dict[Tuple[str, str, str], str] = {}
    for link_id, peer in groups:
        width = links[link_id].transport_bits_per_cycle_per_direction
        if "rx" in kinds_by_group[(link_id, peer)]:
            name = transport_endpoint_port(
                {"kind": "rx", "link": link_id, "peer": peer}
            )
            bus_name[(link_id, peer, "rx")] = name
            ports.append(f"  input  logic [{width - 1}:0] {name}")
        if "tx" in kinds_by_group[(link_id, peer)]:
            name = transport_endpoint_port(
                {"kind": "tx", "link": link_id, "peer": peer}
            )
            bus_name[(link_id, peer, "tx")] = name
            ports.append(f"  output logic [{width - 1}:0] {name}")

    source_index = {
        item["signal"]: item["index"]
        for item in transport["source_signals"]
    }
    shadow_index = {
        item["signal"]: item["index"]
        for item in transport["shadow_signals"]
    }
    source_count = max(1, len(source_index))
    shadow_count = max(1, len(shadow_index))
    slot_bits = max(1, (transport["frame_slots"] - 1).bit_length())
    lines = [
        f"module {module_name} #(",
        f"  parameter integer FRAME_SLOTS = {transport['frame_slots']},",
        f"  parameter integer SLOT_BITS = {slot_bits},",
        f"  parameter integer SOURCE_COUNT = {source_count},",
        f"  parameter integer SHADOW_COUNT = {shadow_count}",
        ") (",
        ",\n".join(ports),
        ");",
        "",
        "  logic [SLOT_BITS-1:0] slot;",
        *(
            ['  (* KEEP = "yes", DONT_TOUCH = "yes" *)']
            if transport.get("cut_mode") == "static-exact-combinational"
            else []
        ),
        "  logic [SHADOW_COUNT-1:0] shadow_regs;",
        "  emuflow_virtual_runtime_controller #(",
        "    .FRAME_SLOTS(FRAME_SLOTS),",
        "    .SLOT_BITS(SLOT_BITS)",
        "  ) runtime_controller (",
        "    .fabric_clk(fabric_clk),",
        "    .reset(reset),",
        "    .links_ready(links_ready),",
        "    .virtual_clock_enable(virtual_clock_enable),",
        "    .event_index_debug(),",
        "    .slot(slot)",
        "  );",
        "  assign slot_debug = slot;",
        "  assign shadow_values = shadow_regs;",
        "",
    ]

    tx_groups = [
        group for group in groups if "tx" in kinds_by_group[group]
    ]
    if tx_groups:
        # Icarus models constant bit selects correctly here but emits one
        # sensitivity warning per select for always_comb. The equivalent
        # Verilog sensitivity form is accepted cleanly by both Icarus/Yosys.
        lines.append("  always @* begin")
        for link_id, peer in tx_groups:
            lines.append(
                f"    {bus_name[(link_id, peer, 'tx')]} = '0;"
            )
        lines.append("    case (slot)")
        by_slot: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
        for endpoint in transport["endpoints"]:
            if endpoint["kind"] == "tx":
                by_slot[endpoint["slot"]].append(endpoint)
        for slot in sorted(by_slot):
            lines.append(f"      {slot}: begin")
            for endpoint in sorted(
                by_slot[slot],
                key=lambda item: (item["link"], item["lane"]),
            ):
                signal = endpoint["signal"]
                if signal.startswith("net:"):
                    expression = f"source_values[{source_index[signal]}]"
                else:
                    expression = f"shadow_regs[{shadow_index[signal]}]"
                name = bus_name[
                    (endpoint["link"], endpoint["peer"], "tx")
                ]
                lines.append(
                    f"        {name}[{endpoint['lane']}] = {expression};"
                )
            lines.append("      end")
        lines.extend(
            ["      default: begin end", "    endcase", "  end", ""]
        )

    lines.extend(
        [
            "  always_ff @(posedge fabric_clk) begin",
            "    if (reset) begin",
            "      shadow_regs <= '0;",
            "    end else begin",
            "      case (slot)",
        ]
    )
    by_slot: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for endpoint in transport["endpoints"]:
        if endpoint["kind"] == "rx":
            by_slot[endpoint["arrival_slot"]].append(endpoint)
    for slot in sorted(by_slot):
        lines.append(f"        {slot}: begin")
        for endpoint in sorted(
            by_slot[slot],
            key=lambda item: (item["link"], item["lane"]),
        ):
            signal = endpoint["signal"]
            name = bus_name[(endpoint["link"], endpoint["peer"], "rx")]
            lines.append(
                f"          shadow_regs[{shadow_index[signal]}] "
                f"<= {name}[{endpoint['lane']}];"
            )
        lines.append("        end")
    lines.extend(
        [
            "        default: begin end",
            "      endcase",
            "    end",
            "  end",
            "endmodule",
            "",
        ]
    )
    return "\n".join(lines)
