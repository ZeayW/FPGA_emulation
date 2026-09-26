"""Compact, provider-neutral physical-macro contracts for Xilinx mapping.

The contract records only cells that participate in an indivisible site macro
or a dedicated inter-site cascade.  It is derived from mapped connectivity and
contains no placement candidate, device-site inventory, or copied netlist.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ValidationError
from .io import read_json, write_json
from .xilinx_packing import (
    CASCADE_PORTS,
    DUAL_OUTPUT_LUT_TYPE,
    LUT_TYPES,
    MUX_TYPES,
    _bits,
    _cascade_signal,
    _derive_cascade_chains,
    _mux_input_driver,
    _net_drivers,
    _pack_mux_cone,
    _select_module,
)
from .xilinx_primitives import (
    XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
    audit_xilinx_mapped_json,
)


XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA = (
    "emuflow.xilinx-physical-macro-contract/v1"
)
XILINX_PHYSICAL_MACRO_VALIDATION_SCHEMA = (
    "emuflow.xilinx-physical-macro-contract-validation/v1"
)

_REQUIRED_SITE_MACRO_TYPES = {
    "CARRY8", DUAL_OUTPUT_LUT_TYPE, *MUX_TYPES, "RAMB18E2"
}
_CASCADE_RESOURCES = {
    "CARRY8": "slice-carry-column",
    "DSP48E2": "dsp-column",
    "RAMB18E2": "bram-column",
    "RAMB36E2": "bram-column",
    "URAM288": "uram-column",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signal_contract(bits: Sequence[Any], context: str) -> Dict[str, Any]:
    if not bits or any(
        not isinstance(bit, int) or isinstance(bit, bool) for bit in bits
    ):
        raise ValidationError(f"{context} is not an explicit mapped signal")
    payload = json.dumps(list(bits), separators=(",", ":")).encode("utf-8")
    return {"width": len(bits), "sha256": hashlib.sha256(payload).hexdigest()}


def _selection(index: Optional[int], width: int) -> Dict[str, Any]:
    if index is None:
        return {"kind": "all"}
    normalized = index if index >= 0 else width + index
    if not 0 <= normalized < width:
        raise ValidationError("physical-macro pin selection is out of range")
    return {"kind": "bit", "index": normalized}


def _endpoint(
    instance: str, port: str, index: Optional[int], width: int
) -> Dict[str, Any]:
    return {
        "instance": instance,
        "port": port,
        "selection": _selection(index, width),
    }


def _connection(
    *,
    kind: str,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    signal: Sequence[Any],
    label: str,
) -> Dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "source": dict(source),
        "target": dict(target),
        "signal": _signal_contract(signal, f"{kind} {label!r}"),
    }


def _driver_endpoints(
    cells: Mapping[str, Mapping[str, Any]],
) -> Dict[int, Tuple[str, str, int]]:
    """Return exact driver pins after the shared packer checks uniqueness."""

    owners = _net_drivers(cells)
    endpoints: Dict[int, Tuple[str, str, int]] = {}
    for instance, cell in sorted(cells.items()):
        directions = cell.get("port_directions")
        connections = cell.get("connections")
        if not isinstance(directions, Mapping) or not isinstance(connections, Mapping):
            raise ValidationError(f"mapped cell {instance!r} lacks port metadata")
        if set(directions) != set(connections):
            raise ValidationError(f"mapped cell {instance!r} has incomplete port metadata")
        for port, direction in sorted(directions.items()):
            bits = connections[port]
            if not isinstance(bits, list):
                raise ValidationError(f"mapped cell {instance!r} port {port!r} is invalid")
            if direction != "output":
                continue
            for index, bit in enumerate(bits):
                if isinstance(bit, int) and not isinstance(bit, bool):
                    if owners.get(bit) != instance or bit in endpoints:
                        raise ValidationError("mapped driver endpoint ownership is inconsistent")
                    endpoints[bit] = (instance, str(port), index)
    return endpoints


def _carry_site_macros(
    cells: Mapping[str, Mapping[str, Any]],
    drivers: Mapping[Any, str],
    endpoints: Mapping[int, Tuple[str, str, int]],
) -> List[Dict[str, Any]]:
    macros = []
    claimed_adapters = set()
    for macro_index, carry in enumerate(sorted(
        name for name, cell in cells.items() if cell.get("type") == "CARRY8"
    )):
        di_bits = _bits(cells[carry], "DI")
        s_bits = _bits(cells[carry], "S")
        if len(di_bits) != 8 or len(s_bits) != 8:
            raise ValidationError(f"CARRY8 {carry!r} must expose eight DI and S bits")
        members = [{
            "instance": carry,
            "cell_type": "CARRY8",
            "role": "carry-root",
            "physical_role": "CARRY8",
        }]
        connections = []
        for bit_index, letter in enumerate("ABCDEFGH"):
            di, select = di_bits[bit_index], s_bits[bit_index]
            adapter = drivers.get(di)
            if (
                adapter is None
                or adapter != drivers.get(select)
                or cells[adapter].get("type") != DUAL_OUTPUT_LUT_TYPE
                or endpoints.get(di) != (adapter, "O5", 0)
                or endpoints.get(select) != (adapter, "O6", 0)
            ):
                raise ValidationError(
                    f"CARRY8 {carry!r} bit {bit_index} has incomplete LUT6_2 O5/O6 topology"
                )
            if adapter in claimed_adapters:
                raise ValidationError("a LUT6_2 adapter belongs to multiple carry roles")
            claimed_adapters.add(adapter)
            members.append({
                "instance": adapter,
                "cell_type": DUAL_OUTPUT_LUT_TYPE,
                "role": f"bit-{bit_index}-adapter",
                "physical_role": f"{letter}6LUT",
            })
            connections.extend([
                _connection(
                    kind="dedicated-intra-site",
                    label=f"DI[{bit_index}]",
                    source=_endpoint(adapter, "O5", 0, 1),
                    target=_endpoint(carry, "DI", bit_index, 8),
                    signal=(di,),
                ),
                _connection(
                    kind="dedicated-intra-site",
                    label=f"S[{bit_index}]",
                    source=_endpoint(adapter, "O6", 0, 1),
                    target=_endpoint(carry, "S", bit_index, 8),
                    signal=(select,),
                ),
            ])
        macros.append({
            "id": f"carry8-lut6_2-{macro_index:06d}",
            "kind": "carry8-lut6_2",
            "members": members,
            "connections": connections,
            "relative_site": {
                "relation": "same-site",
                "resource": "ultrascaleplus-slice",
                "exclusive": True,
            },
        })
    return macros


def _mux_site_macros(
    cells: Mapping[str, Mapping[str, Any]],
    drivers: Mapping[Any, str],
    endpoints: Mapping[int, Tuple[str, str, int]],
) -> List[Dict[str, Any]]:
    muxes = {name for name, cell in cells.items() if cell.get("type") in MUX_TYPES}
    children = set()
    for name in sorted(muxes):
        for port in ("I0", "I1"):
            driver = _mux_input_driver(cells, drivers, name, port)
            if driver in muxes:
                children.add(driver)
    roots = sorted(muxes - children)
    macros = []
    claimed = set()
    for macro_index, root in enumerate(roots):
        assignments = _pack_mux_cone(root, cells, drivers)
        instances = [assignment["instance"] for assignment in assignments]
        if len(instances) != len(set(instances)):
            raise ValidationError(f"mux cone {root!r} reuses a physical member")
        overlap = set(instances).intersection(claimed)
        if overlap:
            raise ValidationError(
                "mux cones share physical members: " + ", ".join(sorted(overlap))
            )
        claimed.update(instances)
        members = []
        for assignment in assignments:
            instance = assignment["instance"]
            cell_type = assignment["cell_type"]
            role = (
                "root"
                if instance == root
                else "lut-leaf"
                if cell_type in LUT_TYPES
                else f"{cell_type.lower()}-stage"
            )
            members.append({
                "instance": instance,
                "cell_type": cell_type,
                "role": role,
                "physical_role": assignment["bel"],
            })
        connections = []
        for assignment in assignments:
            target = assignment["instance"]
            if cells[target].get("type") not in MUX_TYPES:
                continue
            for port in ("I0", "I1"):
                bits = _bits(cells[target], port)
                if len(bits) != 1 or not isinstance(bits[0], int):
                    raise ValidationError(f"mux {target!r} port {port!r} is incomplete")
                source_instance = _mux_input_driver(cells, drivers, target, port)
                source_pin = endpoints.get(bits[0])
                if source_pin is None or source_pin[0] != source_instance:
                    raise ValidationError(
                        f"mux {target!r} port {port!r} has no exact driver pin"
                    )
                source_width = len(_bits(cells[source_instance], source_pin[1]))
                connections.append(_connection(
                    kind="dedicated-intra-site",
                    label=f"{target}.{port}",
                    source=_endpoint(
                        source_instance, source_pin[1], source_pin[2], source_width
                    ),
                    target=_endpoint(target, port, 0, 1),
                    signal=bits,
                ))
        macros.append({
            "id": f"mux-cone-{macro_index:06d}",
            "kind": f"{cells[root]['type'].lower()}-cone",
            "members": members,
            "connections": connections,
            "relative_site": {
                "relation": "same-site",
                "resource": "ultrascaleplus-slice",
                "exclusive": True,
            },
        })
    unowned = sorted(muxes - claimed)
    if unowned:
        raise ValidationError("unrooted or incomplete mux topology: " + ", ".join(unowned))
    return macros


def _ramb18_site_macros(
    cells: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """Record RAMB18 site-mode demand without inventing a pairing.

    Connectivity does not select which two independent RAMB18E2 instances
    should share a RAMB36 site or which instance occupies its lower/upper
    half.  Each instance therefore receives an auditable occupancy contract;
    a later device adapter must assign one of the two legal half roles.
    """

    names = sorted(
        name for name, cell in cells.items() if cell.get("type") == "RAMB18E2"
    )
    macros = []
    for macro_index, name in enumerate(names):
        macros.append({
            "id": f"ramb18-half-site-occupancy-{macro_index:06d}",
            "kind": "ramb18-half-site-occupancy",
            "members": [{
                "instance": name,
                "cell_type": "RAMB18E2",
                "role": "half-site-occupant",
                "physical_roles": ["RAMB18E2_L", "RAMB18E2_U"],
            }],
            "connections": [],
            "relative_site": {
                "relation": "site-mode-occupancy",
                "resource": "ultrascaleplus-ramb36-site",
                "mode": "RAMB18E2-half",
                "capacity_per_site": 2,
                "device_binding": "adapter_required",
            },
        })
    return macros


def _reject_partial_cascade_overlap(
    cells: Mapping[str, Mapping[str, Any]],
) -> None:
    """Reject malformed dedicated connections hidden by exact tuple matching."""

    for cell_type, ports in CASCADE_PORTS.items():
        names = sorted(
            name for name, cell in cells.items() if cell.get("type") == cell_type
        )
        for label, output_port, input_port, index in ports:
            outputs = [
                (name, _cascade_signal(cells[name], output_port, index))
                for name in names
            ]
            inputs = [
                (name, _cascade_signal(cells[name], input_port, None))
                for name in names
            ]
            for source, output in outputs:
                if not output:
                    continue
                output_set = set(output)
                for target, input_signal in inputs:
                    if source == target or not input_signal:
                        continue
                    if output_set.intersection(input_signal) and output != input_signal:
                        raise ValidationError(
                            f"{cell_type} cascade {label!r} has a partial-width "
                            f"connection from {source!r} to {target!r}"
                        )


def _cascade_contracts(
    cells: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    _reject_partial_cascade_overlap(cells)
    base = _derive_cascade_chains(cells)
    contracts = []
    for chain in base:
        cell_type = chain["cell_type"]
        port_by_label = {
            label: (output_port, input_port, index)
            for label, output_port, input_port, index in CASCADE_PORTS[cell_type]
        }
        instances = chain["instances"]
        members = []
        for index, instance in enumerate(instances):
            role = (
                "head" if index == 0
                else "tail" if index == len(instances) - 1
                else f"body-{index:06d}"
            )
            members.append({
                "instance": instance, "cell_type": cell_type, "role": role,
            })
        connections = []
        for link in chain["links"]:
            source = link["source"]
            target = link["target"]
            for label in link["signals"]:
                output_port, input_port, index = port_by_label[label]
                output_bits = _bits(cells[source], output_port)
                source_signal = _cascade_signal(cells[source], output_port, index)
                target_signal = _cascade_signal(cells[target], input_port, None)
                if not source_signal or source_signal != target_signal:
                    raise ValidationError(
                        f"{cell_type} cascade {source!r}->{target!r} label {label!r} is incomplete"
                    )
                connections.append(_connection(
                    kind="dedicated-inter-site",
                    label=label,
                    source=_endpoint(source, output_port, index, len(output_bits)),
                    target=_endpoint(
                        target, input_port, None, len(_bits(cells[target], input_port))
                    ),
                    signal=source_signal,
                ))
        contracts.append({
            "id": chain["id"],
            "kind": "dedicated-cascade-chain",
            "cell_type": cell_type,
            "members": members,
            "connections": connections,
            "relative_sites": {
                "relation": "ordered-native-cascade",
                "resource": _CASCADE_RESOURCES[cell_type],
                "connectivity_order": instances,
                "requirements": [
                    "same-physical-column",
                    "same-slr",
                    "consecutive-native-cascade-neighbors",
                ],
                "device_adjacency": {
                    "status": "adapter_required",
                    "reason": "ArchitectureDB has no typed native-cascade adjacency graph",
                },
            },
        })
    return contracts


def _derive_contract(
    mapped_path: Path, *, top: Optional[str]
) -> Dict[str, Any]:
    audit = audit_xilinx_mapped_json(mapped_path, top=top)
    source = read_json(mapped_path)
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped Xilinx cells are invalid")
    drivers = _net_drivers(cells)
    endpoints = _driver_endpoints(cells)
    site_macros = [
        *_carry_site_macros(cells, drivers, endpoints),
        *_mux_site_macros(cells, drivers, endpoints),
        *_ramb18_site_macros(cells),
    ]
    site_owners: Dict[str, str] = {}
    for macro in site_macros:
        for member in macro["members"]:
            instance = member["instance"]
            if instance in site_owners:
                raise ValidationError(
                    f"physical macro instance {instance!r} has multiple site owners"
                )
            site_owners[instance] = macro["id"]
    required_site_owned = {
        name
        for name, cell in cells.items()
        if cell.get("type") in _REQUIRED_SITE_MACRO_TYPES
    }
    if not required_site_owned.issubset(site_owners):
        missing = sorted(required_site_owned - set(site_owners))
        raise ValidationError(
            "physical macro topology is incomplete; unowned instances: "
            + ", ".join(missing)
        )

    cascades = _cascade_contracts(cells)
    cascade_owners: Dict[str, str] = {}
    for chain in cascades:
        for member in chain["members"]:
            instance = member["instance"]
            if instance in cascade_owners:
                raise ValidationError(
                    f"cascade instance {instance!r} belongs to multiple chains"
                )
            cascade_owners[instance] = chain["id"]
    kind_counts: Dict[str, int] = {}
    for macro in site_macros:
        kind_counts[macro["kind"]] = kind_counts.get(macro["kind"], 0) + 1
    return {
        "schema": XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA,
        "status": "pass",
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "top": selected_top,
        "source": {
            "mapped_json_sha256": audit["mapped_json_sha256"],
            "primitive_library_sha256": audit["primitive_library_sha256"],
        },
        "policy": {
            "provider": "mapped-connectivity-derived-physical-macros-v1",
            "placement_or_search": "absent",
            "unknown_or_incomplete_topology": "fail-closed",
            "device_adjacency_without_typed_graph": "adapter_required",
        },
        "site_macros": site_macros,
        "cascade_chains": cascades,
        "ownership": {
            "site_macros": [
                {"instance": instance, "owner": site_owners[instance]}
                for instance in sorted(site_owners)
            ],
            "cascade_chains": [
                {"instance": instance, "owner": cascade_owners[instance]}
                for instance in sorted(cascade_owners)
            ],
        },
        "summary": {
            "site_macros": len(site_macros),
            "site_macro_kinds": dict(sorted(kind_counts.items())),
            "site_owned_instances": len(site_owners),
            "cascade_chains": len(cascades),
            "cascade_owned_instances": len(cascade_owners),
            "recorded_connections": sum(
                len(item["connections"]) for item in [*site_macros, *cascades]
            ),
        },
    }


def build_xilinx_physical_macro_contract(
    mapped_path: Path, *, top: Optional[str] = None
) -> Dict[str, Any]:
    """Derive the source-sealed physical-macro contract in memory.

    Placement providers need the exact same connectivity-derived macro view as
    the standalone contract writer.  Returning that canonical value directly
    avoids writing and reparsing a diagnostic-sized JSON file in the placement
    hot path.
    """

    return _derive_contract(mapped_path, top=top)


def _validate_shape(value: Mapping[str, Any], cells: Mapping[str, Any]) -> None:
    expected_keys = {
        "schema", "status", "mapping_profile", "top", "source", "policy",
        "site_macros", "cascade_chains", "ownership", "summary",
    }
    if set(value) != expected_keys:
        raise ValidationError("physical macro contract top-level fields are invalid")
    if (
        value.get("schema") != XILINX_PHYSICAL_MACRO_CONTRACT_SCHEMA
        or value.get("status") != "pass"
        or value.get("mapping_profile") != XILINX_ULTRASCALEPLUS_OPEN_PROFILE
    ):
        raise ValidationError("physical macro contract header is invalid")
    site_macros = value.get("site_macros")
    cascades = value.get("cascade_chains")
    if not isinstance(site_macros, list) or not isinstance(cascades, list):
        raise ValidationError("physical macro contract records are invalid")
    seen_ids = set()
    site_owners = set()
    for record, chain in [
        *((item, False) for item in site_macros),
        *((item, True) for item in cascades),
    ]:
        if not isinstance(record, Mapping) or not isinstance(record.get("id"), str):
            raise ValidationError("physical macro record is invalid")
        if record["id"] in seen_ids:
            raise ValidationError("physical macro record id is duplicated")
        seen_ids.add(record["id"])
        members = record.get("members")
        connections = record.get("connections")
        if (
            not isinstance(members, list)
            or not members
            or not isinstance(connections, list)
        ):
            raise ValidationError("physical macro member/connectivity record is invalid")
        local = set()
        for member in members:
            if not isinstance(member, Mapping):
                raise ValidationError("physical macro member is invalid")
            instance = member.get("instance")
            if (
                not isinstance(instance, str)
                or instance in local
                or cells.get(instance, {}).get("type") != member.get("cell_type")
                or not isinstance(member.get("role"), str)
            ):
                raise ValidationError("physical macro member ownership is invalid")
            local.add(instance)
            if not chain:
                if instance in site_owners:
                    raise ValidationError("physical macro site ownership overlaps")
                site_owners.add(instance)
        for connection in connections:
            if not isinstance(connection, Mapping):
                raise ValidationError("physical macro connection is invalid")
            signal = connection.get("signal")
            if (
                not isinstance(signal, Mapping)
                or not isinstance(signal.get("width"), int)
                or signal["width"] <= 0
                or not isinstance(signal.get("sha256"), str)
                or len(signal["sha256"]) != 64
            ):
                raise ValidationError("physical macro signal certificate is invalid")
            for endpoint_name in ("source", "target"):
                endpoint = connection.get(endpoint_name)
                if (
                    not isinstance(endpoint, Mapping)
                    or endpoint.get("instance") not in local
                ):
                    raise ValidationError("physical macro connection escapes its membership")


def derive_xilinx_physical_macro_contract(
    mapped_json: Path,
    output: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Derive and seal physical macros without packing or placement."""

    value = _derive_contract(mapped_json, top=top)
    source = read_json(mapped_json)
    _selected_top, module = _select_module(source, value["top"])
    _validate_shape(value, module["cells"])
    write_json(output, value, compact=True)
    validate_xilinx_physical_macro_contract(mapped_json, output, top=value["top"])
    return value


def validate_xilinx_physical_macro_contract(
    mapped_json: Path,
    contract_path: Path,
    *,
    top: Optional[str] = None,
) -> Dict[str, Any]:
    """Re-derive ownership and topology and reject any contract divergence."""

    value = read_json(contract_path)
    if not isinstance(value, Mapping):
        raise ValidationError("physical macro contract is invalid")
    source = read_json(mapped_json)
    selected_top, module = _select_module(source, top)
    cells = module.get("cells")
    if not isinstance(cells, dict):
        raise ValidationError("mapped Xilinx cells are invalid")
    _validate_shape(value, cells)
    expected = _derive_contract(mapped_json, top=selected_top)
    if value != expected:
        raise ValidationError("physical macro contract differs from mapped connectivity")
    return {
        "schema": XILINX_PHYSICAL_MACRO_VALIDATION_SCHEMA,
        "status": "pass",
        "top": selected_top,
        "site_macros": len(value["site_macros"]),
        "cascade_chains": len(value["cascade_chains"]),
        "contract_sha256": _sha256(contract_path),
    }
