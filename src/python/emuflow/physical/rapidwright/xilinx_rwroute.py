"""Compact RWRoute adapter and independent route-certificate checker."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json
from emuflow.storage.store_publication import bind_store_source


XILINX_ROUTE_DB_SCHEMA = "emuflow.xilinx-route-db/v1"
XILINX_ROUTE_DB_STREAM_SCHEMA = "emuflow.xilinx-route-db/v2"
XILINX_ROUTE_NET_PAYLOAD_FORMAT = "jsonl-object/v1"
XILINX_ROUTE_PIP_PAYLOAD_FORMAT = "jsonl-array/v1"


from emuflow.common.paths import DATA_ROOT

RAPIDWRIGHT_TIMING_CONTRACT = DATA_ROOT / "rapidwright" / "xcvu19p-timing-contract.json"
_TIMING_CONTRACT = json.loads(RAPIDWRIGHT_TIMING_CONTRACT.read_text())["timing"]
RAPIDWRIGHT_TIMING_DATA_REVISION = _TIMING_CONTRACT["source_revision"]
RAPIDWRIGHT_TIMING_DATA_SHA256 = _TIMING_CONTRACT["source_data_sha256"]
RAPIDWRIGHT_DEVICE_DATA_MD5 = _TIMING_CONTRACT["device_data_md5"]


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
        _route_source=route_source,
    )
    if route_value_sink is not None:
        value = read_json(output_path)
        from emuflow.storage.store_publication import require_store_sources_unchanged
        require_store_sources_unchanged((route_source,))
        route_value_sink.clear()
        route_value_sink.update(value)
    return {**report, "output": str(output_path), "log": str(log_path) if log_path else None}


def validate_xilinx_route_db(path: Path, **kwargs) -> Dict[str, Any]:
    """Independently validate original route streams in the shared core."""
    from emuflow.physical.rapidwright.route_validation import validate_native_route
    return validate_native_route(path, **kwargs)
