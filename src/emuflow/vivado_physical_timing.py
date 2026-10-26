"""Indexed, source-bound timing handoffs for the current Vivado backend."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .boundary_identity_storage import read_boundary_identity_storage
from .boundary_timing import boundary_timing_header
from .errors import ValidationError
from .store_publication import bind_view_sources, bind_store_source
from .indexed_physical_ir import IndexedPhysicalIR
from .local_path_timing import (LOCAL_PATH_IDENTITY_SCHEMA_V2, LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION,
                                local_path_timing_header, read_local_path_identity)
from .local_path_timing_storage import write_local_path_timing_storage
from .logic_segment_timing import VIVADO_LOGIC_SEGMENT_QUERY_HEADER, VIVADO_LOGIC_SEGMENT_TIMING_HEADER, _vivado_object


def _hex(text: str) -> str:
    return text.encode("utf-8").hex()


def write_vivado_local_query(
    merged_ir: Path, fpga: str, query_path: Path, identity_path: Path, prepared: Any,
) -> dict[str, Any]:
    """Project the original same-FPGA path population into one scratch owner."""
    targets = [Path(merged_ir).resolve(),Path(query_path).resolve(),Path(identity_path).resolve()]
    if len(set(targets)) != 3 or any(a.exists() and b.exists() and os.path.samefile(a,b)
                                    for i,a in enumerate(targets) for b in targets[:i]):
        raise ValidationError("Vivado local query destinations alias a source/output")
    paths = prepared.local_paths_by_fpga.get(fpga, ())
    identity_header = {
        "schema": LOCAL_PATH_IDENTITY_SCHEMA_V2, "status": "pass",
        "design": prepared.assignment["design"], "fpga": fpga,
        "provider": "vivado-source-bound-local-path-query-v1",
        "source": dict(prepared.source_manifest), "coverage": {"local_paths":len(paths)},
    }
    header = local_path_timing_header(identity_header, provider=identity_header["provider"],
                                      qualification=LOCAL_PATH_IDENTITY_STORAGE_QUALIFICATION)
    query_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix="."+query_path.name+".", suffix=".tmp", dir=query_path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8") as stream, IndexedPhysicalIR(merged_ir) as ir:
            stream.write(VIVADO_LOGIC_SEGMENT_QUERY_HEADER + "\n")
            def records():
                for source in paths:
                    start_kind,start = _vivado_object(ir,source["startpoint"],ir.pins,ir.instances)
                    end_kind,end = _vivado_object(ir,source["endpoint"],ir.pins,ir.instances)
                    record={"id":source["id"],"kind":"local","fpga":fpga,"clock_domain":source["clock_domain"],
                            "clock_period_ns":float(source["clock_period_ns"]),
                            "required_time_ns":float(source["fixed_delay_ns"])+float(source["slack_ns"]),
                            "start_pin":start,"end_pin":end,"measurement":"endpoint-longest-path-fallback", "path_pins":[],"delay_ns":0.0}
                    stream.write("\t".join((_hex(record["id"]),"local",start_kind,_hex(start),end_kind,_hex(end),"",""))+"\n")
                    yield record
            with write_local_path_timing_storage(identity_path,header,records(),
                    source_bindings=(bind_store_source(merged_ir,"physical-input-document-v1"),)):
                pass
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,query_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"status":"pass","fpga":fpga,"local_paths":len(paths),"query":str(query_path),"identity":str(identity_path)}


def import_vivado_local_timing(input_path: Path, identity_path: Path, output_path: Path) -> dict[str, Any]:
    from .physical_measurements_native import import_physical_measurements
    with read_local_path_identity(identity_path) as identity:
        header = local_path_timing_header(
            identity, provider="vivado-source-bound-local-endpoint-delay-v1",
            qualification="routed-vendor-device-local-endpoint-longest-path",
        )
        return import_physical_measurements(
            input_path, identity.store_path, output_path, kind=4, header=header,
            sources=bind_view_sources((identity,"local-path-timing-store-v1")) + (
                bind_store_source(input_path,"physical-timing-measurements-v1"),),
        )


def import_indexed_vivado_boundary(input_path: Path, identity_path: Path, output_path: Path, *,
                                  measurement_scope="partition-boundary",
                                  provider="vivado-indexed-routed-boundary-v1",
                                  qualification="routed-vendor-device-boundary") -> dict[str, Any]:
    from .physical_measurements_native import import_physical_measurements
    from .boundary_identity_storage import BoundaryIdentityView
    with read_boundary_identity_storage(identity_path) as identity:
        if not isinstance(identity, BoundaryIdentityView):
            raise ValidationError("native physical timing requires a typed boundary identity")
        header = boundary_timing_header(identity, provider=provider, qualification=qualification,
                                        measurement_scope=measurement_scope)
        return import_physical_measurements(
            input_path, identity.store_path, output_path, kind=2, header=header,
            sources=bind_view_sources((identity, "boundary-identity-store-v1")) + (
                bind_store_source(input_path, "physical-timing-measurements-v1"),),
        )
