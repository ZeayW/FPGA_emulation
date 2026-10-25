"""Independent RWRoute TSV oracle for explicit tests and qualification only.

This module is not installed and has no production dispatch or fallback.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from emuflow.xilinx_placement import XILINX_PLACEMENT_SCHEMA
from emuflow.xilinx_physical_handoff import validate_xilinx_physical_handoff_pair
from emuflow.xilinx_rwroute import _sha256, _select_module, _primitive_parameter, _is_clock_sink_pin

class _TsvSpool:
    """Append a large route contract without retaining its text in memory."""

    def __init__(self, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp",
            dir=output_path.parent,
        )
        self.output_path = output_path
        self.temporary_path = Path(name)
        self.stream = os.fdopen(descriptor, "w", encoding="utf-8")
        self.committed = False

    def append(self, row: str) -> None:
        self.stream.write(row)
        self.stream.write("\n")

    def commit(self) -> None:
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        os.replace(self.temporary_path, self.output_path)
        self.committed = True

    def __del__(self) -> None:
        if not self.committed:
            try:
                if not self.stream.closed:
                    self.stream.close()
                self.temporary_path.unlink(missing_ok=True)
            except Exception:
                pass


def _logical_pin(port: str, index: int, width: int) -> str:
    return port if width == 1 else f"{port}[{index}]"


def _lut_clock_candidate_pins(
    cells: Iterable[Tuple[str, Mapping[str, Any]]],
) -> Dict[str, Tuple[int, int, int]]:
    """Return the three routed bits of each canonical LUT2 candidate."""

    pins: Dict[str, Tuple[int, int, int]] = {}
    for name, cell in cells:
        if cell.get("type") != "LUT2":
            continue
        parameters = cell.get("parameters", {})
        connections = cell.get("connections", {})
        if (
            not isinstance(parameters, Mapping)
            or not isinstance(connections, Mapping)
            or _primitive_parameter(parameters, "INIT") != "8"
        ):
            continue
        values = []
        for port in ("I0", "I1", "O"):
            bits = connections.get(port)
            if not isinstance(bits, list) or len(bits) != 1 or not isinstance(bits[0], int):
                break
            values.append(bits[0])
        if len(values) != 3:
            continue
        pins[name] = (values[0], values[1], values[2])
    return pins


def _qualified_lut_clock_gates(
    cells: Mapping[str, Any],
    endpoints: Mapping[int, List[Tuple[str, str, str]]],
    candidate_pins: Optional[Mapping[str, Tuple[int, int, int]]] = None,
) -> Set[str]:
    """Recognize the canonical, already-latched NVDLA clock-gate pattern."""

    raw_pins = (
        _lut_clock_candidate_pins(cells.items())
        if candidate_pins is None else candidate_pins
    )
    candidates: Set[str] = set()
    pins: Dict[str, Tuple[int, int, int]] = {}
    for name, values in raw_pins.items():
        enable_drivers = [
            (instance, pin)
            for instance, pin, role in endpoints.get(values[1], [])
            if role == "driver"
        ]
        if (
            len(enable_drivers) != 1
            or cells.get(enable_drivers[0][0], {}).get("type") != "FDRE"
            or enable_drivers[0][1] != "Q"
        ):
            continue
        candidates.add(name)
        pins[name] = values

    qualified: Set[str] = set()
    for name in sorted(candidates):
        root_bit, _enable_bit, output_bit = pins[name]
        root_drivers = [
            (instance, pin)
            for instance, pin, role in endpoints.get(root_bit, [])
            if role == "driver"
        ]
        if root_drivers and not (
            len(root_drivers) == 1
            and root_drivers[0][0] in candidates
            and root_drivers[0][1] == "O"
        ):
            continue
        output_sinks = [
            (instance, pin)
            for instance, pin, role in endpoints.get(output_bit, [])
            if role == "sink"
        ]
        if not output_sinks or any(
            not _is_clock_sink_pin(pin)
            and not (instance in candidates and pin == "I0")
            for instance, pin in output_sinks
        ):
            continue
        qualified.add(name)

    # Preserve only closed gate chains. A malformed link invalidates its
    # neighbors transitively instead of being partially rewritten.
    while True:
        invalid = set()
        for name in qualified:
            root_bit, _enable_bit, output_bit = pins[name]
            root_drivers = [
                instance
                for instance, pin, role in endpoints.get(root_bit, [])
                if role == "driver" and pin == "O"
            ]
            downstream = [
                instance
                for instance, pin, role in endpoints.get(output_bit, [])
                if role == "sink" and pin == "I0"
            ]
            if any(instance not in qualified for instance in [*root_drivers, *downstream]):
                invalid.add(name)
        if not invalid:
            break
        qualified.difference_update(invalid)
    return qualified


def _candidate_lut_clock_bits(
    candidate_pins: Mapping[str, Tuple[int, int, int]],
) -> Set[int]:
    """Return only nets needed to qualify canonical LUT clock gates."""

    return {
        bit for values in candidate_pins.values() for bit in values
    }


def export_rwroute_reference(
    mapped_path: Path,
    packed_path: Path,
    placement_path: Path,
    output_path: Path,
    *,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    placement_value: Optional[Mapping[str, Any]] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    resource_limits: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    export_started = time.perf_counter()
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    packed = read_json(packed_path) if packed_value is None else packed_value
    placement = (
        read_json(placement_path)
        if placement_value is None else placement_value
    )
    validate_xilinx_physical_handoff_pair(packed, placement)
    digests = {
        "mapped_sha256": _sha256(mapped_path),
        "packed_sha256": _sha256(packed_path),
        "placement_sha256": _sha256(placement_path),
    } if source_sha256 is None else dict(source_sha256)
    if set(digests) != {
        "mapped_sha256", "packed_sha256", "placement_sha256"
    } or any(
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in digests.values()
    ):
        raise ValidationError("RWRoute source digests are invalid")
    if packed.get("schema") != PACKED_SITE_NETLIST_SCHEMA:
        raise ValidationError("PackedSiteNetlist header is invalid")
    if placement.get("schema") != XILINX_PLACEMENT_SCHEMA:
        raise ValidationError("Xilinx placement header is invalid")
    top = packed.get("top")
    if not isinstance(top, str):
        raise ValidationError("PackedSiteNetlist top is invalid")
    return _export_rwroute_python_reference(
        mapped, packed, placement, packed_path, placement_path, output_path,
        digests=digests, virtual_io_contract=virtual_io_contract,
        resource_limits=resource_limits, export_started=export_started,
    )


def _export_rwroute_python_reference(
    mapped, packed, placement, packed_path, placement_path, output_path, *,
    digests, virtual_io_contract=None, resource_limits=None, export_started=None,
):
    """Explicit reference/qualification owner; never a native error fallback.

    The caller supplies already checked headers and source identities. A large
    differential may use the old typed cursors without constructing full JSON.
    """
    if export_started is None:
        export_started = time.perf_counter()
    top = packed["top"]
    module = _select_module(mapped, top)
    cells = module["cells"]
    binding_started = time.perf_counter()
    indexed_physical_cells = getattr(mapped, "iter_physical_cells", None)
    indexed_physical_pins = getattr(mapped, "iter_physical_pins_by_net", None)
    indexed_physical_route_endpoints = getattr(
        mapped, "iter_physical_route_endpoints_by_net", None
    )
    indexed_cells_by_type = getattr(mapped, "iter_cells_by_type", None)
    indexed_cell_metadata = getattr(mapped, "cell_type_and_parameters", None)
    indexed_clock_gate_source = getattr(
        mapped, "iter_qualified_lut_clock_gates", None
    )
    use_typed_physical = (
        packed_path == placement_path
        and callable(indexed_physical_cells)
        and callable(indexed_physical_pins)
    )
    use_typed_route_ordinals = (
        use_typed_physical and callable(indexed_physical_route_endpoints)
    )
    physical: Optional[Dict[str, Tuple[str, str]]] = None
    if not use_typed_physical:
        physical = {
            assignment["instance"]: (
                assignment.get("site", entry["site"]), assignment["bel"]
            )
            for entry in placement["clusters"]
            for assignment in entry["assignments"]
        }
        if set(physical) != {
            assignment["instance"]
            for cluster in packed["clusters"]
            for assignment in cluster["assignments"]
        }:
            raise ValidationError(
                "placement and packing cell ownership disagree"
            )

    def physical_rows() -> Iterator[Tuple[str, str, str, str]]:
        if use_typed_physical:
            assert callable(indexed_physical_cells)
            yield from indexed_physical_cells(packed_path)
            return
        assert physical is not None
        for name in sorted(physical):
            site, bel = physical[name]
            yield name, str(cells[name]["type"]), site, bel
    # RapidWright models LUT6_2 as a transformed primitive and deliberately
    # rejects direct createAndPlaceCell(LUT6_2).  Preserve one logical/resource
    # cell in EmuFlow, but lower it at this provider boundary to the documented
    # shared A5LUT/A6LUT physical pair.  Only the four functional route-through
    # pins may carry non-constant nets.
    pin_bindings: Dict[Tuple[str, str], Tuple[str, str]] = {}
    expanded_lut6_2 = 0
    transformed_dsp48e2 = 0
    logical_cell_count = 0
    io_rows: Dict[str, Mapping[str, Any]] = {}
    io_by_bit: Dict[int, Tuple[str, str, str]] = {}
    if virtual_io_contract is not None:
        if (
            virtual_io_contract.get("schema")
            not in {
                "emuflow.virtual-io-physical-contract/v1",
                "emuflow.virtual-io-physical-contract/v2",
            }
            or virtual_io_contract.get("status") != "pass"
        ):
            raise ValidationError("RWRoute virtual I/O contract is invalid")
        ports = module.get("ports")
        if not isinstance(ports, Mapping):
            raise ValidationError("mapped top-port inventory is invalid")
        for index, entry in enumerate(virtual_io_contract.get("entries", [])):
            if not isinstance(entry, Mapping):
                raise ValidationError("RWRoute virtual I/O entry is invalid")
            direction = entry.get("direction")
            logical_port = entry.get("logical_port")
            logical_bit = entry.get("logical_bit")
            port = ports.get(logical_port) if isinstance(logical_port, str) else None
            bits = port.get("bits") if isinstance(port, Mapping) else None
            expected_direction = "output" if direction == "tx" else "input"
            if (
                direction not in {"tx", "rx"}
                or isinstance(logical_bit, bool)
                or not isinstance(logical_bit, int)
                or not isinstance(bits, list)
                or logical_bit < 0
                or logical_bit >= len(bits)
                or port.get("direction") != expected_direction
                or len(bits) != entry.get("logical_port_width")
                or not isinstance(bits[logical_bit], int)
            ):
                raise ValidationError(
                    "RWRoute virtual I/O logical top-port binding is invalid"
                )
            mapped_bit = bits[logical_bit]
            role = "sink" if direction == "tx" else "driver"
            key = f"\0IO{index}"
            if mapped_bit in io_by_bit:
                raise ValidationError("one mapped bit has multiple physical I/O bindings")
            io_by_bit[mapped_bit] = (
                key, "I" if direction == "tx" else "O", role
            )
            io_rows[key] = entry
    def route_keys() -> Iterator[str]:
        for name, cell_type, _site, _bel in physical_rows():
            if cell_type == "LUT6_2":
                yield f"{name}\0O5"
                yield f"{name}\0O6"
            else:
                yield name

    io_ordinals = {
        name: index for index, name in enumerate(sorted(io_rows))
    }
    # Compatibility inputs have no typed pin relation carrying the route-cell
    # ordinal, so retain the historical name lookup for that explicit path.
    # Production physical handoff inputs derive ordinals from ordered cursors
    # and never allocate this graph-sized dictionary.
    safe = None if use_typed_route_ordinals else {
        name: index
        for index, name in enumerate(heapq.merge(
            sorted(io_rows), route_keys()
        ))
    }

    def safe_name(name: str, ordinal: Optional[int] = None) -> str:
        if ordinal is None:
            if safe is None:
                ordinal = io_ordinals.get(name)
                if ordinal is None:
                    raise ValidationError(
                        f"cell {name!r} has no RWRoute route ordinal"
                    )
            else:
                ordinal = safe[name]
        return f"c{ordinal}"
    binding_seconds = time.perf_counter() - binding_started
    cell_rows_started = time.perf_counter()
    lines = _TsvSpool(output_path)
    for field, count in sorted((resource_limits or {}).items()):
        if field not in {"lut", "ff", "bram18k", "dsp48", "carry8", "uram288"} or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError("RWRoute resource budget is invalid")
        lines.append(f"META\tresource_limit_{field}\t{count}")
    for row in (
        f"META\tpart\t{placement['part']}",
        f"META\tmapped_sha256\t{digests['mapped_sha256']}",
        f"META\tpacked_sha256\t{digests['packed_sha256']}",
        f"META\tplacement_sha256\t{digests['placement_sha256']}",
    ):
        lines.append(row)
    if virtual_io_contract is not None:
        lines.append(
            "META\tvirtual_io_contract_sha256\t"
            + hashlib.sha256(
                json.dumps(
                    virtual_io_contract, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
        )
    next_route_ordinal = len(io_ordinals)
    for name, cell_type, site, bel in physical_rows():
        logical_cell_count += 1
        if "\0" in name:
            raise ValidationError(
                f"cell name {name!r} contains the reserved NUL separator"
            )
        cell_ordinal = next_route_ordinal
        if cell_type == "LUT6_2":
            match = re.fullmatch(r"([A-H])6LUT", bel)
            if match is None:
                raise ValidationError(
                    f"LUT6_2 cell {name!r} is not assigned to a 6LUT BEL"
                )
            letter = match.group(1)
            o5_key = f"{name}\0O5"
            o6_key = f"{name}\0O6"
            if not use_typed_route_ordinals:
                pin_bindings[(name, "I0")] = (o5_key, "I0")
                pin_bindings[(name, "O5")] = (o5_key, "O")
                pin_bindings[(name, "I1")] = (o6_key, "I1")
                pin_bindings[(name, "O6")] = (o6_key, "O")
            expanded_lut6_2 += 1
            route_rows = (
                (
                    o5_key, f"{name}$physical_o5", "LUT5",
                    f"{letter}5LUT", cell_ordinal,
                ),
                (
                    o6_key, f"{name}$physical_o6", "LUT6",
                    f"{letter}6LUT", cell_ordinal + 1,
                ),
            )
            next_route_ordinal += 2
        else:
            if cell_type == "DSP48E2":
                transformed_dsp48e2 += 1
            route_rows = ((name, name, cell_type, bel, cell_ordinal),)
            next_route_ordinal += 1
        for (
            route_name, display_name, route_type, route_bel, route_ordinal
        ) in route_rows:
            if any(character in display_name for character in "\t\r\n"):
                raise ValidationError(
                    f"cell name {display_name!r} is not TSV-safe"
                )
            lines.append(
                f"CELL\t{safe_name(route_name, route_ordinal)}\t{display_name}"
                f"\t{route_type}"
                f"\t{site}\t{route_bel}"
            )
        if cell_type in {"RAMB18E2", "RAMB36E2"}:
            if use_typed_physical and callable(indexed_cell_metadata):
                parameter_type, parameters = indexed_cell_metadata(name)
                if parameter_type != cell_type:
                    raise ValidationError(
                        f"hard primitive {name!r} type projection disagrees"
                    )
            else:
                parameters = cells[name].get("parameters", {})
            if not isinstance(parameters, Mapping):
                raise ValidationError(
                    f"hard primitive {name!r} parameters are invalid"
                )
            for parameter in (
                "READ_WIDTH_A", "READ_WIDTH_B", "WRITE_WIDTH_A",
                "WRITE_WIDTH_B", "DOA_REG", "DOB_REG", "WRITE_MODE_A",
                "WRITE_MODE_B", "RAM_MODE",
            ):
                value = _primitive_parameter(parameters, parameter)
                if value is not None:
                    lines.append(
                        f"PARAM\t{safe_name(name, cell_ordinal)}"
                        f"\t{parameter}\t{value}"
                    )
    for key, entry in sorted(io_rows.items()):
        fields = [
            "IOB", safe_name(key, io_ordinals[key]), str(entry["id"]),
            str(entry["direction"]),
            str(entry["p"]["port"]), str(entry["n"]["port"]),
            str(entry["p"]["package_pin"]), str(entry["n"]["package_pin"]),
            str(entry["p"]["site"]), str(entry["n"]["site"]),
            str(entry["logical_port"]), str(entry["logical_bit"]),
            str(entry["iostandard"]),
        ]
        if any(any(character in value for character in "\t\r\n") for value in fields):
            raise ValidationError("RWRoute virtual I/O entry is not TSV-safe")
        lines.append("\t".join(fields))

    cell_rows_seconds = time.perf_counter() - cell_rows_started
    endpoint_index_started = time.perf_counter()
    indexed_pin_source = getattr(mapped, "iter_pins_by_net", None)

    def physical_pin_rows(
    ) -> Iterator[Tuple[int, str, str, int, int, str, str]]:
        if use_typed_physical:
            assert callable(indexed_physical_pins)
            yield from indexed_physical_pins(packed_path)
            return
        assert callable(indexed_pin_source)
        assert physical is not None
        for bit, name, port, port_bit, port_width, direction in (
            indexed_pin_source()
        ):
            binding = physical.get(name)
            if binding is not None:
                yield (
                    bit, name, port, port_bit, port_width, direction,
                    binding[0],
                )

    def physical_route_pin_rows(
    ) -> Iterator[
        Tuple[
            int, str, Optional[str], str, int, int, str, str,
            Optional[int], str,
        ]
    ]:
        if use_typed_route_ordinals:
            assert callable(indexed_physical_route_endpoints)
            yield from indexed_physical_route_endpoints(
                packed_path, ordinal_offset=len(io_ordinals)
            )
            return
        for bit, name, port, port_bit, port_width, direction, site in (
            physical_pin_rows()
        ):
            logical_pin = _logical_pin(port, port_bit, port_width)
            yield (
                bit, name, None, port, port_bit, port_width, direction, site,
                None, logical_pin,
            )

    typed_clock_gate_qualification = (
        use_typed_physical and callable(indexed_clock_gate_source)
    )
    if typed_clock_gate_qualification:
        endpoints = None
        clock_endpoints = None
        clock_route_ordinals: Dict[str, int] = {}
        candidate_pins = None
        endpoint_dataflow = "typed-mapped-physical-join"
    elif use_typed_physical or callable(indexed_pin_source):
        candidate_pins = _lut_clock_candidate_pins(
            indexed_cells_by_type("LUT2")
            if callable(indexed_cells_by_type) else cells.items()
        )
        candidate_bits = _candidate_lut_clock_bits(candidate_pins)
        clock_endpoints: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
        clock_route_ordinals: Dict[str, int] = {}
        if candidate_bits:
            for (
                bit, name, _cell_type, port, port_bit, port_width, direction, _site,
                route_ordinal, _route_pin,
            ) in physical_route_pin_rows():
                if bit not in candidate_bits:
                    continue
                role = (
                    "driver" if direction in {"output", "inout"} else "sink"
                )
                clock_endpoints[bit].append(
                    (name, _logical_pin(port, port_bit, port_width), role)
                )
                if route_ordinal is not None:
                    clock_route_ordinals.setdefault(name, route_ordinal)
            for bit, endpoint in io_by_bit.items():
                if bit in candidate_bits:
                    clock_endpoints[bit].append(endpoint)
        endpoints = None
        endpoint_dataflow = (
            "typed-mapped-physical-join"
            if use_typed_physical else "typed-mapped-pin-stream"
        )
    else:
        endpoints = defaultdict(list)
        assert physical is not None
        for name in sorted(physical):
            cell = cells[name]
            directions = cell.get("port_directions", {})
            for port, bits in cell.get("connections", {}).items():
                if not isinstance(bits, list):
                    raise ValidationError(
                        f"cell {name!r} port {port!r} is invalid"
                    )
                direction = directions.get(port)
                if direction not in {"input", "output", "inout"}:
                    raise ValidationError(
                        f"cell {name!r} port direction is invalid"
                    )
                role = (
                    "driver" if direction in {"output", "inout"} else "sink"
                )
                for index, bit in enumerate(bits):
                    if isinstance(bit, int):
                        endpoints[bit].append(
                            (name, _logical_pin(port, index, len(bits)), role)
                        )
        for bit, endpoint in io_by_bit.items():
            endpoints[bit].append(endpoint)
        clock_endpoints = endpoints
        clock_route_ordinals = {}
        candidate_pins = _lut_clock_candidate_pins(cells.items())
        endpoint_dataflow = "compatibility-python-index"

    endpoint_index_seconds = time.perf_counter() - endpoint_index_started
    clock_validation_started = time.perf_counter()
    if typed_clock_gate_qualification:
        assert callable(indexed_clock_gate_source)
        clock_route_ordinals = dict(indexed_clock_gate_source(
            packed_path,
            ordinal_offset=len(io_ordinals),
            io_endpoints=(
                (bit, endpoint[0], endpoint[1], endpoint[2])
                for bit, endpoint in sorted(io_by_bit.items())
            ),
        ))
        clock_gates: Mapping[str, int] | Set[str] = clock_route_ordinals
    else:
        assert clock_endpoints is not None
        assert candidate_pins is not None
        clock_gates = _qualified_lut_clock_gates(
            cells, clock_endpoints, candidate_pins
        )
    for name in sorted(clock_gates):
        lines.append(
            f"CLOCK_GATE\t{safe_name(name, clock_route_ordinals.get(name))}"
            "\tI0\tI1\tO\t"
            "lut2-init-1000-fdre-latched-enable-v1"
        )

    clock_validation_seconds = time.perf_counter() - clock_validation_started
    net_rows_started = time.perf_counter()
    included = 0
    excluded = 0
    carry_exit_owners: Dict[Tuple[str, int], Tuple[str, str]] = {}

    def indexed_endpoint_groups(
    ) -> Iterator[
        Tuple[
            int,
            List[Tuple[str, str, str, str, Optional[int], str, Optional[str]]],
        ]
    ]:
        cell_groups: List[
            Tuple[str, str, str, str, Optional[int], str, Optional[str]]
        ] = []
        current_bit: Optional[int] = None
        io_items = iter(sorted(io_by_bit.items()))
        current_io = next(io_items, None)

        def emit_cell_group(
            bit: int,
            values: List[
                Tuple[str, str, str, str, Optional[int], str, Optional[str]]
            ],
        ) -> Iterator[
            Tuple[
                int,
                List[Tuple[str, str, str, str, Optional[int], str, Optional[str]]],
            ]
        ]:
            nonlocal current_io
            while current_io is not None and current_io[0] < bit:
                io_name, io_pin, io_role = current_io[1]
                yield current_io[0], [(
                    io_name, io_pin, io_role,
                    str(io_rows[io_name]["p"]["site"]),
                    io_ordinals[io_name], io_pin, None,
                )]
                current_io = next(io_items, None)
            if current_io is not None and current_io[0] == bit:
                io_name, io_pin, io_role = current_io[1]
                values.append((
                    io_name, io_pin, io_role,
                    str(io_rows[io_name]["p"]["site"]),
                    io_ordinals[io_name], io_pin, None,
                ))
                current_io = next(io_items, None)
            yield bit, values

        for (
            bit, name, cell_type, port, port_bit, port_width, direction, site,
            route_ordinal, route_pin,
        ) in physical_route_pin_rows():
            if current_bit is not None and bit != current_bit:
                yield from emit_cell_group(current_bit, cell_groups)
                cell_groups = []
            current_bit = bit
            role = "driver" if direction in {"output", "inout"} else "sink"
            cell_groups.append(
                (
                    name, _logical_pin(port, port_bit, port_width), role,
                    site, route_ordinal, route_pin, cell_type,
                )
            )
        if current_bit is not None:
            yield from emit_cell_group(current_bit, cell_groups)
        while current_io is not None:
            io_name, io_pin, io_role = current_io[1]
            yield current_io[0], [(
                io_name, io_pin, io_role,
                str(io_rows[io_name]["p"]["site"]),
                io_ordinals[io_name], io_pin, None,
            )]
            current_io = next(io_items, None)

    def compatibility_endpoint_groups(
    ) -> Iterator[
        Tuple[
            int,
            List[Tuple[str, str, str, str, Optional[int], str, Optional[str]]],
        ]
    ]:
        assert endpoints is not None
        assert physical is not None
        for bit in sorted(endpoints):
            yield bit, [
                (
                    name, pin, role,
                    physical[name][0]
                    if name in physical
                    else str(io_rows[name]["p"]["site"]),
                    None, pin,
                    str(cells[name]["type"]) if name in cells else None,
                )
                for name, pin, role in endpoints[bit]
            ]

    endpoint_groups = (
        indexed_endpoint_groups()
        if endpoints is None else compatibility_endpoint_groups()
    )
    for bit, values in endpoint_groups:
        drivers = [value for value in values if value[2] == "driver"]
        sinks = [value for value in values if value[2] == "sink"]
        net_name = f"n{bit}"
        boundary_clock = any(
            _is_clock_sink_pin(value[1]) for value in sinks
        )
        if boundary_clock and drivers and not (
            len(drivers) == 1
            and drivers[0][0] in clock_gates
            and drivers[0][1] == "O"
        ):
            raise ValidationError(
                f"routed clock bit {bit} is not driven by a qualified "
                "LUT2 INIT=1000 gate with an FDRE-latched enable"
            )
        if len(drivers) != 1 or not sinks:
            if not drivers and sinks and boundary_clock:
                lines.append(
                    f"EXCLUDED\t{net_name}\tboundary_clock"
                    "\tideal-boundary-clock"
                )
            else:
                reason = (
                    "boundary_or_driverless"
                    if not drivers else "multiple_driver_or_sinkless"
                )
                lines.append(f"EXCLUDED\t{net_name}\t{reason}")
            excluded += 1
            continue
        sites = {value[3] for value in [drivers[0], *sinks]}
        touches_clock_gate = any(
            value[0] in clock_gates for value in [drivers[0], *sinks]
        )
        if len(sites) == 1 and not touches_clock_gate:
            lines.append(f"EXCLUDED\t{net_name}\tintra_site")
            excluded += 1
            continue
        (
            driver_cell, driver_pin, _driver_role, _driver_site,
            _driver_ordinal, _driver_route_pin, driver_cell_type,
        ) = drivers[0]
        if driver_cell_type is None and driver_cell in cells:
            driver_cell_type = cells[driver_cell].get("type")
        if driver_cell_type == "CARRY8":
            match = re.fullmatch(r"(CO|O)\[(\d+)\]", driver_pin)
            if match is not None and int(match.group(2)) < 7:
                exit_key = (driver_cell, int(match.group(2)))
                previous = carry_exit_owners.setdefault(
                    exit_key, (match.group(1), net_name)
                )
                if previous[0] != match.group(1):
                    raise ValidationError(
                        "CARRY8 shared site exit is multiply driven: "
                        f"{driver_cell!r} bit {exit_key[1]} routes both "
                        f"{previous[0]} ({previous[1]}) and "
                        f"{match.group(1)} ({net_name}); normalize the "
                        "observable O prefix before physical routing"
                    )
        kind = "clock" if boundary_clock else "signal"
        lines.append(f"NET\t{net_name}\t{kind}")
        for (
            name, pin, role, _site, route_ordinal, typed_route_pin, _cell_type
        ) in [drivers[0], *sinks]:
            if use_typed_route_ordinals:
                route_name = name
                route_pin = typed_route_pin
            else:
                route_name, route_pin = pin_bindings.get(
                    (name, pin), (name, pin)
                )
            if (
                route_ordinal is None
                and (safe is None or route_name not in safe)
            ):
                raise ValidationError(
                    f"cell {name!r} pin {pin!r} has no RWRoute physical binding"
                )
            lines.append(
                f"PIN\t{net_name}\t{safe_name(route_name, route_ordinal)}"
                f"\t{route_pin}\t{role}"
            )
        included += 1
    net_rows_seconds = time.perf_counter() - net_rows_started
    commit_started = time.perf_counter()
    lines.commit()
    commit_seconds = time.perf_counter() - commit_started
    return {
        "status": "pass", "cells": next_route_ordinal,
        "logical_cells": logical_cell_count,
        "virtual_io_cells": len(io_rows),
        "expanded_lut6_2_cells": expanded_lut6_2,
        "transformed_dsp48e2_cells": transformed_dsp48e2,
        "qualified_clock_gates": len(clock_gates),
        "physical_cells": next_route_ordinal + 7 * transformed_dsp48e2,
        "routable_nets": included,
        "excluded_nets": excluded, "output": str(output_path),
        "runtime_breakdown": {
            "schema": "emuflow.rwroute-export-runtime/v1",
            "binding_seconds": binding_seconds,
            "cell_and_io_rows_seconds": cell_rows_seconds,
            "endpoint_index_seconds": endpoint_index_seconds,
            "clock_validation_seconds": clock_validation_seconds,
            "net_rows_seconds": net_rows_seconds,
            "atomic_commit_seconds": commit_seconds,
            "total_seconds": time.perf_counter() - export_started,
            "dataflow": {
                "route_cells": "generated-from-physical-binding",
                "physical_bindings": (
                    "typed-mapped-physical-join"
                    if use_typed_physical else "compatibility-python-map"
                ),
                "safe_names": (
                    "typed-ordinal-cursor"
                    if use_typed_route_ordinals
                    else "ordered-merge-integer-index"
                ),
                "endpoints": endpoint_dataflow,
                "tsv": "streamed-atomic-spool",
            },
        },
    }

