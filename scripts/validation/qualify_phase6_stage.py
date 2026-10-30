#!/usr/bin/env python3
"""Qualify the complete native baseline Phase 6 stage and its original records.

The retired Python closed-file publisher experiment is no longer an execution
mode. Repeated native runs establish identity, not a reference speedup claim.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import stat
import struct
import subprocess
import time
import tempfile
import threading
from unittest.mock import patch

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.common.native_tools import native_file_generation, resolve_native_executable
from emuflow_tools.stage_clients.transport import run_phase6
from emuflow.transport.split_netlist_storage import open_split_netlists_sqlite
from emuflow.common.stage_metrics import _tree_rss_kib


def workspace_bytes(workspace):
    """Visible files plus unlinked workspace files in the producer tree."""
    sizes = {}

    def add(path):
        try:
            value = path.stat()
            if not stat.S_ISREG(value.st_mode):
                return
            sizes[(value.st_dev, value.st_ino)] = (value.st_size, value.st_blocks * 512)
        except OSError:
            pass

    for directory, _folders, files in os.walk(workspace):
        for filename in files:
            path = Path(directory) / filename
            if not path.is_symlink():
                add(path)
    pending=[os.getpid()];seen=set()
    while pending:
        pid=pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        process=Path('/proc')/str(pid)
        try:
            for task in (process/'task').iterdir():
                pending.extend(int(value) for value in (task/'children').read_text().split())
            descriptors=list((process/'fd').iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
                if target.endswith(" (deleted)"):
                    target = target[:-10]
                if not Path(target).is_absolute():
                    continue
                Path(target).resolve().relative_to(workspace)
            except (OSError, ValueError):
                continue
            add(descriptor)
    return tuple(sum(item[index] for item in sizes.values()) for index in (0, 1))


SQLITE_RECORD_SEAL_SCHEMA = "emuflow.sqlite-ordered-record-seal/v1"
RESULT_SCHEMA = "emuflow.phase6-stage-qualification-result/v1"


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _record_bytes(values):
    """Unambiguous SQLite storage types and lengths; one record at a time."""
    output = bytearray(b"R")
    for value in values:
        if value is None:
            output += b"N"
        elif isinstance(value, int):
            output += b"I" + struct.pack(">q", value)
        elif isinstance(value, float):
            if math.isnan(value):
                raise ValidationError("qualification cannot seal SQLite NaN")
            # SQLite IS considers the two signed zero representations equal.
            output += b"F" + struct.pack(">d", 0.0 if value == 0 else value)
        elif isinstance(value, (str, bytes)):
            data = value.encode("utf-8") if isinstance(value, str) else value
            output += (
                (b"T" if isinstance(value, str) else b"B")
                + struct.pack(">Q", len(data))
                + data
            )
        else:
            raise ValidationError("qualification SQLite storage type is unsupported")
    return output


def seal_sqlite_records(path: Path):
    """Seal every typed field in primary-key order; retain no row population."""
    generation = native_file_generation(path)
    digest = file_sha256(path)
    tables = []
    with closing(sqlite3.connect(immutable_sqlite_uri(path), uri=True)) as connection:
        connection.execute("PRAGMA temp_store=FILE")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*' ORDER BY name"
            )
        ]
        for name in names:
            if any(
                row[6] != 0
                for row in connection.execute(f"PRAGMA table_xinfo({_quote(name)})")
            ):
                raise ValidationError(
                    "qualification SQLite generated/hidden columns are unsupported: "
                    + name
                )
            columns = [
                list(row)
                for row in connection.execute(f"PRAGMA table_info({_quote(name)})")
            ]
            keys = sorted((row[5], row[0]) for row in columns if row[5])
            if (
                not columns
                or not keys
                or [key[0] for key in keys] != list(range(1, len(keys) + 1))
            ):
                raise ValidationError(
                    "qualification requires declared primary identities: " + name
                )
            visible = [
                item[0]
                for item in connection.execute(
                    f"SELECT * FROM {_quote(name)} LIMIT 0"
                ).description
            ]
            if visible != [column[1] for column in columns]:
                raise ValidationError(
                    "qualification SQLite generated/hidden columns are unsupported: "
                    + name
                )
            ordered = ",".join(
                _quote(columns[index][1]) + " COLLATE BINARY"
                for _position, index in keys
            )
            rows = connection.execute(
                f"SELECT * FROM {_quote(name)} ORDER BY {ordered}"
            )
            records = hashlib.sha256(b"emuflow.sqlite-typed-records/v1\0")
            records.update(
                json.dumps(
                    [name, columns], ensure_ascii=True, separators=(",", ":")
                ).encode("utf-8")
            )
            count = 0
            for row in rows:
                if any(row[index] is None for _position, index in keys):
                    raise ValidationError(
                        "qualification requires non-null primary identities: " + name
                    )
                records.update(_record_bytes(row))
                count += 1
            records.update(b"E" + struct.pack(">Q", count))
            tables.append(
                {
                    "name": name,
                    "columns": columns,
                    "records": count,
                    "records_sha256": records.hexdigest(),
                }
            )
    if native_file_generation(path) != generation:
        raise ValidationError("qualification SQLite seal input generation changed")
    return {
        "schema": SQLITE_RECORD_SEAL_SCHEMA,
        "file_sha256": digest,
        "file_bytes": generation[2],
        "application_id": application_id,
        "user_version": user_version,
        "tables": tables,
    }


def _validate_record_seal(value):
    def digest(text):
        return (
            isinstance(text, str)
            and len(text) == 64
            and all(c in "0123456789abcdef" for c in text)
        )

    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema",
            "file_sha256",
            "file_bytes",
            "application_id",
            "user_version",
            "tables",
        }
        or value["schema"] != SQLITE_RECORD_SEAL_SCHEMA
        or not digest(value["file_sha256"])
        or any(
            type(value[key]) is not int or value[key] < 0
            for key in ("file_bytes", "application_id", "user_version")
        )
        or not isinstance(value["tables"], list)
        or not value["tables"]
    ):
        raise ValidationError("qualification SQLite record seal is malformed")
    previous = None
    for table in value["tables"]:
        if (
            not isinstance(table, dict)
            or set(table) != {"name", "columns", "records", "records_sha256"}
            or not isinstance(table["name"], str)
            or not table["name"]
            or (previous is not None and previous >= table["name"])
            or type(table["records"]) is not int
            or table["records"] < 0
            or not digest(table["records_sha256"])
            or not isinstance(table["columns"], list)
            or not table["columns"]
        ):
            raise ValidationError("qualification SQLite table seal is malformed")
        previous = table["name"]
        for index, column in enumerate(table["columns"]):
            if (
                not isinstance(column, list)
                or len(column) != 6
                or type(column[0]) is not int
                or column[0] != index
                or not isinstance(column[1], str)
                or not column[1]
                or not isinstance(column[2], str)
                or type(column[3]) is not int
                or column[3] not in (0, 1)
                or column[4] is not None
                and not isinstance(column[4], str)
                or type(column[5]) is not int
                or not 0 <= column[5] <= len(table["columns"])
            ):
                raise ValidationError("qualification SQLite column seal is malformed")
        keys = sorted(column[5] for column in table["columns"] if column[5])
        if (
            not keys
            or keys != list(range(1, len(keys) + 1))
            or len({column[1] for column in table["columns"]}) != len(table["columns"])
        ):
            raise ValidationError("qualification SQLite primary-key seal is malformed")


def compare_sqlite_seals(first, second):
    for value in (first, second):
        _validate_record_seal(value)
    if any(
        first[key] != second[key]
        for key in ("application_id", "user_version", "tables")
    ):
        raise ValidationError("qualification SQLite records differ")
    return {
        "file_sha256": [first["file_sha256"], second["file_sha256"]],
        "file_identity": first["file_sha256"] == second["file_sha256"],
        "logical_identity": True,
        "comparison": "all-fields-primary-key-ordered-type-length-sha256/v1",
    }


def compare_sqlite_records(first: Path, second: Path):
    return compare_sqlite_seals(seal_sqlite_records(first), seal_sqlite_records(second))


def compare_results(first, second):
    for result in (first, second):
        if (
            result.get("schema") != RESULT_SCHEMA
            or result.get("status") != "pass"
            or result.get("inputs_unchanged") is not True
        ):
            raise ValidationError("qualification arm lacks a successful sealed result")
    if first.get("mode") != "native" or second.get("mode") != "native":
        raise ValidationError("qualification needs two sealed native stage runs")
    for key in (
        "schema",
        "source_commit",
        "design",
        "platform",
        "provider",
        "source_instances",
        "clock_periods",
        "fpgas",
        "generated_text",
        "semantic_contract_sha256",
        "input_generations",
    ):
        if first.get(key) != second.get(key):
            raise ValidationError("qualification arm configuration differs: " + key)
    for result in (first, second):
        if not isinstance(result.get("artifact_seals"), dict) or set(
            result["artifact_seals"]
        ) != {"split", "transport"}:
            raise ValidationError("qualification arm lacks complete typed record seals")
    return {
        name: compare_sqlite_seals(
            first["artifact_seals"][name], second["artifact_seals"][name]
        )
        for name in ("split", "transport")
    }


def require_inside(path, workspace):
    path = Path(path).absolute()
    try:
        path.resolve().relative_to(workspace)
    except ValueError as error:
        raise ValidationError("qualification output escapes its workspace") from error
    return path


def qualify(request_path: Path, result_path: Path):
    if request_path.stat().st_size > 131072:
        raise ValidationError("Phase 6 qualification request exceeds bound")
    request_generation = native_file_generation(request_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema") != "emuflow.phase6-stage-qualification/v1":
        raise ValidationError("Phase 6 qualification request schema is unsupported")
    mode = request.get("mode")
    if mode != "native":
        raise ValidationError("whole-stage qualification requires native mode")
    entry = request.get("entry", "phase-api")
    if entry not in {"phase-api", "native-compile"}:
        raise ValidationError("qualification entry is unsupported")
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    if commit != request.get("source_commit"):
        raise ValidationError("qualification source commit disagrees")
    native = Path(resolve_native_executable("emuflow-native"))
    runtime = Path(os.environ.get("EMUFLOW_NATIVE_ROOT", str(native.parent.parent)))
    if (runtime / "SOURCE_COMMIT").read_text().strip() != commit:
        raise ValidationError("qualification native runtime source commit disagrees")
    workspace = Path(request["workspace"]).resolve(strict=True)
    output = require_inside(request["output"], workspace)
    result_path = require_inside(result_path, workspace)
    require_inside(request_path, workspace)
    if result_path.exists() or output.exists():
        raise ValidationError("qualification outputs must be fresh")
    if output.resolve() in result_path.resolve().parents:
        raise ValidationError(
            "qualification result must be outside the produced artifact directory"
        )
    sources = {
        name: Path(request["inputs"][name]).absolute()
        for name in ("ir", "assignment", "schedule", "platform")
    }
    if sources["schedule"].stat().st_size > 1048576:
        raise ValidationError("qualification needs a bounded typed schedule selector")
    schedule_selector = json.loads(sources["schedule"].read_text(encoding="utf-8"))
    artifact = schedule_selector.get("artifact")
    if (
        schedule_selector.get("schema") != "emuflow.tdm-schedule-storage/v4"
        or not isinstance(artifact, str)
        or Path(artifact).name != artifact
    ):
        raise ValidationError("qualification needs the current typed schedule store")
    sources["schedule_store"] = sources["schedule"].with_name(artifact)
    assignment_json = sources["assignment"].with_name("assignment.json")
    if assignment_json.is_file():
        sources["assignment_json"] = assignment_json
    if any(result_path == path or output == path for path in sources.values()):
        raise ValidationError("qualification output aliases an input")
    clocks = request.get("clock_periods")
    minimum_instances = request.get("minimum_instances")
    if (
        isinstance(minimum_instances, bool)
        or not isinstance(minimum_instances, int)
        or minimum_instances < 1
    ):
        raise ValidationError("qualification needs an explicit positive instance floor")
    if (
        not isinstance(clocks, dict)
        or not clocks
        or any(
            not isinstance(key, str)
            or not key
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
            for key, value in clocks.items()
        )
    ):
        raise ValidationError("qualification needs explicit positive clock periods")
    generations = {name: native_file_generation(path) for name, path in sources.items()}
    operations=[]
    scratch = workspace / "tmp"
    scratch.mkdir(exist_ok=True)

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    name: str(scratch)
                    for name in (
                        "TMPDIR",
                        "TMP",
                        "TEMP",
                        "SQLITE_TMPDIR",
                        "XDG_CACHE_HOME",
                    )
                },
            )
        )
        stack.enter_context(patch.object(tempfile, "tempdir", str(scratch)))
        from emuflow_tools.stage_clients import transport_native as phase6_native
        original_stage=phase6_native.run_native_phase6

        def capture_stage(*args, **kwargs):
            return original_stage(*args, _capture_operation=operations.append, **kwargs)

        stack.enter_context(patch.object(phase6_native, 'run_native_phase6', capture_stage))
        done = threading.Event()
        peaks = [0, 0, 0]

        def sample_once():
            logical, allocated = workspace_bytes(workspace)
            peaks[0] = max(peaks[0], _tree_rss_kib())
            peaks[1] = max(peaks[1], logical)
            peaks[2] = max(peaks[2], allocated)

        def sample():
            while not done.wait(0.5):
                sample_once()

        sample_once()
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        before = resource.getrusage(resource.RUSAGE_SELF)
        children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.perf_counter()
        try:
            if entry == "phase-api":
                report = run_phase6(
                    sources["ir"], sources["assignment"], sources["schedule"],
                    sources["platform"], output, managed_storage=True,
                    clock_periods=clocks,
                )
            else:
                from emuflow_tools.stage_clients.transport_native import prepare_compile_policy
                from emuflow.common.tool_runtime import NativeToolProcess

                with tempfile.TemporaryDirectory(prefix="native-compile-", dir=scratch) as temporary:
                    policy = prepare_compile_policy(Path(temporary) / "policy",
                        sources["ir"], sources["platform"], clock_periods=clocks)
                    operation = Path(temporary) / "operation.json"
                    argv = [str(native), "--report", str(operation), "phase6", "compile",
                        *(str(sources[name]) for name in ("ir", "assignment", "schedule", "platform")),
                        str(policy), str(output)]
                    with NativeToolProcess(str(native), argv, environment=dict(os.environ),
                            scratch_parent=Path(temporary)) as process:
                        result = process.finish()
                    if result.returncode:
                        raise ValidationError("native Phase 6 compile failed: " + result.stdout)
                    if operation.stat().st_size > 262144:
                        raise ValidationError("native Phase 6 operation exceeds bound")
                    proof = json.loads(operation.read_text())
                    fields = ('device','inode','bytes','mtime_seconds','mtime_nanoseconds','ctime_seconds','ctime_nanoseconds')
                    outputs = proof.get('outputs', [])
                    if len(outputs) != 3:
                        raise ValidationError('native Phase 6 output coverage disagrees')
                    for row, (name, schema) in zip(outputs, (
                        ('phase6_report.json','phase6-report-v1'),
                        ('split-netlists.sqlite','split-netlists-v3'),
                        ('transport.sqlite','transport-store-v2'))):
                        path = output / name
                        if (row.get('path') != str(path.resolve()) or row.get('schema') != schema or
                            tuple(row.get('generation', {}).get(key) for key in fields) != native_file_generation(path)):
                            raise ValidationError('native Phase 6 output generation changed')
                    operations.append(proof)
                report = read_json(output / 'phase6_report.json')
            elapsed = time.perf_counter() - started
            after = resource.getrusage(resource.RUSAGE_SELF)
            children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        finally:
            done.set()
            sampler.join()
            sample_once()
    if report.get("status") != "pass" or report.get("stage_implementation_id") != "native-baseline-phase6-v1":
        raise ValidationError("Phase 6 production API did not pass")
    expected_stage = 'phase6.run' if entry == 'phase-api' else 'phase6.compile'
    if len(operations)!=1 or operations[0].get('stage')!=expected_stage or operations[0].get('status')!='pass':
        raise ValidationError('qualification requires one complete native stage operation')
    manifest_path = output / "manifest.json"
    if manifest_path.stat().st_size > 1048576:
        raise ValidationError("qualification phase manifest exceeds bound")
    manifest = read_json(manifest_path)
    if manifest.get("semantic_contract_sha256") != request.get(
        "semantic_contract_sha256"
    ):
        raise ValidationError("qualification Static Exact identity disagrees")
    if (
        report.get("design") != request.get("expected_design")
        or report.get("platform") != request.get("expected_platform")
    ) or sorted(entry["fpga"] for entry in manifest["fpgas"]) != request.get(
        "expected_fpgas"
    ):
        raise ValidationError("qualification design or FPGA coverage disagrees")
    connection = open_split_netlists_sqlite(output / "split-netlists.sqlite")
    try:
        source_instances = connection.execute(
            "SELECT count(*) FROM source_instances"
        ).fetchone()[0]
    finally:
        connection.close()
    if source_instances < minimum_instances:
        raise ValidationError("qualification DUT is below its declared scale floor")
    text_files = [output / "virtual_runtime_controller.sv"]
    for fpga in request["expected_fpgas"]:
        for pattern in ("*.sv", "*.svh", "*.xdc", "*.template"):
            text_files.extend(sorted((output / fpga).glob(pattern)))
    generated_text = {
        str(path.relative_to(output)): file_sha256(path) for path in text_files
    }
    if native_file_generation(request_path) != request_generation or any(
        native_file_generation(sources[name]) != generation
        for name, generation in generations.items()
    ):
        raise ValidationError("qualification input generation changed")
    seal_started = time.perf_counter()
    artifact_seals = {
        name: seal_sqlite_records(output / filename)
        for name, filename in (
            ("split", "split-netlists.sqlite"),
            ("transport", "transport.sqlite"),
        )
    }
    seal_wall = time.perf_counter() - seal_started
    if native_file_generation(request_path) != request_generation or any(
        native_file_generation(sources[name]) != generation
        for name, generation in generations.items()
    ):
        raise ValidationError("qualification input generation changed during sealing")
    result = {
        "schema": RESULT_SCHEMA,
        "status": "pass",
        "source_commit": commit,
        "design": report["design"],
        "platform": report["platform"],
        "provider": report["provider"],
        "source_instances": source_instances,
        "generated_text": generated_text,
        "semantic_contract_sha256": manifest.get("semantic_contract_sha256"),
        "fpgas": request["expected_fpgas"],
        "mode": mode,
        "entry": entry,
        "scope": "complete native baseline Phase6; phase API/native compiler entry identity and stage measurements; no old-publisher comparison or complete-flow/physical/timing claim",
        "clock_periods": clocks,
        "api_wall_seconds": elapsed,
        "sampled_peak_process_tree_rss_kib": peaks[0],
        "sampled_peak_workspace_logical_bytes": peaks[1],
        "sampled_peak_workspace_allocated_bytes": peaks[2],
        "rss_measurement": "stage_metrics-sampled-at-0.5s",
        "disk_measurement": "workspace files and process-tree unlinked workspace fds; inode deduplicated; sampled-at-0.5s plus final; not guaranteed high-water",
        "unlinked_fd_sampling_available": Path("/proc/self/fd").is_dir(),
        "user_cpu_seconds": after.ru_utime - before.ru_utime,
        "system_cpu_seconds": after.ru_stime - before.ru_stime,
        "children_user_cpu_seconds": children_after.ru_utime - children_before.ru_utime,
        "children_system_cpu_seconds": children_after.ru_stime - children_before.ru_stime,
        "cpu_scope": "separate self and reaped-child process-interval deltas; do not add native operation CPU a second time",
        "native_stage_operation": operations[0],
        "stage_implementation_id": report["stage_implementation_id"],
        "inputs_unchanged": True,
        "input_generations": {name: list(value) for name, value in generations.items()},
        "artifact_seals": artifact_seals,
        "record_seal_wall_seconds": seal_wall,
        "record_seal_scope": "post-API verification, excluded from API wall/CPU/RSS/disk intervals; every record and storage type",
        "artifacts": {
            "split": str(output / "split-netlists.sqlite"),
            "transport": str(output / "transport.sqlite"),
            "report": str(output / "phase6_report.json"),
        },
    }
    write_json(result_path, result, compact=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.request, args.result)
