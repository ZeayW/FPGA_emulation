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
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.storage.store_publication import StoreSource, bind_store_source, require_store_sources_unchanged
from emuflow.physical.xilinx_packing import PACKED_SITE_NETLIST_SCHEMA
from emuflow.physical.xilinx_placement import XILINX_PLACEMENT_SCHEMA


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


_CLOCK_SINK_PINS = frozenset({
    "C",
    "CLK",
    "CLKARDCLK",
    "CLKBWRCLK",
})


def _is_clock_sink_pin(pin: str) -> bool:
    """Return whether a physical primitive pin consumes a clock net."""
    return pin in _CLOCK_SINK_PINS


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


def export_rwroute_input(
    mapped_path: Path,
    physical_handoff_path: Path,
    output_path: Path,
    *,
    source_sha256: Optional[Mapping[str, str]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    resource_limits: Optional[Mapping[str, int]] = None,
) -> Dict[str, Any]:
    """Export only from the two authoritative indexed stores.

    Header/ownership projection and record generation belong to the shared
    native core. This entry has no JSON or Python object exporter.
    """

    from emuflow.common.native_tools import native_file_generation
    from emuflow.physical.rapidwright.rwroute_native import export_native_rwroute

    mapped_path, physical_handoff_path = Path(mapped_path), Path(physical_handoff_path)
    inputs = (mapped_path, physical_handoff_path)
    generations = tuple(native_file_generation(path) for path in inputs)
    for path in inputs:
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ValidationError(
                    "RWRoute export requires a mapped SQLite index and one typed "
                    "physical handoff; JSON export is retired"
                )
    if source_sha256 is None:
        physical_digest = _sha256(physical_handoff_path)
        digests = {
            "mapped_sha256": _sha256(mapped_path),
            "packed_sha256": physical_digest,
            "placement_sha256": physical_digest,
        }
    else:
        digests = dict(source_sha256)
    if set(digests) != {
        "mapped_sha256", "packed_sha256", "placement_sha256"
    } or any(
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in digests.values()
    ):
        raise ValidationError("RWRoute source digests are invalid")
    if tuple(native_file_generation(path) for path in inputs) != generations:
        raise ValidationError("RWRoute export source generation changed")
    return export_native_rwroute(
        mapped_path, physical_handoff_path, output_path,
        digests=digests, virtual_io_contract=virtual_io_contract,
        resource_limits=resource_limits,
    )


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
    route_source = bind_store_source(output_path, "xilinx-route-manifest-v2")
    value = read_json(output_path)
    if not isinstance(value.get("timing"), dict):
        raise ValidationError("RWRoute output lacks its timing qualification")
    report = validate_xilinx_route_db(
        output_path,
        rwroute_input_path=input_path,
        mapped_path=mapped_path,
        packed_path=packed_path,
        placement_path=placement_path,
        mapped_value=mapped_value,
        packed_value=packed_value,
        source_sha256=source_sha256,
        virtual_io_contract=virtual_io_contract,
        _value=value,
        _route_source=route_source,
    )
    if route_value_sink is not None:
        route_value_sink.clear()
        route_value_sink.update(value)
    return {**report, "output": str(output_path), "log": str(log_path) if log_path else None}


def validate_xilinx_route_db(
    path: Path,
    *,
    rwroute_input_path: Optional[Path] = None,
    mapped_path: Optional[Path] = None,
    packed_path: Optional[Path] = None,
    placement_path: Optional[Path] = None,
    mapped_value: Optional[Mapping[str, Any]] = None,
    packed_value: Optional[Mapping[str, Any]] = None,
    source_sha256: Optional[Mapping[str, str]] = None,
    virtual_io_contract: Optional[Mapping[str, Any]] = None,
    _value: Optional[Dict[str, Any]] = None,
    _route_source: Optional[StoreSource] = None,
) -> Dict[str, Any]:
    # RWRoute already materializes a very large in-memory object.  Accepting
    # that exact object internally avoids immediately parsing the same route
    # database a second time before it is sealed.  External callers retain the
    # ordinary path-only validation API.
    route_source = _route_source or bind_store_source(path, "xilinx-route-manifest-v2")
    if route_source.path != path.resolve():
        raise ValidationError("route validation source belongs to another artifact")
    require_store_sources_unchanged((route_source,))
    value = read_json(path) if _value is None else _value
    if (
        not isinstance(value, dict)
        or value.get("schema")
        not in {XILINX_ROUTE_DB_SCHEMA, XILINX_ROUTE_DB_STREAM_SCHEMA}
    ):
        raise ValidationError("XilinxRouteDB header is invalid")
    if value.get("status") not in {"candidate", "pass"}:
        raise ValidationError("XilinxRouteDB status is invalid")
    route_sources = {"route": route_source}
    if value["schema"] == XILINX_ROUTE_DB_STREAM_SCHEMA:
        payloads = value.get("payloads", {})
        if not isinstance(payloads, Mapping) or set(payloads) != {"nets", "pips"}:
            raise ValidationError("XilinxRouteDB payload manifest is invalid")
        for name, format_name in (("nets", XILINX_ROUTE_NET_PAYLOAD_FORMAT), ("pips", XILINX_ROUTE_PIP_PAYLOAD_FORMAT)):
            payload, _digest, _count = _route_payload_path(path, payloads.get(name), field=name, expected_format=format_name)
            route_sources[name] = bind_store_source(payload, "xilinx-route-nets-v1" if name == "nets" else "physical-input-document-v1")
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
    fabric_prefilter_clock_gates = materialization.get(
        "fabric_prefilter_clock_gate_cells", 0
    )
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
        or isinstance(fabric_prefilter_clock_gates, bool)
        or not isinstance(fabric_prefilter_clock_gates, int)
        or fabric_prefilter_clock_gates < 0
        or fabric_prefilter_clock_gates > dedicated_clock_gates
        or physical_cells
        != route_cells + 7 * transformed_dsp48e2 + fabric_prefilter_clock_gates
        or router
        != (
            "CUFR-HUS-non-timing-driven-uturn-enabled-"
            "parallel-unroutable-recovery-extended-lcb-clock"
        )
    ):
        raise ValidationError("XilinxRouteDB transformed-cell accounting is invalid")
    seen_clock_gate_cells: Set[str] = set()
    seen_clock_gate_sites: Set[str] = set()
    observed_prefilter_clock_gates = 0
    for index, gate in enumerate(clock_gates):
        gate_keys = frozenset(gate) if isinstance(gate, dict) else frozenset()
        base_keys = {
            "safe_cell", "logical_cell", "logical_type", "physical_type",
            "physical_site", "physical_bel", "ce_type", "qualification",
        }
        extended_keys = base_keys | {
            "input_site", "implementation", "cascaded_input"
        }
        implementation = (
            gate.get("implementation", "direct-bufgce")
            if isinstance(gate, dict) else None
        )
        direct = implementation == "direct-bufgce"
        prefiltered = implementation == "lut2-prefilter-bufgce-tied-high"
        if (
            not isinstance(gate, dict)
            or gate_keys not in {frozenset(base_keys), frozenset(extended_keys)}
            or gate.get("logical_type") != "LUT2"
            or (
                direct
                and (
                    gate.get("physical_type") != "BUFGCE"
                    or gate.get("ce_type") != "ASYNC"
                )
            )
            or (
                prefiltered
                and (
                    gate_keys != extended_keys
                    or gate.get("physical_type") != "BUFGCE"
                    or gate.get("ce_type") != "ASYNC_TIED_HIGH"
                )
            )
            or not (direct or prefiltered)
            or (
                "cascaded_input" in gate
                and not isinstance(gate.get("cascaded_input"), bool)
            )
            or (
                gate.get("cascaded_input") is True and not prefiltered
            )
            or gate.get("physical_bel") != "BUFCE"
            or gate.get("qualification")
            != "lut2-init-1000-fdre-latched-enable-v1"
            or not isinstance(gate.get("safe_cell"), str)
            or not isinstance(gate.get("logical_cell"), str)
            or not isinstance(gate.get("physical_site"), str)
            or (
                "input_site" in gate
                and (
                    not isinstance(gate.get("input_site"), str)
                    or not gate["input_site"]
                )
            )
            or gate["logical_cell"] in seen_clock_gate_cells
            or gate["physical_site"] in seen_clock_gate_sites
        ):
            raise ValidationError(
                f"XilinxRouteDB clock_gates[{index}] is invalid"
            )
        seen_clock_gate_cells.add(gate["logical_cell"])
        seen_clock_gate_sites.add(gate["physical_site"])
        if prefiltered:
            observed_prefilter_clock_gates += 1
    if observed_prefilter_clock_gates != fabric_prefilter_clock_gates:
        raise ValidationError("XilinxRouteDB clock-prefilter accounting disagrees")
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
        indexed_type_counts = getattr(mapped, "cell_type_counts", None)
        indexed_cell_metadata = getattr(mapped, "cell_type_and_parameters", None)
        if callable(indexed_type_counts):
            type_counts = indexed_type_counts()
            expected_lut6_2 = type_counts.get("LUT6_2", 0)
            expected_dsp48e2 = type_counts.get("DSP48E2", 0)
        else:
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
            if callable(indexed_cell_metadata):
                try:
                    logical_type, logical_parameters = indexed_cell_metadata(
                        gate["logical_cell"]
                    )
                except KeyError:
                    logical_type, logical_parameters = None, {}
            else:
                logical = mapped_cells.get(gate["logical_cell"])
                logical_type = (
                    logical.get("type") if isinstance(logical, Mapping) else None
                )
                logical_parameters = (
                    logical.get("parameters", {})
                    if isinstance(logical, Mapping) else {}
                )
            if (
                logical_type != "LUT2"
                or _primitive_parameter(logical_parameters, "INIT") != "8"
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
    if (
        rwroute_input_path is not None
        and _sha256(rwroute_input_path) != input_digest
    ):
        raise ValidationError(
            "XilinxRouteDB source.rwroute_input_sha256 does not match "
            "the routed input"
        )
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
    internal_clock_nets = 0
    extended_lcb_clock_nets = 0
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
            if qualification not in {
                "dedicated-bufgce-clock",
                "dedicated-bufgce-clock-with-fabric-cascade-spur",
                "dedicated-bufgce-clock-extended-lcb-last-mile",
            }:
                raise ValidationError(f"{context}: clock qualification is invalid")
            clock_nets += 1
            if qualification == "dedicated-bufgce-clock-extended-lcb-last-mile":
                extended_lcb_clock_nets += 1
        elif kind == "clock_internal":
            if (
                qualification != "lut2-prefilter-to-bufgce-v1"
                or re.fullmatch(r"__emuflow_clock_prefilter_c[0-9]+", net_name)
                is None
            ):
                raise ValidationError(
                    f"{context}: internal clock qualification is invalid"
                )
            internal_clock_nets += 1
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
        elif reason == "clock_gate_cascade_internalized":
            if (
                set(excluded) != {"net", "reason", "qualification"}
                or excluded.get("qualification")
                != "lut2-prefilter-fabric-edge-v1"
            ):
                raise ValidationError(
                    f"{context}: internalized clock-edge qualification is invalid"
                )
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
    if internal_clock_nets or "internal_clock_nets" in summary:
        expected_summary["internal_clock_nets"] = internal_clock_nets
    if extended_lcb_clock_nets or "extended_lcb_clock_nets" in summary:
        expected_summary["extended_lcb_clock_nets"] = extended_lcb_clock_nets
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
        "fabric_prefilter_clock_gate_cells": fabric_prefilter_clock_gates,
        "static_nets": static_nets,
        "static_sinks": static_sinks,
        "clock_nets": clock_nets,
        "internal_clock_nets": internal_clock_nets,
        "extended_lcb_clock_nets": extended_lcb_clock_nets,
    }
    used_pips.close()
    require_store_sources_unchanged(tuple(route_sources.values()))
    report["source_generations"] = {key: list(source.generation) for key, source in route_sources.items()}
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
