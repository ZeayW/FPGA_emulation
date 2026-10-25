#!/usr/bin/env python3
"""Time the complete Phase 6 API with native or qualification-only reference I/O.

This script is not installed. Both modes use the same production row builders
and independent validators. The reference replaces only closed-file publication;
it is not another production flow or a fallback from a native error.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import stat
import subprocess
import time
import tempfile
import threading
from unittest.mock import patch

from emuflow.errors import ValidationError
from emuflow.io import file_sha256, read_json, write_json
from emuflow.sqlite_artifacts import immutable_sqlite_uri
from emuflow.native_tools import native_file_generation, resolve_native_executable
from emuflow.phase6 import run_phase6
from emuflow.store_publication import require_store_sources_unchanged
from emuflow.stage_metrics import _tree_rss_kib


PUBLICATION_MODULES = (
    "split_netlist_storage",
    "transport_storage",
    "position_hints_storage",
    "pin_plan_storage",
    "chimew_binding_storage",
    "chimew_bank_channel_storage",
    "chimew_bank_channel_report_storage",
)


def workspace_bytes(workspace):
    """Visible files plus this producer's unlinked SQLite workspace files."""
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
    descriptors = Path("/proc/self/fd")
    if descriptors.is_dir():
        for descriptor in descriptors.iterdir():
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


