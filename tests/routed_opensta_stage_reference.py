"""Native routed graph owner, borrowed model index and external Liberty input."""
from collections.abc import Mapping
from contextlib import closing, ExitStack
from heapq import merge
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.json_io import file_sha256
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import require_native_schema, resolve_native_executable
from emuflow.storage.sqlite_artifacts import immutable_sqlite_uri
from emuflow.storage.store_publication import bind_store_source, publish_derived_outputs, require_store_sources_unchanged, validate_store_destination
from emuflow.common.tool_runtime import NativeToolProcess

_PREFIX = "EMUFLOW_RW_ROUTE_DELAY_"


class _TimingCells(Mapping):
    def __init__(self, model):
        self.model = model

    def __len__(self):
        return len(self.model.base_model["cells"]) + self.model.delay_count

    def __getitem__(self, key):
        self.model.require_unchanged()
        base = self.model.base_model["cells"]
        if key in base:
            return base[key]
        if not isinstance(key, str) or not key.startswith(_PREFIX):
            raise KeyError(key)
        ordinal = key[len(_PREFIX):]
        if not ordinal.isascii() or not ordinal.isdecimal():
            raise KeyError(key)
        ordinal = int(ordinal)
        if key != _PREFIX + f"{ordinal:06d}":
            raise KeyError(key)
        with closing(sqlite3.connect(immutable_sqlite_uri(self.model.owner.path), uri=True)) as connection:
            row = connection.execute("SELECT p.value FROM routed_delay_types t JOIN payloads p ON p.seq=t.attributes_seq WHERE t.seq=?", (ordinal,)).fetchone()
        if row is None:
            raise KeyError(key)
        self.model.require_unchanged()
        return self._record(json.loads(row[0])["emuflow_route_delay_ns"])

    @staticmethod
    def _record(delay):
        return {"kind": "combinational", "inputs": ["A"], "output": "Y", "delay_ns": delay}

    def items(self):
        self.model.require_unchanged()
        try:
            with closing(sqlite3.connect(immutable_sqlite_uri(self.model.owner.path), uri=True)) as connection:
                rows = connection.execute("SELECT t.seq,p.value FROM routed_delay_types t JOIN payloads p ON p.seq=t.attributes_seq ORDER BY printf('EMUFLOW_RW_ROUTE_DELAY_%06d',t.seq)")
                delayed = ((_PREFIX + f"{seq:06d}", self._record(json.loads(payload)["emuflow_route_delay_ns"])) for seq, payload in rows)
                yield from merge(sorted(self.model.base_model["cells"].items()), delayed, key=lambda item: item[0])
        finally:
            self.model.require_unchanged()

    def __iter__(self):
        for name, _record in self.items():
            yield name


class RoutedTimingModel(dict):
    """Small base model plus a source-bound index, never a Python delay table."""
    def __init__(self, base, owner, library, summary):
        super().__init__(base)
        self.base_model = base
        self.owner, self.library = owner, library
        self.sources = (owner, library)
        self.delay_count = summary["unique_route_delay_cells"]
        self.original_types = tuple(summary["original_cell_types"])
        self["cells"] = _TimingCells(self)

    def require_unchanged(self):
        require_store_sources_unchanged(self.sources)

    def validate_coverage(self, ir, instance_cell_types=None):
        self.require_unchanged()
        if instance_cell_types is not None or not hasattr(ir, "connection"):
            raise ValidationError("native routed timing coverage requires its indexed graph owner")
        filenames = [Path(row[2]).resolve() for row in ir.connection.execute("PRAGMA database_list") if row[1] == "main"]
        if filenames != [self.owner.path]:
            raise ValidationError("native routed timing model belongs to another graph")
        unsupported = set(self.original_types) - self.base_model["cells"].keys()
        if unsupported:
            raise ValidationError(f"native routed timing model lacks {sorted(unsupported)!r}")
        return {"schema": "emuflow.native-routed-model-coverage/v1", "status": "pass",
                "base_used_cell_types": list(self.original_types),
                "base_model_cell_types": sorted(self.base_model["cells"]),
                "used_cell_type_count": len(self.original_types) + self.delay_count,
                "model_cell_type_count": len(self.base_model["cells"]) + self.delay_count,
                "generated_cell_types": {"owner": str(self.owner.path),
                                         "artifact_generation": list(self.owner.generation),
                                         "schema": "emuflow.routed-opensta-types/v1",
                                         "records": self.delay_count}}


