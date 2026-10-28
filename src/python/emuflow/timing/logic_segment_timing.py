"""Endpoint-exact physical timing for DUT logic between TDM boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from emuflow.common.errors import ValidationError
from emuflow.storage.store_publication import bind_view_sources, bind_store_source
from emuflow.platform.platform import Platform


LOGIC_SEGMENT_IDENTITY_SCHEMA = "emuflow.logic-segment-identity/v1"
LOGIC_SEGMENT_TIMING_SCHEMA = "emuflow.logic-segment-timing/v1"
LOGIC_SEGMENT_QUERY_HEADER = "endpoint\tkind\tstart_pin\tend_pin"
LOGIC_SEGMENT_TIMING_HEADER = (
    "endpoint\tkind\tdelay_ns\tstart_pin\tend_pin"
)
VIVADO_LOGIC_SEGMENT_QUERY_HEADER = (
    "endpoint_hex\tkind\tstart_kind\tstart_object_hex\t"
    "end_kind\tend_object_hex\tcone_anchor_kind\tcone_anchor_object_hex"
)
VIVADO_LOGIC_SEGMENT_TIMING_HEADER = (
    "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex\t"
    "measurement\tactual_start_object_hex\tactual_end_object_hex"
)
from emuflow.timing.logic_segment_query_native import LogicSegmentQueryInputs, prepare_logic_segment_query_inputs, write_logic_segment_query as _write_logic_segment_query


def write_vivado_logic_segment_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
    merged_ir_path: Path,
    boundary_identity_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
) -> Dict[str, Any]:
    return _write_logic_segment_query(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        schedule_path,
        platform,
        merged_ir_path,
        boundary_identity_path,
        fpga,
        query_path,
        identity_path,
        object_provider="vivado",
        prepared_inputs=prepared_inputs,
    )


def write_xilinx_logic_segment_query(
    original_ir_path: Path,
    assignment_path: Path,
    path_database_path: Path,
    routes_path: Path,
    schedule_path: Path,
    platform: Platform,
    merged_ir_path: Path,
    boundary_identity_path: Path,
    fpga: str,
    query_path: Path,
    identity_path: Path,
    *,
    prepared_inputs: Optional[LogicSegmentQueryInputs] = None,
) -> Dict[str, Any]:
    return _write_logic_segment_query(
        original_ir_path,
        assignment_path,
        path_database_path,
        routes_path,
        schedule_path,
        platform,
        merged_ir_path,
        boundary_identity_path,
        fpga,
        query_path,
        identity_path,
        object_provider="xilinx",
        prepared_inputs=prepared_inputs,
    )


def validate_logic_segment_identity(database: Mapping[str, Any]) -> Dict[str, Any]:
    from emuflow.timing.logic_segment_identity_storage import LogicSegmentIdentityView
    if not isinstance(database, LogicSegmentIdentityView):
        raise ValidationError("logic segment identity requires a current typed owner")
    return database.validate_identity()


def _indexed_logic_measurement_import(input_path, identity, output_path, *, provider, qualification,
                                       allow_missing, allow_legacy_timing_format):
    from emuflow.physical.physical_measurements_native import import_physical_measurements
    sources = bind_view_sources((identity, "logic-segment-identity-store-v1")) + (
        bind_store_source(input_path, "physical-timing-measurements-v1"),
    )
    header = {key: identity[key] for key in (
        "design", "platform", "fpga", "coverage", "system_routes_sha256", "tdm_schedule_sha256",
    )}
    header.update(schema=LOGIC_SEGMENT_TIMING_SCHEMA, status="pass", provider=provider, qualification=qualification)
    if "semantic_contract_sha256" in identity:
        header["semantic_contract_sha256"] = identity["semantic_contract_sha256"]
    return import_physical_measurements(
        input_path, identity.store_path, output_path, kind=3, header=header,
        sources=sources, source=identity.selector["source"], allow_missing=allow_missing,
        allow_legacy=allow_legacy_timing_format,
    )



def validate_logic_segment_timing(database: Mapping[str, Any]) -> Dict[str, Any]:
    from emuflow.timing.logic_segment_timing_storage import LogicSegmentTimingView
    if not isinstance(database, LogicSegmentTimingView):
        raise ValidationError("logic segment timing requires a current typed owner")
    return database.validate_timing()
