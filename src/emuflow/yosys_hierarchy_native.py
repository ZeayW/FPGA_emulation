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
from .store_publication import bind_store_source, require_store_sources_unchanged
from .io import file_sha256


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
        source = (nullcontext(source_stream) if source_stream is not None else
                  gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else
                  path.open("r", encoding="utf-8"))
        with source as stream, NativeToolProcess(
            executable, command, environment=dict(os.environ), stdin=subprocess.PIPE,
            scratch_parent=root,
        ) as process:
            try:
                # Thin decoding/pump only. Kernel pipe backpressure bounds this
                # buffer; Python does not parse records, names, pins or aliases.
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    process.process.stdin.write(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
                process.process.stdin.close()
            except BrokenPipeError:
                # Preserve the native parser's precise rejection below.
                pass
            completed = process.finish()
            if completed.returncode:
                raise ImportError("native Yosys hierarchy import failed: " + completed.stdout)
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
