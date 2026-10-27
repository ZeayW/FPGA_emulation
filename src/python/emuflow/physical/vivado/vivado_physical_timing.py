"""Indexed, source-bound timing handoffs for the current Vivado backend."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from emuflow.timing.boundary_identity_storage import read_boundary_identity_storage
from emuflow.timing.boundary_timing import boundary_timing_header
from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import bind_view_sources, bind_store_source
from emuflow.timing.local_path_timing import local_path_timing_header, read_local_path_identity


def write_vivado_local_query(merged_ir: Path, fpga: str, query_path: Path,
        identity_path: Path, prepared: Any) -> dict[str, Any]:
    from emuflow.timing.local_path_query_native import write_native_local_path_query
    return write_native_local_path_query(merged_ir, fpga, query_path, identity_path, prepared, backend='vivado')


def import_vivado_local_timing(input_path: Path, identity_path: Path, output_path: Path) -> dict[str, Any]:
    from emuflow.physical.physical_measurements_native import import_physical_measurements
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
    from emuflow.physical.physical_measurements_native import import_physical_measurements
    from emuflow.timing.boundary_identity_storage import BoundaryIdentityView
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
