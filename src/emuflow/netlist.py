from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .errors import ValidationError
from .ir import EmuIR
from .partition import PARTITION_ASSIGNMENT_SCHEMA
from .platform import Platform
from .resources import ResourceVector
from .tdm import TDM_SCHEDULE_SCHEMA


FPGA_NETLIST_SCHEMA = "emuflow.fpga-netlist/v1"
LOGICAL_LANE_MAP_SCHEMA = "emuflow.logical-lane-map/v1"
TRANSPORT_ENDPOINTS_SCHEMA = "emuflow.transport-endpoints/v1"
VIRTUAL_IO_ANCHORS_SCHEMA = "emuflow.virtual-io-anchors/v1"
SPLIT_MANIFEST_SCHEMA = "emuflow.split-manifest/v1"


def _instance_endpoint(
    endpoint_id: str, port: str
) -> Dict[str, Any]:
    return {"instance": endpoint_id, "port": port, "bit": 0}


def _endpoint_id(kind: str, schedule_entry_id: str) -> str:
    return f"__emuflow_{kind}_{schedule_entry_id}"


def _sv_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not name or name[0].isdigit():
        name = f"n_{name}"
    return name


def _instance_copies(
    ir: EmuIR,
    assignment: Mapping[str, Any],
) -> Dict[str, List[Tuple[str, str, bool]]]:
    primary = assignment["instance_assignment"]
    copies = {
        instance["id"]: [(primary[instance["id"]], instance["id"], False)]
        for instance in ir.value["instances"]
    }
    replication = assignment.get("replication")
    if replication is None:
        return copies
    for record in replication["replicas"]:
        target = record["target_fpga"]
        for item in record["instances"]:
            original = item["original_instance"]
            if original not in copies:
                raise ValidationError(
                    f"replica references unknown original instance {original!r}"
                )
            copies[original].append(
                (target, item["replica_instance"], True)
            )
    return copies


