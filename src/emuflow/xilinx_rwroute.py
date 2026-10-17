"""Compact RWRoute adapter and independent route-certificate checker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from .errors import ValidationError
from .io import file_sha256, read_json
from .xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from .xilinx_placement import XILINX_PLACEMENT_SCHEMA


XILINX_ROUTE_DB_SCHEMA = "emuflow.xilinx-route-db/v1"
XILINX_ROUTE_DB_STREAM_SCHEMA = "emuflow.xilinx-route-db/v2"
XILINX_ROUTE_NET_PAYLOAD_FORMAT = "jsonl-object/v1"
XILINX_ROUTE_PIP_PAYLOAD_FORMAT = "jsonl-array/v1"
RAPIDWRIGHT_TIMING_DATA_REVISION = (
    "127f55cd704c277372697e699f1559e1cdc91f34"
)
RAPIDWRIGHT_TIMING_DATA_SHA256 = {
    "intersite_delay_terms.txt": (
        "3b122837c4a1b5f3c212fc6353bee3a0854a204f2f69e2bc1cac4a2a1f9d7333"
    ),
    "intrasite_delay_terms.txt": (
        "ff08ce9041da649f2bf886d900f033dd23de8ad54eb4db55d37ce032dc0ec0a0"
    ),
}
RAPIDWRIGHT_DEVICE_DATA_MD5 = {
    "data/parts.db": "58dd6f20c37798322b6904a8a786a3de",
    "data/devices/virtexuplus/xcvu19p_db.dat": (
        "5ad01490fe442f360aa67d7dfe0fa1c3"
    ),
}


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


def _sha256(path: Path) -> str:
    return file_sha256(path)


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_rapidwright_device_data(root: Path) -> Dict[str, str]:
    observed: Dict[str, str] = {}
    for relative, expected_md5 in RAPIDWRIGHT_DEVICE_DATA_MD5.items():
        path = root / relative
        actual_md5 = _md5(path) if path.is_file() else "missing"
        if actual_md5 != expected_md5:
            raise ValidationError(
                "RapidWright device data is missing or does not match the "
                f"pinned XCVU19P provider: {path}"
            )
        observed[relative] = actual_md5
    return observed


def _route_payload_path(
    manifest_path: Path,
    descriptor: object,
    *,
    field: str,
    expected_format: str,
) -> tuple[Path, str, int]:
    if not isinstance(descriptor, Mapping):
        raise ValidationError(f"XilinxRouteDB payloads.{field} is invalid")
    relative = descriptor.get("path")
    digest = descriptor.get("sha256")
    records = descriptor.get("records")
    if (
        descriptor.get("format") != expected_format
        or not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or Path(relative).name != relative
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or isinstance(records, bool)
        or not isinstance(records, int)
        or records < 0
    ):
        raise ValidationError(f"XilinxRouteDB payloads.{field} is invalid")
    path = manifest_path.parent / relative
    if not path.is_file():
        raise ValidationError(f"XilinxRouteDB payloads.{field} is missing")
    return path, digest, records


def _hashed_json_lines(
    path: Path,
    *,
    expected_sha256: str,
    expected_records: int,
    field: str,
) -> Iterator[object]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            if not raw.endswith(b"\n"):
                raise ValidationError(
                    f"XilinxRouteDB payloads.{field} has a truncated record"
                )
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValidationError(
                    f"XilinxRouteDB payloads.{field} record {count} is invalid"
                ) from error
            count += 1
            yield value
    if count != expected_records:
        raise ValidationError(
            f"XilinxRouteDB payloads.{field} record count disagrees"
        )
    if digest.hexdigest() != expected_sha256:
        raise ValidationError(
            f"XilinxRouteDB payloads.{field} digest disagrees"
        )


def iter_xilinx_route_nets(
    path: Path,
    route: Mapping[str, Any],
    *,
    include_pips: bool,
) -> Iterator[Dict[str, Any]]:
    """Iterate one route net at a time without loading the v2 payloads.

    The legacy v1 object remains readable for immutable historical evidence.
    New producers emit a small manifest plus line-delimited net and PIP
    payloads.  Validation consumes both streams exactly once; timing binding
    reads only the much smaller net/end-point stream and never scans PIPs.
    """

    schema = route.get("schema")
    if schema == XILINX_ROUTE_DB_SCHEMA:
        nets = route.get("nets")
        if not isinstance(nets, list):
            raise ValidationError("XilinxRouteDB net collection is invalid")
        for net in nets:
            if not isinstance(net, dict):
                raise ValidationError("XilinxRouteDB net record is invalid")
            if include_pips:
                yield net
            else:
                yield {key: value for key, value in net.items() if key != "pips"}
        return
    if schema != XILINX_ROUTE_DB_STREAM_SCHEMA:
        raise ValidationError("XilinxRouteDB header is invalid")
    payloads = route.get("payloads")
    if not isinstance(payloads, Mapping) or set(payloads) != {"nets", "pips"}:
        raise ValidationError("XilinxRouteDB payload manifest is invalid")
    nets_path, nets_digest, nets_count = _route_payload_path(
        path,
        payloads["nets"],
        field="nets",
        expected_format=XILINX_ROUTE_NET_PAYLOAD_FORMAT,
    )
    net_stream = _hashed_json_lines(
        nets_path,
        expected_sha256=nets_digest,
        expected_records=nets_count,
        field="nets",
    )
    if not include_pips:
        for index, net in enumerate(net_stream):
            if not isinstance(net, dict):
                raise ValidationError(
                    f"XilinxRouteDB payloads.nets record {index} is invalid"
                )
            pip_count = net.get("pip_count")
            if isinstance(pip_count, bool) or not isinstance(pip_count, int) or pip_count < 0:
                raise ValidationError(
                    f"XilinxRouteDB payloads.nets record {index} pip count is invalid"
                )
            yield net
        return
    pips_path, pips_digest, pips_count = _route_payload_path(
        path,
        payloads["pips"],
        field="pips",
        expected_format=XILINX_ROUTE_PIP_PAYLOAD_FORMAT,
    )
    pip_stream = iter(_hashed_json_lines(
        pips_path,
        expected_sha256=pips_digest,
        expected_records=pips_count,
        field="pips",
    ))
    consumed_pips = 0
    for index, net in enumerate(net_stream):
        if not isinstance(net, dict):
            raise ValidationError(
                f"XilinxRouteDB payloads.nets record {index} is invalid"
            )
        pip_count = net.get("pip_count")
        if isinstance(pip_count, bool) or not isinstance(pip_count, int) or pip_count < 0:
            raise ValidationError(
                f"XilinxRouteDB payloads.nets record {index} pip count is invalid"
            )
        net_name = net.get("net")
        pips: List[Dict[str, Any]] = []
        for _ in range(pip_count):
            try:
                record = next(pip_stream)
            except StopIteration as error:
                raise ValidationError(
                    f"XilinxRouteDB payloads.pips ends inside net {net_name!r}"
                ) from error
            consumed_pips += 1
            if (
                not isinstance(record, list)
                or len(record) != 8
                or record[0] != net_name
                or any(not isinstance(record[field], str) or not record[field]
                       for field in range(1, 6))
                or not isinstance(record[6], bool)
                or not isinstance(record[7], bool)
            ):
                raise ValidationError(
                    f"XilinxRouteDB payloads.pips record {consumed_pips - 1} is invalid"
                )
            pips.append({
                "tile": record[1],
                "start_wire": record[2],
                "end_wire": record[3],
                "start_node": record[4],
                "end_node": record[5],
                "bidirectional": record[6],
                "reversed": record[7],
            })
        value = dict(net)
        value.pop("pip_count", None)
        value["pips"] = pips
        yield value
    try:
        next(pip_stream)
    except StopIteration:
        pass
    else:
        raise ValidationError("XilinxRouteDB payloads.pips has unbound records")
    if consumed_pips != pips_count:
        raise ValidationError("XilinxRouteDB payloads.pips record count disagrees")


class _PipOwnership:
    """Bounded-memory global PIP ownership checker."""

    def __init__(self, expected_pips: int) -> None:
        self.count = 0
        self._memory: Optional[Dict[Tuple[str, str, str], str]] = (
            {} if expected_pips <= 250_000 else None
        )
        self._database: Optional[sqlite3.Connection] = None
        self._database_path: Optional[Path] = None
        if self._memory is None:
            descriptor, name = tempfile.mkstemp(
                prefix="emuflow-route-pips-", suffix=".sqlite3"
            )
            os.close(descriptor)
            self._database_path = Path(name)
            self._database = sqlite3.connect(name)
            self._database.execute("PRAGMA journal_mode=OFF")
            self._database.execute("PRAGMA synchronous=OFF")
            self._database.execute("PRAGMA temp_store=FILE")
            self._database.execute(
                "CREATE TABLE ownership ("
                "tile TEXT NOT NULL, first_wire TEXT NOT NULL, "
                "second_wire TEXT NOT NULL, net TEXT NOT NULL, "
                "PRIMARY KEY (tile, first_wire, second_wire)) WITHOUT ROWID"
            )

    def claim_many(
        self, keys: Iterable[Tuple[str, str, str]], net_name: str
    ) -> None:
        records = [(tile, first, second, net_name) for tile, first, second in keys]
        if self._memory is not None:
            for key in (record[:3] for record in records):
                previous = self._memory.get(key)
                if previous is not None and previous != net_name:
                    raise ValidationError(f"routing conflict on PIP {key}")
                self._memory[key] = net_name
            self.count = len(self._memory)
            return
        assert self._database is not None
        before = self._database.total_changes
        try:
            self._database.executemany(
                "INSERT INTO ownership VALUES (?, ?, ?, ?)", records
            )
        except sqlite3.IntegrityError as error:
            self._database.rollback()
            raise ValidationError(
                f"routing conflict while claiming PIPs for {net_name!r}"
            ) from error
        self.count += self._database.total_changes - before

    def close(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None
        if self._database_path is not None:
            self._database_path.unlink(missing_ok=True)
            self._database_path = None

    def __del__(self) -> None:
        self.close()


def _select_module(mapped: Mapping[str, Any], top: str) -> Mapping[str, Any]:
    modules = mapped.get("modules")
    module = modules.get(top) if isinstance(modules, Mapping) else None
    if not isinstance(module, Mapping) or not isinstance(
        module.get("cells"), Mapping
    ):
        raise ValidationError(f"mapped JSON has no valid top module {top!r}")
    return module


def _logical_pin(port: str, index: int, width: int) -> str:
    return port if width == 1 else f"{port}[{index}]"


def _primitive_parameter(parameters: Mapping[str, Any], name: str) -> Optional[str]:
    """Return a compact RapidWright property value for a routed hard primitive.

    Yosys JSON writes integer parameters as binary strings.  RWRoute needs the
    decoded width/register value before the primitive is placed because the
    physical pin expansion of a RAMB36 write-enable depends on TDP versus SDP
    mode.  Large INIT payloads are deliberately excluded from this hot path.
    """
    value = parameters.get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        raise ValidationError(f"primitive parameter {name!r} has invalid type")
    if value and set(value) <= {"0", "1"}:
        return str(int(value, 2))
    if any(character in value for character in "\t\r\n"):
        raise ValidationError(f"primitive parameter {name!r} is not TSV-safe")
    return value


def _qualified_lut_clock_gates(
    cells: Mapping[str, Any],
    endpoints: Mapping[int, List[Tuple[str, str, str]]],
) -> Set[str]:
    """Recognize the canonical, already-latched NVDLA clock-gate pattern."""
    candidates: Set[str] = set()
    pins: Dict[str, Tuple[int, int, int]] = {}
    for name, cell in cells.items():
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
        pins[name] = (values[0], values[1], values[2])

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
            pin not in {"C", "CLK"}
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


def export_rwroute_input(
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
) -> Dict[str, Any]:
    mapped = read_json(mapped_path) if mapped_value is None else mapped_value
    packed = read_json(packed_path) if packed_value is None else packed_value
    placement = (
        read_json(placement_path)
        if placement_value is None else placement_value
    )
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
    module = _select_module(mapped, top)
    cells = module["cells"]
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
        raise ValidationError("placement and packing cell ownership disagree")
    # RapidWright models LUT6_2 as a transformed primitive and deliberately
    # rejects direct createAndPlaceCell(LUT6_2).  Preserve one logical/resource
    # cell in EmuFlow, but lower it at this provider boundary to the documented
    # shared A5LUT/A6LUT physical pair.  Only the four functional route-through
    # pins may carry non-constant nets.
    route_cells: Dict[str, Tuple[str, str, str, str]] = {}
    pin_bindings: Dict[Tuple[str, str], Tuple[str, str]] = {}
    expanded_lut6_2 = 0
    transformed_dsp48e2 = 0
    for name in sorted(physical):
        site, bel = physical[name]
        cell_type = cells[name]["type"]
        if cell_type != "LUT6_2":
            route_cells[name] = (name, cell_type, site, bel)
            if cell_type == "DSP48E2":
                transformed_dsp48e2 += 1
            continue
        match = re.fullmatch(r"([A-H])6LUT", bel)
        if match is None:
            raise ValidationError(
                f"LUT6_2 cell {name!r} is not assigned to a 6LUT BEL"
            )
        letter = match.group(1)
        o5_key = f"{name}\0O5"
        o6_key = f"{name}\0O6"
        route_cells[o5_key] = (f"{name}$physical_o5", "LUT5", site, f"{letter}5LUT")
        route_cells[o6_key] = (f"{name}$physical_o6", "LUT6", site, f"{letter}6LUT")
        pin_bindings[(name, "I0")] = (o5_key, "I0")
        pin_bindings[(name, "O5")] = (o5_key, "O")
        pin_bindings[(name, "I1")] = (o6_key, "I1")
        pin_bindings[(name, "O6")] = (o6_key, "O")
        expanded_lut6_2 += 1
    io_rows: Dict[str, Mapping[str, Any]] = {}
    io_by_bit: Dict[int, Tuple[str, str, str]] = {}
    if virtual_io_contract is not None:
        if (
            virtual_io_contract.get("schema")
            != "emuflow.virtual-io-physical-contract/v1"
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
    safe = {
        name: f"c{index}"
        for index, name in enumerate(sorted([*route_cells, *io_rows]))
    }
    lines = _TsvSpool(output_path)
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
    for route_name in sorted(route_cells):
        display_name, cell_type, site, bel = route_cells[route_name]
        if any(character in display_name for character in "\t\r\n"):
            raise ValidationError(f"cell name {display_name!r} is not TSV-safe")
        lines.append(
            f"CELL\t{safe[route_name]}\t{display_name}\t{cell_type}\t{site}\t{bel}"
        )
        if cell_type in {"RAMB18E2", "RAMB36E2"}:
            parameters = cells[route_name].get("parameters", {})
            if not isinstance(parameters, dict):
                raise ValidationError(
                    f"hard primitive {display_name!r} parameters are invalid"
                )
            for parameter in (
                "READ_WIDTH_A", "READ_WIDTH_B", "WRITE_WIDTH_A",
                "WRITE_WIDTH_B", "DOA_REG", "DOB_REG", "WRITE_MODE_A",
                "WRITE_MODE_B", "RAM_MODE",
            ):
                value = _primitive_parameter(parameters, parameter)
                if value is not None:
                    lines.append(
                        f"PARAM\t{safe[route_name]}\t{parameter}\t{value}"
                    )
    for key, entry in sorted(io_rows.items()):
        fields = [
            "IOB", safe[key], str(entry["id"]), str(entry["direction"]),
            str(entry["p"]["port"]), str(entry["n"]["port"]),
            str(entry["p"]["package_pin"]), str(entry["n"]["package_pin"]),
            str(entry["p"]["site"]), str(entry["n"]["site"]),
            str(entry["logical_port"]), str(entry["logical_bit"]),
            str(entry["iostandard"]),
        ]
        if any(any(character in value for character in "\t\r\n") for value in fields):
            raise ValidationError("RWRoute virtual I/O entry is not TSV-safe")
        lines.append("\t".join(fields))

    endpoints: Dict[int, List[Tuple[str, str, str]]] = defaultdict(list)
    for name in sorted(physical):
        cell = cells[name]
        directions = cell.get("port_directions", {})
        for port, bits in cell.get("connections", {}).items():
            if not isinstance(bits, list):
                raise ValidationError(f"cell {name!r} port {port!r} is invalid")
            direction = directions.get(port)
            if direction not in {"input", "output", "inout"}:
                raise ValidationError(f"cell {name!r} port direction is invalid")
            role = "driver" if direction in {"output", "inout"} else "sink"
            for index, bit in enumerate(bits):
                if isinstance(bit, int):
                    endpoints[bit].append(
                        (name, _logical_pin(port, index, len(bits)), role)
                    )
    for bit, endpoint in io_by_bit.items():
        endpoints[bit].append(endpoint)

    clock_gates = _qualified_lut_clock_gates(cells, endpoints)
    for bit, values in endpoints.items():
        sinks = [value for value in values if value[2] == "sink"]
        if not any(pin in {"C", "CLK"} for _name, pin, _role in sinks):
            continue
        drivers = [value for value in values if value[2] == "driver"]
        if drivers and not (
            len(drivers) == 1
            and drivers[0][0] in clock_gates
            and drivers[0][1] == "O"
        ):
            raise ValidationError(
                f"routed clock bit {bit} is not driven by a qualified "
                "LUT2 INIT=1000 gate with an FDRE-latched enable"
            )
    for name in sorted(clock_gates):
        lines.append(
            f"CLOCK_GATE\t{safe[name]}\tI0\tI1\tO\t"
            "lut2-init-1000-fdre-latched-enable-v1"
        )

    included = 0
    excluded = 0
    carry_exit_owners: Dict[Tuple[str, int], Tuple[str, str]] = {}
    for bit in sorted(endpoints):
        values = endpoints[bit]
        drivers = [value for value in values if value[2] == "driver"]
        sinks = [value for value in values if value[2] == "sink"]
        net_name = f"n{bit}"
        boundary_clock = any(
            pin in {"C", "CLK"} for _cell, pin, _role in sinks
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
        sites = {
            (
                physical[name][0]
                if name in physical
                else str(io_rows[name]["p"]["site"])
            )
            for name, _pin, _role in [drivers[0], *sinks]
        }
        touches_clock_gate = any(
            name in clock_gates
            for name, _pin, _role in [drivers[0], *sinks]
        )
        if len(sites) == 1 and not touches_clock_gate:
            lines.append(f"EXCLUDED\t{net_name}\tintra_site")
            excluded += 1
            continue
        driver_cell, driver_pin, _driver_role = drivers[0]
        if driver_cell in cells and cells[driver_cell].get("type") == "CARRY8":
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
        for name, pin, role in [drivers[0], *sinks]:
            route_name, route_pin = pin_bindings.get(
                (name, pin), (name, pin)
            )
            if route_name not in route_cells and route_name not in io_rows:
                raise ValidationError(
                    f"cell {name!r} pin {pin!r} has no RWRoute physical binding"
                )
            lines.append(
                f"PIN\t{net_name}\t{safe[route_name]}\t{route_pin}\t{role}"
            )
        included += 1
    lines.commit()
    return {
        "status": "pass", "cells": len(safe), "logical_cells": len(physical),
        "virtual_io_cells": len(io_rows),
        "expanded_lut6_2_cells": expanded_lut6_2,
        "transformed_dsp48e2_cells": transformed_dsp48e2,
        "qualified_clock_gates": len(clock_gates),
        "physical_cells": len(safe) + 7 * transformed_dsp48e2,
        "routable_nets": included,
        "excluded_nets": excluded, "output": str(output_path),
    }


def run_rwroute(
    input_path: Path,
    output_path: Path,
    *,
    rapidwright_jar: Path,
    java: Path,
    classes_dir: Path,
    java_source: Path,
    device_data_root: Path,
    timing_data_dir: Path,
    log_path: Optional[Path] = None,
    mapped_path: Optional[Path] = None,
    packed_path: Optional[Path] = None,
    placement_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    route_value_sink: Optional[Dict[str, Any]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    classes_dir.mkdir(parents=True, exist_ok=True)
    runtime_home = classes_dir.parent / "rapidwright-runtime-home"
    runtime_home.mkdir(parents=True, exist_ok=True)
    rapidwright_path = runtime_home / "RapidWright"
    rapidwright_path.mkdir(parents=True, exist_ok=True)
    device_data_md5 = _validate_rapidwright_device_data(device_data_root)
    runtime_data = rapidwright_path / "data"
    provider_data = (device_data_root / "data").resolve()
    if runtime_data.is_symlink():
        if runtime_data.resolve() != provider_data:
            runtime_data.unlink()
    elif runtime_data.exists():
        shutil.rmtree(runtime_data)
    if not runtime_data.exists():
        runtime_data.symlink_to(provider_data, target_is_directory=True)
    runtime_timing_dir = rapidwright_path / "timing" / "ultrascaleplus"
    runtime_timing_dir.mkdir(parents=True, exist_ok=True)
    for name, expected_sha256 in RAPIDWRIGHT_TIMING_DATA_SHA256.items():
        source = timing_data_dir / name
        if not source.is_file() or _sha256(source) != expected_sha256:
            raise ValidationError(
                "RapidWright timing data is missing or does not match pinned "
                f"revision {RAPIDWRIGHT_TIMING_DATA_REVISION}: {source}"
            )
        destination = runtime_timing_dir / name
        if not destination.is_file() or _sha256(destination) != expected_sha256:
            shutil.copyfile(source, destination)
    class_file = classes_dir / "EmuFlowRWRoute.class"
    if not class_file.is_file() or class_file.stat().st_mtime < java_source.stat().st_mtime:
        javac = java.with_name("javac")
        completed = subprocess.run(
            [str(javac), "-cp", str(rapidwright_jar), "-d", str(classes_dir), str(java_source)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
        )
        if completed.returncode != 0:
            raise ValidationError("RWRoute adapter compilation failed:\n" + completed.stdout[-8000:])
    maximum_heap = os.environ.get("EMUFLOW_RAPIDWRIGHT_MAX_HEAP", "32g")
    if re.fullmatch(r"[1-9][0-9]*[gGmM]", maximum_heap) is None:
        raise ValidationError(
            "EMUFLOW_RAPIDWRIGHT_MAX_HEAP must be a positive Java g/m size"
        )
    command = [
        str(java), f"-Xmx{maximum_heap}", f"-Duser.home={runtime_home}",
        "-cp", f"{classes_dir}:{rapidwright_jar}",
        "EmuFlowRWRoute", str(input_path), str(output_path),
        RAPIDWRIGHT_TIMING_DATA_REVISION,
        RAPIDWRIGHT_TIMING_DATA_SHA256["intersite_delay_terms.txt"],
        RAPIDWRIGHT_TIMING_DATA_SHA256["intrasite_delay_terms.txt"],
        device_data_md5["data/parts.db"],
        device_data_md5["data/devices/virtexuplus/xcvu19p_db.dat"],
    ]
    temporary_log = log_path is None
    if temporary_log:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="emuflow-rwroute-", suffix=".log"
        )
        os.close(descriptor)
        route_log_path = Path(temporary_name)
    else:
        route_log_path = log_path
        assert route_log_path is not None
        route_log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with route_log_path.open("w", encoding="utf-8") as route_log:
            completed = subprocess.run(
                command, stdout=route_log, stderr=subprocess.STDOUT,
                text=True, check=False,
                env={**os.environ, "RAPIDWRIGHT_PATH": str(rapidwright_path)},
            )
        with route_log_path.open("rb") as route_log:
            route_log.seek(0, os.SEEK_END)
            route_log.seek(max(0, route_log.tell() - 8000), os.SEEK_SET)
            failure_tail = route_log.read().decode("utf-8", errors="replace")
    finally:
        if temporary_log:
            route_log_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise ValidationError(
            f"RWRoute failed with exit code {completed.returncode}:\n"
            + "\n".join(failure_tail.splitlines()[-80:])
        )
    value = read_json(output_path)
    if not isinstance(value.get("timing"), dict):
        raise ValidationError("RWRoute output lacks its timing qualification")
    report = validate_xilinx_route_db(
        output_path,
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
        mapped_value=mapped_value,
        packed_value=packed_value,
        source_sha256=source_sha256,
        virtual_io_contract=virtual_io_contract,
        _value=value,
    )
    if route_value_sink is not None:
        route_value_sink.clear()
        route_value_sink.update(value)
    return {**report, "output": str(output_path), "log": str(log_path) if log_path else None}


def validate_xilinx_route_db(
    path: Path,
    *,
    mapped_path: Optional[Path] = None,
    packed_path: Optional[Path] = None,
    placement_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    _value: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # RWRoute already materializes a very large in-memory object.  Accepting
    # that exact object internally avoids immediately parsing the same route
    # database a second time before it is sealed.  External callers retain the
    # ordinary path-only validation API.
    value = read_json(path) if _value is None else _value
    if (
        not isinstance(value, dict)
        or value.get("schema")
        not in {XILINX_ROUTE_DB_SCHEMA, XILINX_ROUTE_DB_STREAM_SCHEMA}
    ):
        raise ValidationError("XilinxRouteDB header is invalid")
    if value.get("status") not in {"candidate", "pass"}:
        raise ValidationError("XilinxRouteDB status is invalid")
    if value.get("provider") != "rapidwright-rwroute-2026.1.0":
        raise ValidationError("XilinxRouteDB provider identity is invalid")
    route_cells = value.get("cells")
    materialization = value.get("materialization")
    virtual_io = value.get("virtual_io", [])
    clock_gates = value.get("clock_gates", [])
    if (
        isinstance(route_cells, bool)
        or not isinstance(route_cells, int)
        or route_cells < 0
        or not isinstance(materialization, dict)
        or materialization.get("route_cells") != route_cells
        or not isinstance(virtual_io, list)
        or not isinstance(clock_gates, list)
    ):
        raise ValidationError("XilinxRouteDB materialized route-cell count is invalid")
    physical_cells = materialization.get("physical_cells")
    transformed_dsp48e2 = materialization.get("transformed_dsp48e2_cells")
    dedicated_clock_gates = materialization.get("dedicated_clock_gate_cells", 0)
    router = materialization.get("router")
    if (
        isinstance(physical_cells, bool)
        or not isinstance(physical_cells, int)
        or isinstance(transformed_dsp48e2, bool)
        or not isinstance(transformed_dsp48e2, int)
        or transformed_dsp48e2 < 0
        or transformed_dsp48e2 > route_cells
        or isinstance(dedicated_clock_gates, bool)
        or not isinstance(dedicated_clock_gates, int)
        or dedicated_clock_gates < 0
        or dedicated_clock_gates != len(clock_gates)
        or physical_cells != route_cells + 7 * transformed_dsp48e2
        or router
        != (
            "CUFR-HUS-non-timing-driven-uturn-enabled-"
            "parallel-unroutable-recovery"
        )
    ):
        raise ValidationError("XilinxRouteDB transformed-cell accounting is invalid")
    seen_clock_gate_cells: Set[str] = set()
    seen_clock_gate_sites: Set[str] = set()
    for index, gate in enumerate(clock_gates):
        if (
            not isinstance(gate, dict)
            or set(gate) != {
                "safe_cell", "logical_cell", "logical_type", "physical_type",
                "physical_site", "physical_bel", "ce_type", "qualification",
            }
            or gate.get("logical_type") != "LUT2"
            or gate.get("physical_type") != "BUFGCE"
            or gate.get("physical_bel") != "BUFCE"
            or gate.get("ce_type") != "ASYNC"
            or gate.get("qualification")
            != "lut2-init-1000-fdre-latched-enable-v1"
            or not isinstance(gate.get("safe_cell"), str)
            or not isinstance(gate.get("logical_cell"), str)
            or not isinstance(gate.get("physical_site"), str)
            or gate["logical_cell"] in seen_clock_gate_cells
            or gate["physical_site"] in seen_clock_gate_sites
        ):
            raise ValidationError(
                f"XilinxRouteDB clock_gates[{index}] is invalid"
            )
        seen_clock_gate_cells.add(gate["logical_cell"])
        seen_clock_gate_sites.add(gate["physical_site"])
    source = value.get("source")
    if not isinstance(source, dict):
        raise ValidationError("XilinxRouteDB source seal is missing")
    expected_paths = {
        "mapped_sha256": mapped_path,
        "packed_sha256": packed_path,
        "placement_sha256": placement_path,
    }
    expected_sources = (
        {key: _sha256(source_path) for key, source_path in expected_paths.items()
         if source_path is not None}
        if source_sha256 is None else dict(source_sha256)
    )
    for key, source_path in expected_paths.items():
        digest = source.get(key)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValidationError(f"XilinxRouteDB source.{key} is invalid")
        if source_path is not None and digest != expected_sources.get(key):
            raise ValidationError(f"XilinxRouteDB source.{key} does not match input")
    expected_io_digest = (
        hashlib.sha256(
            json.dumps(
                virtual_io_contract, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        if virtual_io_contract is not None
        else None
    )
    if source.get("virtual_io_contract_sha256") != expected_io_digest:
        raise ValidationError("XilinxRouteDB virtual I/O source seal disagrees")
    if mapped_path is not None and packed_path is not None:
        mapped = read_json(mapped_path) if mapped_value is None else mapped_value
        packed = read_json(packed_path) if packed_value is None else packed_value
        top = packed.get("top")
        if not isinstance(top, str):
            raise ValidationError("PackedSiteNetlist top is invalid")
        mapped_cells = _select_module(mapped, top)["cells"]
        expected_lut6_2 = sum(
            cell.get("type") == "LUT6_2" for cell in mapped_cells.values()
        )
        expected_dsp48e2 = sum(
            cell.get("type") == "DSP48E2" for cell in mapped_cells.values()
        )
        expected_route_cells = len(mapped_cells) + expected_lut6_2 + len(virtual_io)
        if route_cells != expected_route_cells:
            raise ValidationError("XilinxRouteDB route-cell accounting disagrees")
        if transformed_dsp48e2 != expected_dsp48e2:
            raise ValidationError("XilinxRouteDB DSP48E2 transform count disagrees")
        for gate in clock_gates:
            logical = mapped_cells.get(gate["logical_cell"])
            if (
                not isinstance(logical, Mapping)
                or logical.get("type") != "LUT2"
                or _primitive_parameter(logical.get("parameters", {}), "INIT") != "8"
            ):
                raise ValidationError(
                    "XilinxRouteDB clock-gate logical identity disagrees"
                )
    if virtual_io_contract is None:
        if virtual_io:
            raise ValidationError("XilinxRouteDB has undeclared virtual I/O cells")
    else:
        expected_io = {
            entry["id"]: entry for entry in virtual_io_contract.get("entries", [])
        }
        observed_io = {}
        for index, entry in enumerate(virtual_io):
            if not isinstance(entry, dict) or entry.get("id") in observed_io:
                raise ValidationError(
                    f"XilinxRouteDB virtual I/O record {index} is invalid"
                )
            observed_io[entry["id"]] = entry
        if set(observed_io) != set(expected_io):
            raise ValidationError("XilinxRouteDB virtual I/O coverage disagrees")
        for identity, expected in expected_io.items():
            observed = observed_io[identity]
            required = {
                "id": identity,
                "direction": expected["direction"],
                "logical_port": expected["logical_port"],
                "logical_bit": expected["logical_bit"],
                "iostandard": expected["iostandard"],
                "p_port": expected["p"]["port"],
                "n_port": expected["n"]["port"],
                "p_package_pin": expected["p"]["package_pin"],
                "n_package_pin": expected["n"]["package_pin"],
                "p_site": expected["p"]["site"],
                "n_site": expected["n"]["site"],
            }
            if any(observed.get(key) != expected_value for key, expected_value in required.items()):
                raise ValidationError(
                    f"XilinxRouteDB virtual I/O {identity!r} differs from XDC contract"
                )
            delay = observed.get("route_delay_ps")
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(float(delay))
                or float(delay) < 0.0
            ):
                raise ValidationError(
                    f"XilinxRouteDB virtual I/O {identity!r} delay is invalid"
                )
    input_digest = source.get("rwroute_input_sha256")
    if not isinstance(input_digest, str) or re.fullmatch(r"[0-9a-f]{64}", input_digest) is None:
        raise ValidationError("XilinxRouteDB source.rwroute_input_sha256 is invalid")
    excluded_nets = value.get("excluded_nets")
    summary = value.get("summary")
    if (
        not isinstance(excluded_nets, list)
        or not isinstance(summary, dict)
    ):
        raise ValidationError("XilinxRouteDB net collections are invalid")
    expected_pips = summary.get("pips")
    if (
        isinstance(expected_pips, bool)
        or not isinstance(expected_pips, int)
        or expected_pips < 0
    ):
        raise ValidationError("XilinxRouteDB summary PIP count is invalid")
    used_pips = _PipOwnership(expected_pips)
    seen_nets: Set[str] = set()
    checked_nets = 0
    checked_sinks = 0
    static_nets = 0
    static_sinks = 0
    clock_nets = 0
    nets_with_pips = 0
    timed_endpoints = 0
    maximum_route_delay_ps = 0.0
    for index, net in enumerate(
        iter_xilinx_route_nets(path, value, include_pips=True)
    ):
        context = f"route.nets[{index}]"
        if not isinstance(net, dict) or not isinstance(net.get("net"), str):
            raise ValidationError(f"{context}: invalid net")
        net_name = net["net"]
        if not net_name or net_name in seen_nets:
            raise ValidationError(f"{context}: duplicate or empty net identity")
        seen_nets.add(net_name)
        if net.get("has_gap") is not False:
            raise ValidationError(f"{context}: RWRoute reports gap routing")
        pins = net.get("pins")
        alternate_sources = net.get("alternate_sources", [])
        pips = net.get("pips")
        if (
            not isinstance(pins, list)
            or not isinstance(alternate_sources, list)
            or not isinstance(pips, list)
        ):
            raise ValidationError(f"{context}: pins/PIPs are invalid")
        if not pins:
            raise ValidationError(f"{context}: routed net has no site pins")
        for pin_index, pin in enumerate(pins):
            if (
                not isinstance(pin, dict)
                or not isinstance(pin.get("site"), str)
                or not pin["site"]
                or not isinstance(pin.get("pin"), str)
                or not pin["pin"]
                or not isinstance(pin.get("is_output"), bool)
                or not isinstance(pin.get("node"), str)
                or not pin["node"]
            ):
                raise ValidationError(f"{context}.pins[{pin_index}]: invalid site pin")
        for pin_index, pin in enumerate(alternate_sources):
            if (
                not isinstance(pin, dict)
                or not isinstance(pin.get("site"), str)
                or not pin["site"]
                or not isinstance(pin.get("pin"), str)
                or not pin["pin"]
                or pin.get("is_output") is not True
                or not isinstance(pin.get("node"), str)
                or not pin["node"]
                or "route_delay_ps" in pin
            ):
                raise ValidationError(
                    f"{context}.alternate_sources[{pin_index}]: invalid physical source"
                )
        sources = [pin for pin in pins if pin["is_output"]]
        sinks = [pin for pin in pins if not pin["is_output"]]
        kind = net.get("kind")
        qualification = net.get("qualification")
        static = kind in {"static_gnd", "static_vcc"}
        if kind == "signal":
            if qualification != "ordinary-fabric-signal":
                raise ValidationError(f"{context}: signal qualification is invalid")
        elif kind == "clock":
            if qualification != "dedicated-bufgce-clock":
                raise ValidationError(f"{context}: clock qualification is invalid")
            clock_nets += 1
        elif static:
            if qualification != "device-tied-static":
                raise ValidationError(f"{context}: static qualification is invalid")
            static_nets += 1
            static_sinks += len(sinks)
        else:
            raise ValidationError(f"{context}: unsupported routed net kind")
        if static:
            roots = net.get("roots")
            if (
                sources
                or alternate_sources
                or not sinks
                or not isinstance(roots, list)
                or not roots
                or any(not isinstance(root, str) or not root for root in roots)
                or len(set(roots)) != len(roots)
            ):
                raise ValidationError(f"{context}: static route roots/sinks are invalid")
            source_nodes = set(roots)
        else:
            if len(sources) != 1 or not sinks or "roots" in net:
                raise ValidationError(f"{context}: routed net lacks one source and sinks")
            if kind == "clock" and sources[0]["site"] not in seen_clock_gate_sites:
                raise ValidationError(
                    f"{context}: clock source is not a declared BUFGCE site"
                )
            source_nodes = {
                sources[0]["node"],
                *(pin["node"] for pin in alternate_sources),
            }
            if (
                len(source_nodes) != 1 + len(alternate_sources)
                or source_nodes.intersection(pin["node"] for pin in sinks)
            ):
                raise ValidationError(
                    f"{context}: duplicate or conflicting physical route source"
                )
        if net.get("source_present") is not bool(sources):
            raise ValidationError(f"{context}: source-presence summary disagrees")
        if net.get("sink_count") != len(sinks):
            raise ValidationError(f"{context}: sink-count summary disagrees")
        for pin_index, pin in enumerate(pins):
            delay = pin.get("route_delay_ps")
            if static:
                if delay is not None:
                    raise ValidationError(
                        f"{context}.pins[{pin_index}]: static pin has a route delay"
                    )
                continue
            if pin["is_output"]:
                if delay is not None:
                    raise ValidationError(
                        f"{context}.pins[{pin_index}]: source has a route delay"
                    )
                continue
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(float(delay))
                or float(delay) < 0.0
            ):
                raise ValidationError(
                    f"{context}.pins[{pin_index}]: route delay is invalid"
                )
            timed_endpoints += 1
            maximum_route_delay_ps = max(
                maximum_route_delay_ps, float(delay)
            )
        graph: Dict[str, Set[str]] = defaultdict(set)
        local_pips: Set[Tuple[str, str, str]] = set()
        for pip_index, pip in enumerate(pips):
            if not isinstance(pip, dict):
                raise ValidationError(f"{context}.pips[{pip_index}]: invalid PIP")
            start, end = pip.get("start_node"), pip.get("end_node")
            if not isinstance(start, str) or not isinstance(end, str) or start == end:
                raise ValidationError(f"{context}.pips[{pip_index}]: invalid directed adjacency")
            tile = pip.get("tile")
            start_wire = pip.get("start_wire")
            end_wire = pip.get("end_wire")
            if any(
                not isinstance(item, str) or not item
                for item in (tile, start_wire, end_wire)
            ):
                raise ValidationError(f"{context}.pips[{pip_index}]: invalid PIP identity")
            wires = sorted([start_wire, end_wire])
            key = (tile, wires[0], wires[1])
            if key in local_pips:
                raise ValidationError(f"{context}: duplicate PIP {key}")
            local_pips.add(key)
            graph[start].add(end)
        used_pips.claim_many(local_pips, net_name)
        if pips:
            nets_with_pips += 1
        reachable = set(source_nodes)
        work = deque(sorted(source_nodes))
        while work:
            current = work.popleft()
            for following in graph.get(current, set()):
                if following not in reachable:
                    reachable.add(following)
                    work.append(following)
        missing = sorted(pin["node"] for pin in sinks if pin["node"] not in reachable)
        if missing:
            raise ValidationError(
                f"{context}: {len(missing)} sinks are not source-connected"
            )
        checked_nets += 1
        checked_sinks += len(sinks)
    excluded_names: Set[str] = set()
    boundary_clock_nets = 0
    allowed_exclusions = {
        "boundary_or_driverless", "multiple_driver_or_sinkless", "intra_site"
    }
    for index, excluded in enumerate(excluded_nets):
        context = f"route.excluded_nets[{index}]"
        if not isinstance(excluded, dict):
            raise ValidationError(f"{context}: invalid exclusion record")
        net_name, reason = excluded.get("net"), excluded.get("reason")
        if reason == "boundary_clock":
            if (
                set(excluded) != {"net", "reason", "qualification"}
                or excluded.get("qualification") != "ideal-boundary-clock"
            ):
                raise ValidationError(
                    f"{context}: boundary clock qualification is invalid"
                )
            boundary_clock_nets += 1
        elif set(excluded) != {"net", "reason"} or reason not in allowed_exclusions:
            raise ValidationError(f"{context}: invalid exclusion record")
        if (
            not isinstance(net_name, str)
            or not net_name
            or net_name in seen_nets
            or net_name in excluded_names
        ):
            raise ValidationError(f"{context}: invalid or conflicting exclusion")
        excluded_names.add(net_name)
    expected_summary = {
        "candidate_nets": checked_nets - static_nets,
        "certificate_nets": checked_nets,
        "static_nets": static_nets,
        "static_sinks": static_sinks,
        "nets_with_pips": nets_with_pips,
        "pips": used_pips.count,
        "excluded_nets": len(excluded_nets),
        "boundary_clock_nets": boundary_clock_nets,
    }
    if summary != expected_summary:
        raise ValidationError("XilinxRouteDB summary disagrees with route certificate")
    timing = value.get("timing")
    expected_timing = {
        "provider": "rapidwright-lightweight",
        "family": "UltraScalePlus",
        "units": "ps",
        "setup_route_delays": "available",
        "hold_analysis": "unavailable",
        "hard_block_clock_timing": "unqualified",
        "source_revision": RAPIDWRIGHT_TIMING_DATA_REVISION,
        "source_data_sha256": RAPIDWRIGHT_TIMING_DATA_SHA256,
        "device_data_md5": RAPIDWRIGHT_DEVICE_DATA_MD5,
    }
    if not isinstance(timing, dict):
        raise ValidationError("XilinxRouteDB timing qualification is missing")
    for key, expected in expected_timing.items():
        if timing.get(key) != expected:
            raise ValidationError(f"XilinxRouteDB timing.{key} is invalid")
    if timing.get("routed_endpoints") != timed_endpoints:
        raise ValidationError("XilinxRouteDB timed endpoint count disagrees")
    logic_coefficients = timing.get("logic_coefficients_ps")
    expected_logic_coefficients = {
        "ff_clock_to_q", "carry_co", "lut_a1", "lut_a2", "lut_a3",
        "lut_a4", "lut_a5", "lut_a6",
    }
    if (
        not isinstance(logic_coefficients, dict)
        or set(logic_coefficients) != expected_logic_coefficients
    ):
        raise ValidationError(
            "XilinxRouteDB logic timing coefficients are invalid"
        )
    for name, delay in logic_coefficients.items():
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError(
                f"XilinxRouteDB logic timing coefficient {name!r} is invalid"
            )
    reported_maximum = timing.get("maximum_route_delay_ps")
    if (
        isinstance(reported_maximum, bool)
        or not isinstance(reported_maximum, (int, float))
        or not math.isclose(
            float(reported_maximum), maximum_route_delay_ps,
            rel_tol=1.0e-6, abs_tol=1.0e-3,
        )
    ):
        raise ValidationError("XilinxRouteDB maximum route delay disagrees")
    # The independent validation report is the promotion certificate.  Keep
    # the Java-produced route artifact immutable rather than rewriting a
    # near-gigabyte JSON document merely to change candidate -> pass.
    report = {
        "status": "pass", "schema": "emuflow.xilinx-route-validation/v1",
        "nets": checked_nets, "sinks": checked_sinks,
        "pips": used_pips.count, "route_sha256": _sha256(path),
        "excluded_nets": len(value.get("excluded_nets", [])),
        "boundary_clock_nets": boundary_clock_nets,
        "timed_endpoints": timed_endpoints,
        "maximum_route_delay_ps": maximum_route_delay_ps,
        "timing_provider": timing["provider"],
        "hold_analysis": timing["hold_analysis"],
        "route_cells": route_cells,
        "physical_cells": physical_cells,
        "virtual_io_cells": len(virtual_io),
        "transformed_dsp48e2_cells": transformed_dsp48e2,
        "dedicated_clock_gate_cells": dedicated_clock_gates,
        "static_nets": static_nets,
        "static_sinks": static_sinks,
        "clock_nets": clock_nets,
    }
    used_pips.close()
    if all(path is not None for path in expected_paths.values()):
        # These digests were checked against the supplied source paths above.
        # Returning the seal lets the immediately following timing binder
        # reuse it instead of hashing the same large inputs again.  A
        # path-free structural check deliberately does not mint such a seal.
        report["source_sha256"] = {
            key: source[key]
            for key in (
                "mapped_sha256", "packed_sha256", "placement_sha256"
            )
        }
    return report
