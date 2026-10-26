"""Bounded byte-stream host for the native hierarchy producer."""
from contextlib import closing, nullcontext
import gzip
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import tempfile

from .errors import ImportError
from .native_protocol import CheckedRequestWriter
from .native_tools import require_native_schema, resolve_native_executable
from .sqlite_artifacts import immutable_sqlite_uri
from .tool_runtime import NativeToolProcess
from .store_publication import (bind_store_source, require_store_sources_unchanged,
                                validate_store_destination, publish_sqlite_store, StoreSource)
from .io import file_sha256


def _consume(executable, command, path, source_stream, root, label):
    source = (nullcontext(source_stream) if source_stream is not None else
              gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else
              path.open("r", encoding="utf-8"))
    environment = dict(os.environ)
    environment.update({key: str(root) for key in ("TMPDIR", "TMP", "TEMP", "SQLITE_TMPDIR")})
    with source as stream, NativeToolProcess(
        executable, command, environment=environment, stdin=subprocess.PIPE,
        scratch_parent=root,
    ) as process:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                process.process.stdin.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            process.process.stdin.close()
        except BrokenPipeError:
            pass
        completed = process.finish()
        if completed.returncode:
            raise ImportError(label + " failed: " + completed.stdout)
        return completed.native_metrics


def stage_hierarchy(path, store, top, *, source_stream=None, normalization_library=None):
    request_schema = "yosys-hierarchy-request-v2" if normalization_library is not None else "yosys-hierarchy-request-v1"
    require_native_schema(request_schema)
    library = (bind_store_source(normalization_library, "xilinx-primitive-library-v1")
               if normalization_library is not None else None)
    require_native_schema("external-tool-request-v2")
    store = Path(store)
    root_parent = store.parent
    if store.exists() or store.is_symlink():
        raise ImportError("native hierarchy staging destination must be absent")
    with tempfile.TemporaryDirectory(prefix=".yosys-hierarchy-", dir=root_parent) as name:
        root = Path(name)
        request, summary = root/"request.bin", root/"summary.json"
        native_store = root/"staging.sqlite"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", 0x31485953464D5545, 2 if library is not None else 1)
            writer.text(top)
            if library is not None:
                writer.put("3Q4q", *library.generation)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable("emuflow-native")
        command = [executable, "phase1", "stage-yosys-hierarchy", str(request), str(native_store), str(summary)]
        if library is not None:
            command.append(str(library.path))
            require_store_sources_unchanged((library,))
        _consume(executable, command, path, source_stream, root, "native Yosys hierarchy import")
        if library is not None:
            require_store_sources_unchanged((library,))
        if summary.stat().st_size > 128*1024:
            raise ImportError("native Yosys hierarchy summary exceeds bound")
        result = json.loads(summary.read_text())
        if (result.get("schema") != "emuflow.yosys-hierarchy-stage/v1"
                or result.get("status") != "pass"
                or result.get("implementation_id") != ("native-yosys-hierarchy-and-xilinx-normalization-v1" if library is not None else "native-yosys-hierarchy-v1")
                or result.get("hierarchical_interchange", {}).get("top") != top):
            raise ImportError("native Yosys hierarchy summary disagrees")
        # The importer continues in this same private database. Do not copy
        # cell/name populations to a second staging owner.
        with closing(sqlite3.connect(immutable_sqlite_uri(native_store), uri=True)) as connection:
            ports = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT name,record_json FROM hierarchy_ports ORDER BY name"
            )}
        normalization = result.get("normalization")
        if library is not None:
            if (not isinstance(normalization, dict) or normalization.get("status") != "pass"
                    or normalization.get("top") != top):
                raise ImportError("native Xilinx normalization summary disagrees")
            normalization["input_sha256"] = file_sha256(path) if path.is_file() else None
        # Same-filesystem adoption, not another SQLite materialization. All
        # native writer temporaries remain under the cancellation-owned root.
        os.replace(native_store, store)
        return result["creator"], ports, result["hierarchical_interchange"], normalization