def build_split_artifacts(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    platform: Platform,
    pin_plan: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if assignment.get("schema") != PARTITION_ASSIGNMENT_SCHEMA:
        raise ValidationError(
            f"assignment.schema: expected {PARTITION_ASSIGNMENT_SCHEMA!r}"
        )
    if schedule.get("schema") != TDM_SCHEDULE_SCHEMA:
        raise ValidationError(
            f"schedule.schema: expected {TDM_SCHEDULE_SCHEMA!r}"
        )
    if assignment.get("design") != ir.value["design"]["name"]:
        raise ValidationError("assignment.design does not match EmuIR")
    if schedule.get("design") != ir.value["design"]["name"]:
        raise ValidationError("schedule.design does not match EmuIR")
    if assignment.get("platform") != platform.name:
        raise ValidationError("assignment.platform does not match BoardDB")
    if schedule.get("platform") != platform.name:
        raise ValidationError("schedule.platform does not match BoardDB")
    exact_contract = None
    exact_contract_sha256 = None
    exact_node_by_net: Dict[str, Mapping[str, Any]] = {}
    exact_segments_by_cut_fpga: Dict[
        Tuple[str, str], List[Mapping[str, Any]]
    ] = defaultdict(list)
    if assignment.get("semantic_contract") is not None:
        from .combinational_cut import semantic_contract_sha256
        from .routing import static_exact_contract_from_assignment
        from .tdm import is_sampled_virtual_wire_schedule

        exact_contract = static_exact_contract_from_assignment(assignment)
        if exact_contract is None:
            raise ValidationError("exact assignment contract is missing")
        exact_contract_sha256 = semantic_contract_sha256(exact_contract)
        if (
            not is_sampled_virtual_wire_schedule(schedule)
            or schedule.get("semantic_contract_sha256")
            != exact_contract_sha256
        ):
            raise ValidationError(
                "exact split inputs do not share one semantic contract"
            )
        exact_node_by_net = {
            item["net"]: item for item in exact_contract["cut_nodes"]
        }
        for segment in exact_contract["logic_segments"]:
            source_cut = segment.get("source_cut_net")
            fpga = segment.get("fpga")
            if isinstance(source_cut, str) and isinstance(fpga, str):
                exact_segments_by_cut_fpga[(source_cut, fpga)].append(
                    segment
                )
    physical_lane_by_entry: Dict[str, int] = {}
    if pin_plan is not None:
        if pin_plan.get("schema") != "emuflow.placement-aware-pin-plan/v1":
            raise ValidationError("pin plan has an unsupported schema")
        physical_lane_by_entry = {
            item["schedule_entry"]: item["physical_lane"]
            for item in pin_plan.get("entries", [])
        }
        expected_entries = {item["id"] for item in schedule["entries"]}
        if (
            set(physical_lane_by_entry) != expected_entries
            or len(physical_lane_by_entry)
            != len(pin_plan.get("entries", []))
        ):
            raise ValidationError(
                "pin plan must cover every schedule entry exactly"
            )

    fpga_ids = [fpga.id for fpga in platform.fpgas]
    fpga_set = set(fpga_ids)
    instance_assignment = assignment.get("instance_assignment")
    if not isinstance(instance_assignment, dict):
        raise ValidationError("assignment.instance_assignment: expected object")
    if set(instance_assignment.values()) - fpga_set:
        raise ValidationError("assignment contains unknown FPGA identifiers")
    copies_by_instance = _instance_copies(ir, assignment)
    replica_instance_ids = {
        copy_id
        for copies in copies_by_instance.values()
        for _, copy_id, is_replica in copies
        if is_replica
    }

    instances_by_fpga: Dict[str, List[Dict[str, Any]]] = {
        fpga_id: [] for fpga_id in fpga_ids
    }
    for instance in ir.value["instances"]:
        locations = copies_by_instance.get(instance["id"])
        if not locations:
            raise ValidationError(
                f"instance {instance['id']!r} has no partition assignment"
            )
        for fpga_id, copy_id, is_replica in locations:
            copied_instance = dict(instance)
            copied_instance["id"] = copy_id
            if is_replica:
                copied_instance["replica_of"] = instance["id"]
                copied_instance["replica_target_fpga"] = fpga_id
            instances_by_fpga[fpga_id].append(copied_instance)

    route_by_demand = {
        route["id"]: route for route in schedule_routes(schedule)
    }
    incoming: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    endpoints_by_fpga: Dict[str, List[Dict[str, Any]]] = {
        fpga_id: [] for fpga_id in fpga_ids
    }
    lane_entries: List[Dict[str, Any]] = []

    for entry in sorted(schedule["entries"], key=lambda item: item["id"]):
        demand = entry["demand"]
        route = route_by_demand.get(demand)
        if route is None:
            raise ValidationError(
                f"schedule entry {entry['id']!r} has no route metadata"
            )
        tx_id = _endpoint_id("tx", entry["id"])
        rx_id = _endpoint_id("rx", entry["id"])
        tx_signal = (
            f"net:{entry['net']}"
            if entry["from"] == route["source"]
            else f"shadow:{demand}:{entry['from']}"
        )
        rx_signal = f"shadow:{demand}:{entry['to']}"
        common = {
            "schedule_entry": entry["id"],
            "demand": demand,
            "net": entry["net"],
            "link": entry["link"],
            "slot": entry["slot"],
            "arrival_slot": entry["arrival_slot"],
            "lane": physical_lane_by_entry.get(entry["id"], entry["lane"]),
            "logical_lane": entry["lane"],
        }
        exact_tx = {}
        exact_rx = {}
        if exact_contract is not None:
            node = exact_node_by_net[entry["net"]]
            source_entry = entry["from"] == route["source"]
            exact_tx = {
                "boundary_identity": f"static_exact:tx:{entry['id']}",
                "semantic_contract_sha256": exact_contract_sha256,
                "source_role": (
                    "cut_source" if source_entry else "route_tree_relay"
                ),
                "source_logic_segment_ids": (
                    list(node["source_segment_ids"])
                    if source_entry
                    else []
                ),
                "preserve": True,
            }
            exact_rx = {
                "boundary_identity": f"static_exact:rx:{entry['id']}",
                "semantic_contract_sha256": exact_contract_sha256,
                "consumer_logic_segment_ids": sorted(
                    segment["id"]
                    for segment in exact_segments_by_cut_fpga.get(
                        (entry["net"], entry["to"]), []
                    )
                ),
                "preserve": True,
            }
        endpoints_by_fpga[entry["from"]].append(
            {
                "id": tx_id,
                "kind": "tx",
                "fpga": entry["from"],
                "peer": entry["to"],
                "signal": tx_signal,
                **common,
                **exact_tx,
            }
        )
        endpoints_by_fpga[entry["to"]].append(
            {
                "id": rx_id,
                "kind": "rx",
                "fpga": entry["to"],
                "peer": entry["from"],
                "signal": rx_signal,
                **common,
                **exact_rx,
            }
        )
        lane_entries.append(
            {
                "id": f"lane_{entry['id']}",
                "schedule_entry": entry["id"],
                "demand": demand,
                "net": entry["net"],
                "link": entry["link"],
                "from": entry["from"],
                "to": entry["to"],
                "slot": entry["slot"],
                "lane": physical_lane_by_entry.get(
                    entry["id"], entry["lane"]
                ),
                "logical_lane": entry["lane"],
                "tx_endpoint": tx_id,
                "rx_endpoint": rx_id,
            }
        )
        key = (demand, entry["to"])
        if key in incoming:
            raise ValidationError(
                f"demand {demand!r} has multiple incoming edges at "
                f"{entry['to']!r}"
            )
        incoming[key] = entry

    cut_by_net = {
        cut["net"]: cut for cut in assignment.get("cut_nets", [])
    }
    demand_by_net = {
        route["net"]: route["id"] for route in route_by_demand.values()
    }
    net_segments: Dict[str, List[Dict[str, Any]]] = {
        fpga_id: [] for fpga_id in fpga_ids
    }
    ports_by_fpga: Dict[str, Set[str]] = {
        fpga_id: set() for fpga_id in fpga_ids
    }

    for net in ir.value["nets"]:
        original_by_fpga: Dict[
            str, Dict[str, List[Dict[str, Any]]]
        ] = {
            fpga_id: {"drivers": [], "sinks": []} for fpga_id in fpga_ids
        }
        touched: Set[str] = set()
        for collection in ("drivers", "sinks"):
            for endpoint in net[collection]:
                if endpoint["instance"] is None:
                    continue
                for fpga_id, copy_id, _ in copies_by_instance[
                    endpoint["instance"]
                ]:
                    copied_endpoint = dict(endpoint)
                    copied_endpoint["instance"] = copy_id
                    original_by_fpga[fpga_id][collection].append(
                        copied_endpoint
                    )
                    touched.add(fpga_id)

        top_drivers = [
            dict(endpoint)
            for endpoint in net["drivers"]
            if endpoint["instance"] is None
        ]
        top_sinks = [
            dict(endpoint)
            for endpoint in net["sinks"]
            if endpoint["instance"] is None
        ]
        for fpga_id in touched:
            original_by_fpga[fpga_id]["drivers"].extend(top_drivers)
            original_by_fpga[fpga_id]["sinks"].extend(top_sinks)
            ports_by_fpga[fpga_id].update(
                endpoint["port"] for endpoint in top_drivers + top_sinks
            )

        demand = demand_by_net.get(net["id"])
        for fpga_id in sorted(touched):
            drivers = original_by_fpga[fpga_id]["drivers"]
            sinks = original_by_fpga[fpga_id]["sinks"]
            source_kind = "original"
            if demand is not None and fpga_id in cut_by_net[net["id"]]["sink_fpgas"]:
                entry = incoming.get((demand, fpga_id))
                if entry is None:
                    raise ValidationError(
                        f"cut net {net['id']!r} has no incoming scheduled hop "
                        f"at {fpga_id!r}"
                    )
                drivers.append(
                    _instance_endpoint(
                        _endpoint_id("rx", entry["id"]), "shadow_out"
                    )
                )
                source_kind = "transport_shadow"
            elif any(
                endpoint["instance"] in replica_instance_ids
                for endpoint in drivers
            ):
                source_kind = "logic_replica"
            net_segments[fpga_id].append(
                {
                    "id": f"{net['id']}@{fpga_id}",
                    "original_net": net["id"],
                    "name": net["name"],
                    "cut_class": net["cut_class"],
                    "source_kind": source_kind,
                    "drivers": drivers,
                    "sinks": sinks,
                    **(
                        {
                            "static_exact_boundary": {
                                "semantic_contract_sha256": (
                                    exact_contract_sha256
                                ),
                                "role": (
                                    "transport_shadow_sink"
                                    if source_kind == "transport_shadow"
                                    else "cut_source"
                                    if demand is not None
                                    and fpga_id
                                    == route_by_demand[demand]["source"]
                                    else "local_logic"
                                ),
                                "logic_segment_ids": (
                                    sorted(
                                        segment["id"]
                                        for segment in (
                                            exact_segments_by_cut_fpga.get(
                                                (net["id"], fpga_id), []
                                            )
                                        )
                                    )
                                    if source_kind == "transport_shadow"
                                    else list(
                                        exact_node_by_net[net["id"]][
                                            "source_segment_ids"
                                        ]
                                    )
                                    if demand is not None
                                    and fpga_id
                                    == route_by_demand[demand]["source"]
                                    else []
                                ),
                            }
                        }
                        if exact_contract is not None and demand is not None
                        else {}
                    ),
                }
            )

    port_by_id = {port["id"]: port for port in ir.value["ports"]}
    fpga_netlists: Dict[str, Dict[str, Any]] = {}
    transports: Dict[str, Dict[str, Any]] = {}
    anchors: Dict[str, Dict[str, Any]] = {}
    for fpga_id in fpga_ids:
        local_instances = sorted(
            instances_by_fpga[fpga_id], key=lambda item: item["id"]
        )
        local_resources = ResourceVector.sum(
            ResourceVector.from_mapping(instance["resources"])
            for instance in local_instances
        ).to_dict(include_zeros=False)
        fpga_netlists[fpga_id] = {
            "schema": FPGA_NETLIST_SCHEMA,
            "design": ir.value["design"],
            "platform": platform.name,
            "fpga": fpga_id,
            "ports": [
                dict(port_by_id[port_id])
                for port_id in sorted(ports_by_fpga[fpga_id])
            ],
            "instances": local_instances,
            "nets": sorted(
                net_segments[fpga_id],
                key=lambda item: item["original_net"],
            ),
            "resources": local_resources,
            **(
                {"semantic_contract_sha256": exact_contract_sha256}
                if exact_contract is not None
                else {}
            ),
        }
        local_endpoints = sorted(
            endpoints_by_fpga[fpga_id], key=lambda item: item["id"]
        )
        source_signals = sorted(
            {
                endpoint["signal"]
                for endpoint in local_endpoints
                if endpoint["kind"] == "tx"
                and endpoint["signal"].startswith("net:")
            }
        )
        shadow_signals = sorted(
            {
                f"shadow:{endpoint['demand']}:{fpga_id}"
                for endpoint in local_endpoints
                if endpoint["kind"] == "rx"
            }
        )
        transports[fpga_id] = {
            "schema": TRANSPORT_ENDPOINTS_SCHEMA,
            "design": ir.value["design"]["name"],
            "platform": platform.name,
            "fpga": fpga_id,
            "frame_slots": schedule["metrics"]["frame_slots"],
            "source_signals": [
                {"index": index, "signal": signal}
                for index, signal in enumerate(source_signals)
            ],
            "shadow_signals": [
                {"index": index, "signal": signal}
                for index, signal in enumerate(shadow_signals)
            ],
            "endpoints": local_endpoints,
            **(
                {
                    "cut_mode": "static-exact-combinational",
                    "semantic_contract_sha256": exact_contract_sha256,
                    "shadow_reset_value": 0,
                    "startup_policy": "first-post-reset-frame-current-arrivals",
                }
                if exact_contract is not None
                else {}
            ),
        }
        anchors[fpga_id] = _build_virtual_anchors(
            fpga_id, platform, local_endpoints
        )

    lane_map = {
        "schema": LOGICAL_LANE_MAP_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "frame_slots": schedule["metrics"]["frame_slots"],
        "binding_status": "logical_only",
        "entries": sorted(lane_entries, key=lambda item: item["id"]),
        **(
            {
                "cut_mode": "static-exact-combinational",
                "semantic_contract_sha256": exact_contract_sha256,
            }
            if exact_contract is not None
            else {}
        ),
    }
    manifest = {
        "schema": SPLIT_MANIFEST_SCHEMA,
        "design": ir.value["design"]["name"],
        "platform": platform.name,
        "provider": (
            (
                "chimew-electrical-pin-split-v1"
                if pin_plan.get("provider")
                == "chimew-paper-plus-emuflow-electrical-slot-v1"
                else "placement-aware-pin-split-v1"
            )
            if pin_plan is not None
            else "deterministic-cut-shadow-split-v1"
        ),
        "board_binding": {
            "status": "virtual",
            "requires_hardware_bsp_for_package_pins": True,
        },
        "fpgas": [
            {
                "fpga": fpga_id,
                "netlist": f"{fpga_id}/netlist.json",
                "transport": f"{fpga_id}/transport.json",
                "transport_rtl": f"{fpga_id}/transport_schedule.sv",
                "virtual_anchors": f"{fpga_id}/virtual_anchors.json",
                "virtual_xdc_template": (
                    f"{fpga_id}/virtual_anchors.xdc.template"
                ),
            }
            for fpga_id in fpga_ids
        ],
        "lane_map": "lane_map.json",
        "runtime_controller_rtl": "virtual_runtime_controller.sv",
        **(
            {
                "cut_mode": "static-exact-combinational",
                "qualification": "exact-boundary-materialization-pass",
                "semantic_contract_schema": exact_contract["schema"],
                "semantic_contract_sha256": exact_contract_sha256,
            }
            if exact_contract is not None
            else {}
        ),
        **(
            {
                "pin_plan": "pin_plan.json",
                "position_hints": "position_hints.json",
            }
            if pin_plan is not None
            else {}
        ),
    }
    return {
        "manifest": manifest,
        "lane_map": lane_map,
        "netlists": fpga_netlists,
        "transports": transports,
        "anchors": anchors,
    }


def schedule_routes(schedule: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    routes = schedule.get("routes")
    if isinstance(routes, list):
        return routes
    raise ValidationError(
        "schedule.routes metadata is required for Phase 6 netlist splitting"
    )


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
    ports = [
        "  input  logic fabric_clk",
        "  input  logic reset",
        "  input  logic links_ready",
        "  input  logic [SOURCE_COUNT-1:0] source_values",
        "  output logic [SHADOW_COUNT-1:0] shadow_values",
        "  output logic virtual_clock_enable",
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


def anchors_to_xdc_template(anchors: Mapping[str, Any]) -> str:
    lines = [
        "# EmuFlow virtual IO anchors.",
        "# This file is intentionally non-binding until a hardware BSP is selected.",
        "# Replace placeholders through the hardware pin planner; do not source this",
        "# template directly in Vivado.",
        "",
    ]
    for anchor in anchors["anchors"]:
        port = _sv_name(anchor["id"])
        lines.extend(
            [
                f"# anchor={anchor['id']} peer={anchor['peer']} "
                f"lane={anchor['logical_lane']} "
                f"direction={anchor['direction']}",
                f"# set_property PACKAGE_PIN <PACKAGE_PIN> "
                f"[get_ports {{{port}}}]",
                f"# set_property IOSTANDARD <IOSTANDARD> "
                f"[get_ports {{{port}}}]",
                "",
            ]
        )
    return "\n".join(lines)


def _build_virtual_anchors(
    fpga_id: str,
    platform: Platform,
    endpoints: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    links = {link.id: link for link in platform.links}
    grouped: Dict[Tuple[str, str, str, int], List[Mapping[str, Any]]] = (
        defaultdict(list)
    )
    for endpoint in endpoints:
        grouped[
            (
                endpoint["link"],
                endpoint["peer"],
                endpoint["kind"],
                endpoint["lane"],
            )
        ].append(endpoint)
    records = []
    for (link, peer, kind, lane), items in sorted(grouped.items()):
        board_link = links[link]
        if board_link.mode == "serial":
            required_fields = [
                "package_pin_p",
                "package_pin_n",
                "connector",
                "transceiver_site",
            ]
            physical_lane = lane // board_link.payload_bits_per_lane_per_cycle
            bit_within_physical_lane = (
                lane % board_link.payload_bits_per_lane_per_cycle
            )
        else:
            required_fields = ["package_pin", "bank", "iostandard"]
            physical_lane = lane
            bit_within_physical_lane = 0
        records.append(
            {
                "id": f"{link}:{fpga_id}:{kind}:{lane}",
                "link": link,
                "peer": peer,
                "direction": kind,
                "logical_lane": lane,
                "physical_lane": physical_lane,
                "bit_within_physical_lane": bit_within_physical_lane,
                "endpoint_ids": sorted(item["id"] for item in items),
                "slots": sorted(item["slot"] for item in items),
                "placement_class": "virtual_link_io_region",
                "binding_status": "unbound",
                "required_hardware_binding_fields": required_fields,
            }
        )
    required_fields = sorted(
        {
            field
            for record in records
            for field in record["required_hardware_binding_fields"]
        }
    )
    return {
        "schema": VIRTUAL_IO_ANCHORS_SCHEMA,
        "platform": platform.name,
        "fpga": fpga_id,
        "part": next(fpga.part for fpga in platform.fpgas if fpga.id == fpga_id),
        "anchors": records,
        "required_hardware_binding_fields": required_fields,
    }


def validate_split_artifacts(
    ir: EmuIR,
    assignment: Mapping[str, Any],
    schedule: Mapping[str, Any],
    platform: Platform,
    artifacts: Mapping[str, Any],
    pin_plan: Optional[Mapping[str, Any]] = None,
    *,
    reconstruct: bool = True,
) -> Dict[str, Any]:
    if reconstruct:
        expected = build_split_artifacts(
            ir, assignment, schedule, platform, pin_plan
        )
        for key in ("manifest", "lane_map", "netlists", "transports", "anchors"):
            if artifacts.get(key) != expected[key]:
                raise ValidationError(
                    f"split artifact {key!r} does not match independent "
                    "reconstruction"
                )

    instance_counts = Counter(
        instance["id"]
        for netlist in artifacts["netlists"].values()
        for instance in netlist["instances"]
    )
    original_instances = {item["id"] for item in ir.value["instances"]}
    expected_replica_instances = set(
        assignment.get("replication", {}).get(
            "replica_instance_assignment", {}
        )
    )
    expected_instances = original_instances | expected_replica_instances
    if set(instance_counts) != expected_instances or any(
        count != 1 for count in instance_counts.values()
    ):
        raise ValidationError(
            "per-FPGA netlists do not exactly cover original and replica "
            "instances"
        )

    endpoint_ids = {
        endpoint["id"]
        for transport in artifacts["transports"].values()
        for endpoint in transport["endpoints"]
    }
    lane_endpoint_ids = {
        endpoint_id
        for entry in artifacts["lane_map"]["entries"]
        for endpoint_id in (entry["tx_endpoint"], entry["rx_endpoint"])
    }
    if endpoint_ids != lane_endpoint_ids:
        raise ValidationError("logical lane map endpoint agreement failed")

    exact_boundary_count = 0
    hidden_bypass_errors = 0
    exact_contract = assignment.get("semantic_contract")
    if exact_contract is not None:
        from .combinational_cut import semantic_contract_sha256
        from .routing import static_exact_contract_from_assignment

        exact_contract = static_exact_contract_from_assignment(assignment)
        exact_digest = semantic_contract_sha256(exact_contract)
        manifest = artifacts["manifest"]
        if (
            manifest.get("cut_mode") != "static-exact-combinational"
            or manifest.get("qualification")
            != "exact-boundary-materialization-pass"
            or manifest.get("semantic_contract_schema")
            != exact_contract.get("schema")
            or manifest.get("semantic_contract_sha256") != exact_digest
        ):
            raise ValidationError(
                "exact split manifest is not bound to the semantic contract"
            )
        node_by_net = {
            item["net"]: item for item in exact_contract["cut_nodes"]
        }
        consumers: Dict[Tuple[str, str], List[str]] = defaultdict(list)
        for segment in exact_contract["logic_segments"]:
            source_cut = segment.get("source_cut_net")
            fpga = segment.get("fpga")
            if isinstance(source_cut, str) and isinstance(fpga, str):
                consumers[(source_cut, fpga)].append(segment["id"])
        boundary_ids = set()
        for transport in artifacts["transports"].values():
            if (
                transport.get("semantic_contract_sha256") != exact_digest
                or transport.get("shadow_reset_value") != 0
                or transport.get("startup_policy")
                != "first-post-reset-frame-current-arrivals"
            ):
                raise ValidationError(
                    "exact transport is missing reset/contract binding"
                )
            for endpoint in transport["endpoints"]:
                boundary = endpoint.get("boundary_identity")
                if (
                    not isinstance(boundary, str)
                    or boundary in boundary_ids
                    or endpoint.get("semantic_contract_sha256") != exact_digest
                    or endpoint.get("preserve") is not True
                ):
                    raise ValidationError(
                        "exact transport boundary identity is invalid"
                    )
                boundary_ids.add(boundary)
                exact_boundary_count += 1
                if endpoint["kind"] == "tx":
                    source_entry = endpoint["signal"].startswith("net:")
                    expected_role = (
                        "cut_source" if source_entry else "route_tree_relay"
                    )
                    expected_segments = (
                        list(node_by_net[endpoint["net"]]["source_segment_ids"])
                        if source_entry
                        else []
                    )
                    if (
                        endpoint.get("source_role") != expected_role
                        or endpoint.get("source_logic_segment_ids")
                        != expected_segments
                    ):
                        raise ValidationError(
                            "exact TX boundary source binding is invalid"
                        )
                elif endpoint.get("consumer_logic_segment_ids") != sorted(
                    consumers.get((endpoint["net"], endpoint["fpga"]), [])
                ):
                    raise ValidationError(
                        "exact RX boundary consumer binding is invalid"
                    )
        for netlist in artifacts["netlists"].values():
            if netlist.get("semantic_contract_sha256") != exact_digest:
                raise ValidationError(
                    "exact split netlist lacks semantic contract binding"
                )
            for segment in netlist["nets"]:
                if segment["source_kind"] != "transport_shadow":
                    continue
                drivers = segment["drivers"]
                if (
                    len(drivers) != 1
                    or not isinstance(drivers[0].get("instance"), str)
                    or not drivers[0]["instance"].startswith("__emuflow_rx_")
                    or drivers[0].get("port") != "shadow_out"
                ):
                    hidden_bypass_errors += 1
        if hidden_bypass_errors:
            raise ValidationError(
                "exact split netlists contain a hidden cross-FPGA bypass"
            )

    cut_sink_endpoints = sum(
        sum(
            endpoint["instance"] is not None
            for endpoint in segment["sinks"]
        )
        for netlist in artifacts["netlists"].values()
        for segment in netlist["nets"]
        if segment["source_kind"] == "transport_shadow"
    )
    expected_cut_sink_endpoints = sum(
        cut["sink_endpoints"] for cut in assignment["cut_nets"]
    )
    if cut_sink_endpoints != expected_cut_sink_endpoints:
        raise ValidationError(
            "split netlists do not realize the effective replicated cut "
            f"endpoints: expected {expected_cut_sink_endpoints}, got "
            f"{cut_sink_endpoints}"
        )

    return {
        "status": "pass",
        "fpgas": len(platform.fpgas),
        "instances": len(original_instances),
        "replica_instances": len(expected_replica_instances),
        "physical_instances": len(expected_instances),
        "net_segments": sum(
            len(netlist["nets"])
            for netlist in artifacts["netlists"].values()
        ),
        "transport_endpoints": len(endpoint_ids),
        "scheduled_hops": len(schedule["entries"]),
        "lane_map_entries": len(artifacts["lane_map"]["entries"]),
        "cut_sink_endpoints": cut_sink_endpoints,
        "virtual_anchors": sum(
            len(value["anchors"]) for value in artifacts["anchors"].values()
        ),
        "unbound_package_pins": sum(
            len(value["anchors"]) for value in artifacts["anchors"].values()
        ),
        "instance_coverage_errors": 0,
        "endpoint_agreement_errors": 0,
        **(
            {
                "cut_mode": "static-exact-combinational",
                "exact_boundary_identities": exact_boundary_count,
                "hidden_cross_fpga_bypass_errors": hidden_bypass_errors,
                "startup_uninitialized_shadow_reads": 0,
            }
            if exact_contract is not None
            else {}
        ),
    }