def compare_sqlite_records(first: Path, second: Path):
    """Compare every declared field in both directions; SQLite owns the joins."""
    generations = [native_file_generation(path) for path in (first, second)]
    hashes = [file_sha256(path) for path in (first, second)]
    result = {
        "file_sha256": hashes,
        "file_identity": hashes[0] == hashes[1],
        "logical_identity": True,
    }
    if hashes[0] != hashes[1]:

        def quote(name):
            return '"' + name.replace('"', '""') + '"'

        connection = sqlite3.connect(immutable_sqlite_uri(first), uri=True)
        try:
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(
                "ATTACH DATABASE ? AS other", (immutable_sqlite_uri(second),)
            )
            names = []
            for database in ("main", "other"):
                names.append(
                    [
                        row[0]
                        for row in connection.execute(
                            f"SELECT name FROM {database}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                        )
                    ]
                )
            if names[0] != names[1]:
                raise ValidationError("qualification SQLite table coverage differs")
            for name in names[0]:
                signatures = [
                    list(
                        connection.execute(
                            f"PRAGMA {database}.table_info({quote(name)})"
                        )
                    )
                    for database in ("main", "other")
                ]
                if signatures[0] != signatures[1] or not any(
                    row[5] for row in signatures[0]
                ):
                    raise ValidationError(
                        "qualification SQLite typed columns or primary key differ: "
                        + name
                    )
                nullable_keys = [
                    row[1] for row in signatures[0] if row[5] and not row[3]
                ]
                for database in ("main", "other"):
                    if (
                        nullable_keys
                        and connection.execute(
                            f"SELECT 1 FROM {database}.{quote(name)} WHERE "
                            + " OR ".join(
                                f"{quote(key)} IS NULL" for key in nullable_keys
                            )
                            + " LIMIT 1"
                        ).fetchone()
                        is not None
                    ):
                        raise ValidationError(
                            "qualification requires non-null primary identities: "
                            + name
                        )
                equal = " AND ".join(
                    f"(a.{quote(row[1])} COLLATE BINARY IS b.{quote(row[1])} COLLATE BINARY AND "
                    f"typeof(a.{quote(row[1])})=typeof(b.{quote(row[1])}))"
                    for row in signatures[0]
                )
                for left, right in (("main", "other"), ("other", "main")):
                    query = f"SELECT 1 FROM {left}.{quote(name)} a WHERE NOT EXISTS (SELECT 1 FROM {right}.{quote(name)} b WHERE {equal}) LIMIT 1"
                    if connection.execute(query).fetchone() is not None:
                        raise ValidationError(
                            "qualification SQLite records differ: " + name + " " + left
                        )
        finally:
            connection.close()
    if generations != [native_file_generation(path) for path in (first, second)]:
        raise ValidationError(
            "qualification SQLite comparison input generation changed"
        )
    return result


def compare_results(first, second):
    for result in (first, second):
        if (
            result.get("schema") != "emuflow.phase6-publication-qualification-result/v1"
            or result.get("status") != "pass"
            or result.get("inputs_unchanged") is not True
        ):
            raise ValidationError("qualification arm lacks a successful sealed result")
    if {first.get("mode"), second.get("mode")} != {"native", "python-reference"}:
        raise ValidationError("qualification needs one native and one reference arm")
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
    ):
        if first.get(key) != second.get(key):
            raise ValidationError("qualification arm configuration differs: " + key)
    return {
        name: compare_sqlite_records(
            Path(first["artifacts"][name]), Path(second["artifacts"][name])
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
    if request.get("schema") != "emuflow.phase6-publication-qualification/v1":
        raise ValidationError("Phase 6 qualification request schema is unsupported")
    mode = request.get("mode")
    if mode not in {"native", "python-reference"}:
        raise ValidationError("unknown qualification publication mode")
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
    publications = []
    scratch = workspace / "tmp"
    scratch.mkdir(exist_ok=True)

    def instrument(original):
        def publish(temporary, destination, schema, *, sources=()):
            started = time.perf_counter()
            if mode == "native":
                result = original(temporary, destination, schema, sources=sources)
            else:
                # The prior publication boundary, used only in this explicitly
                # selected qualification process. No producer or validator is
                # replaced, and no native failure selects this branch.
                require_store_sources_unchanged(sources)
                with Path(temporary).open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                result = None
            publications.append(
                {
                    "schema": schema,
                    "wall_seconds": time.perf_counter() - started,
                    **(
                        {"native_metrics": result["metrics"]}
                        if result is not None
                        else {}
                    ),
                }
            )
            return result

        return publish

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
        for name in PUBLICATION_MODULES:
            module = importlib.import_module("emuflow." + name)
            stack.enter_context(
                patch.object(
                    module,
                    "publish_sqlite_store",
                    instrument(module.publish_sqlite_store),
                )
            )
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
        started = time.perf_counter()
        try:
            report = run_phase6(
                sources["ir"],
                sources["assignment"],
                sources["schedule"],
                sources["platform"],
                output,
                managed_storage=True,
                clock_periods=clocks,
            )
            elapsed = time.perf_counter() - started
            after = resource.getrusage(resource.RUSAGE_SELF)
        finally:
            done.set()
            sampler.join()
            sample_once()
    if report.get("status") != "pass":
        raise ValidationError("Phase 6 production API did not pass")
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
    connection = sqlite3.connect(
        immutable_sqlite_uri(output / "split-netlists.sqlite"), uri=True
    )
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
    if {entry["schema"] for entry in publications} != {
        "transport-store-v1",
        "split-netlists-v2",
    }:
        raise ValidationError(
            "qualification did not exercise both baseline Phase 6 publishers"
        )
    if native_file_generation(request_path) != request_generation or any(
        native_file_generation(sources[name]) != generation
        for name, generation in generations.items()
    ):
        raise ValidationError("qualification input generation changed")
    result = {
        "schema": "emuflow.phase6-publication-qualification-result/v1",
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
        "scope": "complete Phase6 API; publication integration only; no complete-flow or physical/timing claim",
        "clock_periods": clocks,
        "api_wall_seconds": elapsed,
        "sampled_peak_process_tree_rss_kib": peaks[0],
        "sampled_peak_workspace_logical_bytes": peaks[1],
        "sampled_peak_workspace_allocated_bytes": peaks[2],
        "rss_measurement": "stage_metrics-sampled-at-0.5s",
        "disk_measurement": "workspace files and producer unlinked workspace fds; inode deduplicated; sampled-at-0.5s plus final; not guaranteed high-water",
        "unlinked_fd_sampling_available": Path("/proc/self/fd").is_dir(),
        "user_cpu_seconds": after.ru_utime - before.ru_utime,
        "system_cpu_seconds": after.ru_stime - before.ru_stime,
        "cpu_scope": "self-process-interval; native child metrics are separate",
        "publications": publications,
        "inputs_unchanged": True,
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
