"""Bounded host for the shared-core RapidWright timing graph and projections."""
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import tempfile

from emuflow_tools.timing.boundary_identity_storage import read_boundary_identity_storage
from emuflow_tools.timing.boundary_timing import boundary_timing_header
from emuflow.common.errors import ValidationError
from emuflow_tools.timing.local_path_timing import read_local_path_identity, local_path_timing_header
from emuflow_tools.timing.logic_segment_identity_storage import read_logic_segment_identity_storage
from emuflow.common.native_protocol import CheckedRequestWriter
from emuflow.common.native_tools import require_native_schema, resolve_native_executable
from emuflow_tools.physical.physical_measurements_native import publish_physical_projection
from emuflow.storage.store_publication import bind_store_source, bind_view_sources, require_store_sources_unchanged, validate_store_destination
from emuflow.common.tool_runtime import NativeToolProcess
from tests.routed_timing_adapter import validate_xilinx_routed_timing


def build_routed_segments(*, boundary_identity_path, mapped_path, timing_path,
                          boundary_output_path, logic_identity_path=None, logic_output_path=None,
                          local_identity_path=None, local_output_path=None, mapped_value,
                          timing_value=None, timing_validation=None, source_ir_path=None):
    from tests.routed_segment_entry import XILINX_SEGMENT_QUALIFICATION

    if source_ir_path is None:
        raise ValidationError("native routed timing requires the source placement IR")
    require_native_schema("routed-segment-request-v1")
    executable = resolve_native_executable("emuflow-native")
    if timing_value is None:
        from emuflow_tools.document_io import read_json
        timing_value = read_json(timing_path)
    if timing_validation is None:
        validate_xilinx_routed_timing(timing_path, mapped_path=mapped_path, _value=timing_value)
    if timing_value.get("schema") != "emuflow.xilinx-routed-timing/v2":
        raise ValidationError("native routed timing requires the current endpoint stream")
    from tests.routed_endpoint_adapter import bind_routed_timing_sources
    timing_sources = bind_routed_timing_sources(timing_path, timing_value)
    paths = {2: Path(boundary_output_path)}
    if logic_identity_path is not None:
        paths[3] = Path(logic_output_path)
    if local_identity_path is not None:
        paths[4] = Path(local_output_path)
    with ExitStack() as lifetime:
        views = {2: lifetime.enter_context(read_boundary_identity_storage(boundary_identity_path))}
        if 3 in paths:
            views[3] = lifetime.enter_context(read_logic_segment_identity_storage(logic_identity_path))
        if 4 in paths:
            views[4] = lifetime.enter_context(read_local_path_identity(local_identity_path))
        headers = {2: boundary_timing_header(views[2], provider="rapidwright-lightweight-segment-graph-v1",
                                            qualification=XILINX_SEGMENT_QUALIFICATION)}
        if 3 in views:
            identity = views[3]
            headers[3] = {"schema": "emuflow.logic-segment-timing/v1", "status": "pass",
                          **{key: identity[key] for key in ("design", "platform", "fpga", "system_routes_sha256", "tdm_schedule_sha256")},
                          "provider": "rapidwright-lightweight-logic-segment-graph-v1",
                          "qualification": XILINX_SEGMENT_QUALIFICATION,
                          "coverage": {**identity["coverage"], "endpoint_exact_segments": identity["coverage"]["segments"], "cone_bound_segments": 0}}
            if "semantic_contract_sha256" in identity:
                headers[3]["semantic_contract_sha256"] = identity["semantic_contract_sha256"]
        if 4 in views:
            headers[4] = local_path_timing_header(
                views[4], provider="rapidwright-lightweight-local-path-graph-v1",
                qualification="source-bound-routed-endpoint-longest-path-upper-bound-with-launch-clock-to-q-and-capture-setup",
            )
        if any((header["design"], header["fpga"]) != (headers[2]["design"], headers[2]["fpga"])
               for header in headers.values()):
            raise ValidationError("routed timing projection design/FPGA namespaces disagree")
        identity_schemas = {2: "boundary-identity-store-v1", 3: "logic-segment-identity-store-v1", 4: "local-path-timing-store-v1"}
        sources = (*bind_view_sources((mapped_value, "xilinx-mapped-index-v2")),
                   bind_store_source(source_ir_path, "emuir-compact-v2"),
                   *timing_sources)
        if len(sources) != 4:
            raise ValidationError("native routed timing mapped ownership is missing")
        source_bindings = sources
        for kind, view in views.items():
            bindings = bind_view_sources((view, identity_schemas[kind]))
            primary = [s for s in bindings if s.path == Path(view.store_path).resolve() and s.schema == identity_schemas[kind]]
            if len(primary) != 1:
                raise ValidationError("native routed timing identity ownership is missing")
            sources += (primary[0],)
            source_bindings += bindings
        for index, (kind, path) in enumerate(paths.items()):
            validate_store_destination(path, source_bindings)
            for other in list(paths.values())[:index]:
                if path.resolve() == other.resolve() or (path.exists() and other.exists() and os.path.samefile(path, other)):
                    raise ValidationError("routed timing outputs alias each other")
            path.parent.mkdir(parents=True, exist_ok=True)
        for kind, path in sorted(paths.items(), key=lambda item: str(item[1].resolve())):
            name = {2: "boundary_timing", 3: "logic_segment_timing", 4: "local_path_timing"}[kind]
            lease = importlib.import_module("emuflow_tools.timing." + name + "_storage")._Lease(path, exclusive=True)
            lifetime.callback(lease.close)
        with tempfile.TemporaryDirectory(prefix=".routed-segments.", dir=paths[2].parent) as temporary:
            root = Path(temporary)
            stores = root / "stores"
            stores.mkdir()
            request, summary = root / "request.bin", root / "summary.json"
            writer = CheckedRequestWriter(request)
            try:
                flags = int(3 in paths) | (int(4 in paths) << 1)
                writer.put("QII", 0x31544752464D5545, 1, flags)
                for source in sources:
                    writer.put("3Q4q", *source.generation)
                for kind in (2, 3, 4):
                    writer.text(json.dumps(headers[kind], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) if kind in headers else "")
                writer.finish()
            finally:
                writer.close()
            require_store_sources_unchanged(source_bindings)
            with NativeToolProcess(executable, [executable, "physical", "build-routed-segments", str(summary), str(stores),
                                               *(str(s.path) for s in sources), str(request)],
                                   environment=dict(os.environ), scratch_parent=root) as process:
                completed = process.finish()
            if completed.returncode:
                raise ValidationError("native routed timing failed: " + completed.stdout)
            require_store_sources_unchanged(source_bindings)
            if summary.stat().st_size > 8 * 1024 * 1024:
                raise ValidationError("native routed timing summary exceeds bound")
            result = json.loads(summary.read_text())
            if (result.get("schema") != "emuflow.routed-segment-summary/v1" or result.get("status") != "pass"
                    or result.get("implementation_id") != "native-routed-segments-v1"
                    or [p.get("kind") for p in result.get("projections", [])] != list(paths)
                    or any(type(result.get(key)) is not int or result[key] < 0
                           for key in ("nodes", "edges", "solves", "visited_nodes", "undriven_bit_count"))):
                raise ValidationError("native routed timing summary disagrees")
            output = {}
            for projection in result["projections"]:
                kind = projection["kind"]
                name = {2: "boundary", 3: "logic", 4: "local"}[kind]
                key = {2: "boundary", 3: "logic_segment", 4: "local_path"}[kind]
                output[key] = publish_physical_projection(stores / (name + ".sqlite"), paths[kind],
                                                         kind=kind, result=projection, sources=source_bindings)
            output["source_constraints"] = {"provider": "source-declared-undriven-inputs-v1", "source_ir": str(source_ir_path),
                                             "undriven_bit_count": result["undriven_bit_count"],
                                             "timing_policy": "disconnected; no fabricated driver or timing source"}
            output["native_graph"] = {key: result[key] for key in ("implementation_id", "nodes", "edges", "solves", "visited_nodes")}
            output["native_graph"]["engine_metrics"] = completed.native_metrics
            return output