def import_hierarchical_emuir(path, output, top, clocks, *, source_stream=None,
                              normalization_library=None, source_bindings=(), finalize=None):
    """Native source-to-compact producer; the host carries no circuit records."""
    require_native_schema("yosys-emuir-import-request-v1")
    require_native_schema("external-tool-request-v2")
    library = (bind_store_source(normalization_library, "xilinx-primitive-library-v1")
               if normalization_library is not None else None)
    sources = tuple(source_bindings) + ((library,) if library is not None else ())
    validate_store_destination(output, sources)
    clock_list = sorted(set(clocks))
    if len(clock_list) > 16384 or len(sources) > 64:
        raise ImportError("native EmuIR import control configuration exceeds bound")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".yosys-emuir-", dir=output.parent) as name:
        root = Path(name)
        request, store, summary = root/"request.bin", root/"design.emuir.sqlite", root/"summary.json"
        operation = root/"operation.json"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QII", 0x31524959464D5545, 1, int(library is not None))
            writer.text(top)
            writer.text(str(path))
            writer.put("I", len(clock_list))
            for clock in clock_list:
                writer.text(clock)
            if library is not None:
                writer.put("3Q4q", *library.generation)
            writer.finish()
        finally:
            writer.close()
        executable = resolve_native_executable("emuflow-native")
        command = [executable, "--report", str(operation), "phase1", "import-yosys", str(request), str(store), str(summary)]
        if library is not None:
            command.append(str(library.path))
        require_store_sources_unchanged(sources)
        metrics = _consume(executable, command, path, source_stream, root, "native Yosys EmuIR import")
        require_store_sources_unchanged(sources)
        if operation.stat().st_size > 256*1024:
            raise ImportError("native EmuIR producer operation exceeds bound")
        produced = json.loads(operation.read_text())
        if (produced.get("schema") != "emuflow.native-stage-result/v1" or produced.get("stage") != "phase1.import-yosys"
                or produced.get("status") != "pass" or len(produced.get("outputs", [])) != 2):
            raise ImportError("native EmuIR producer operation disagrees")
        private_sources = []
        fields = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds", "ctime_seconds", "ctime_nanoseconds")
        for record, path, schema in zip(produced["outputs"], (store, summary), ("emuir-compact-v2", "yosys-emuir-import-summary-v1")):
            generation = record.get("generation", {})
            if (record.get("schema") != schema or record.get("path") != str(path.resolve())
                    or any(type(generation.get(key)) is not int for key in fields)):
                raise ImportError("native EmuIR original output binding disagrees")
            private_sources.append(StoreSource(path, schema, tuple(generation[key] for key in fields)))
        require_store_sources_unchanged(private_sources)
        if summary.stat().st_size > 128*1024:
            raise ImportError("native EmuIR import summary exceeds bound")
        result = json.loads(summary.read_text())
        if (result.get("schema") != "emuflow.yosys-emuir-import/v1"
                or result.get("status") != "pass"
                or result.get("implementation_id") != "native-yosys-emuir-v1"
                or result.get("hierarchical_interchange", {}).get("top") != top
                or result.get("validation", {}).get("status") != "pass"):
            raise ImportError("native EmuIR import summary disagrees")
        counts = result.get("emuir_records")
        if (not isinstance(counts, dict) or set(counts) != {"cell_records", "net_records", "endpoint_records"}
                or any(type(value) is not int or value < 0 for value in counts.values())):
            raise ImportError("native EmuIR import record counts disagree")
        transform = result.get("normalization")
        if library is not None:
            if (not isinstance(transform, dict) or transform.get("schema") != "emuflow.xilinx-primitive-normalization/v1"
                    or transform.get("status") != "pass" or transform.get("top") != top):
                raise ImportError("native EmuIR normalization summary disagrees")
            transform["input_sha256"] = file_sha256(path) if path.is_file() else None
        else:
            transform = {}
        require_store_sources_unchanged(private_sources)
        if finalize is not None:
            with closing(sqlite3.connect(store)) as connection:
                finalize(connection)
                connection.commit()
        # The existing shared publisher consumes the closed private owner once.
        # No Python cell/endpoint/net generator or SQLite row copy intervenes.
        require_store_sources_unchanged(sources)
        fd, name = tempfile.mkstemp(prefix=".yosys-emuir-publish-", suffix=".tmp", dir=output.parent)
        os.close(fd)
        sibling = Path(name)
        try:
            # Same-filesystem ownership transfer required by the publisher;
            # never copy or reserialize the canonical database.
            os.replace(store, sibling)
            publish_sqlite_store(sibling, output, "emuir-compact-v2", sources=sources)
        finally:
            sibling.unlink(missing_ok=True)
        return {**transform, "hierarchical_interchange": result["hierarchical_interchange"],
                "native_emuir_import": {"implementation_id": result["implementation_id"],
                                        "records": counts, "validation": result["validation"],
                                        "validation_scope": "before-explicit-finalizer" if finalize is not None else "published-records",
                                        "native_metrics": metrics}}