def stage_routed_opensta(mapped_path, timing_path, output_path, *, clocks,
                         timing_validation=None, timing_value=None,
                         mapped_sha256=None, timing_sha256=None, source_bindings=None):
    from emuflow_tools.stage_clients.opensta import DEFAULT_TIMING_MODEL, load_timing_model
    from tests.routed_opensta_reference import _bind_routed_opensta_inputs, _routed_base_model

    require_native_schema("routed-opensta-request-v1")
    executable = resolve_native_executable("emuflow-native")
    if source_bindings is None:
        sources, timing_value = _bind_routed_opensta_inputs(mapped_path, timing_path, timing_value)
    else:
        sources = tuple(source_bindings)
        require_store_sources_unchanged(sources)
        if timing_value is None:
            from emuflow_tools.document_io import read_json
            timing_value = read_json(timing_path)
    if (len(sources) != 3 or sources[0].schema != "xilinx-mapped-index-v2"
            or timing_value.get("schema") != "emuflow.xilinx-routed-timing/v2"):
        raise ValidationError("native routed staging requires the current mapped index and timing stream")
    mapped_sha256 = mapped_sha256 or file_sha256(mapped_path)
    timing_sha256 = timing_sha256 or file_sha256(timing_path)
    if timing_value.get("source", {}).get("mapped_sha256") != mapped_sha256:
        raise ValidationError("native routed timing mapped source seal is stale")
    if timing_validation is not None and (timing_validation.get("schema") != "emuflow.xilinx-routed-timing-validation/v1"
            or timing_validation.get("status") != "pass" or timing_validation.get("timing_sha256") != timing_sha256):
        raise ValidationError("native routed timing validation seal is invalid")
    output_path = Path(output_path)
    library_path = output_path.with_name(output_path.name + ".route-delays.lib")
    for destination in (output_path, library_path):
        validate_store_destination(destination, sources)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    configuration = {"clocks": dict(clocks), "clock_count": len(clocks),
                     "source_path": str(mapped_path), "mapped_sha256": mapped_sha256,
                     "timing_sha256": timing_sha256,
                     "base_types": sorted(load_timing_model(DEFAULT_TIMING_MODEL)["cells"])}
    with ExitStack() as lifetime:
        root = Path(lifetime.enter_context(tempfile.TemporaryDirectory(prefix=".routed-opensta.", dir=output_path.parent)))
        request, summary = root/"request.bin", root/"summary.json"
        staged = []
        for destination in (output_path, library_path):
            descriptor, name = tempfile.mkstemp(prefix="."+destination.name+".", suffix=".tmp", dir=output_path.parent)
            os.close(descriptor)
            path = Path(name)
            path.unlink()
            lifetime.callback(path.unlink, missing_ok=True)
            staged.append(path)
        staged_emuir, staged_library = staged
        # Native partial files remain wholly inside this invocation's scope.
        # Only closed, validated outputs move to sibling staging for adoption.
        emuir, library = root/"routed.sqlite", root/"delays.lib"
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI", 0x314F5352464D5545, 1)
            for source in sources:
                writer.put("3Q4q", *source.generation)
            writer.text(json.dumps(configuration, sort_keys=True, separators=(",", ":"), allow_nan=False))
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        with NativeToolProcess(executable, [executable, "physical", "stage-routed-opensta",
                                           *(str(source.path) for source in sources), str(request),
                                           str(emuir), str(library), str(summary)],
                               environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(f"native routed OpenSTA staging failed ({completed.returncode}): " + completed.stdout)
        require_store_sources_unchanged(sources)
        if summary.stat().st_size > 65536:
            raise ValidationError("native routed staging summary exceeds bound")
        result = json.loads(summary.read_text())
        if (result.get("schema") != "emuflow.routed-opensta-summary/v1" or result.get("status") != "pass"
                or result.get("implementation_id") != "native-routed-opensta-v1"
                or result.get("logic_coefficients_ps") != timing_value["qualification"]["logic_coefficients_ps"]
                or result.get("top") != timing_value["top"]
                or result.get("inserted_route_delay_cells") != timing_value["summary"]["logical_endpoints"]):
            raise ValidationError("native routed staging summary disagrees")
        base, hard_blocks = _routed_base_model(timing_value, result["pin_sets"])
        os.replace(emuir, staged_emuir)
        os.replace(library, staged_library)
        publish_derived_outputs(((staged_emuir, output_path, "emuir-compact-v2"), (staged_library, library_path, None)), sources=sources)
    model = RoutedTimingModel(base, bind_store_source(output_path, "emuir-compact-v2"),
                              bind_store_source(library_path, "routed-opensta-liberty-v1"), result)
    metadata = {"logical_route_endpoints": result["inserted_route_delay_cells"],
                "inserted_route_delay_cells": result["inserted_route_delay_cells"],
                "unique_route_delay_cells": result["unique_route_delay_cells"],
                "hard_block_types_unqualified": sorted(set(hard_blocks)),
                "falling_edge_ff_instances": result["falling_edge_ff_instances"],
                "representation": "compact-emuir-sqlite", "implementation_id": result["implementation_id"],
                "native_metrics": completed.native_metrics}
    return model, metadata
