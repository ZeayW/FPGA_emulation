"""Endpoint-exact physical timing for DUT logic between TDM boundaries."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from .errors import ValidationError
from .store_publication import bind_view_sources, bind_store_source
from .platform import Platform


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
from .logic_segment_query_native import (
    LogicSegmentQueryInputs,
    prepare_logic_segment_query_inputs,
    write_logic_segment_query as _write_logic_segment_query,
)


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


def validate_logic_segment_identity(
    database: Mapping[str, Any],
    *,
    require_source_seals: bool = True,
) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_identity", None)
    if callable(indexed_validator):
        return indexed_validator()
    if database.get("schema") != LOGIC_SEGMENT_IDENTITY_SCHEMA:
        raise ValidationError("logic segment identity schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("logic segment identity did not pass")
    fpga = database.get("fpga")
    if not isinstance(fpga, str) or not fpga:
        raise ValidationError("logic segment identity FPGA is invalid")
    exact_digest = database.get("semantic_contract_sha256")
    if exact_digest is not None and (
        not isinstance(exact_digest, str)
        or len(exact_digest) != 64
        or any(character not in "0123456789abcdef" for character in exact_digest)
    ):
        raise ValidationError("logic segment exact contract digest is invalid")
    for field in ("system_routes_sha256", "tdm_schedule_sha256"):
        digest = database.get(field)
        if digest is None and not require_source_seals:
            continue
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(f"logic segment {field} seal is invalid")
    ids = set()
    members = set()
    paths = set()
    for index, segment in enumerate(database.get("segments", [])):
        context = f"logic segment identity[{index}]"
        if not isinstance(segment, dict):
            raise ValidationError(f"{context} is invalid")
        segment_id = segment.get("id")
        if (
            not isinstance(segment_id, str)
            or not segment_id
            or segment_id in ids
            or segment.get("kind") not in {"launch", "transition", "capture"}
            or segment.get("fpga") != fpga
        ):
            raise ValidationError(f"{context} identity is invalid")
        ids.add(segment_id)
        exact_segment_id = segment.get("static_exact_segment_id")
        if exact_digest is not None and (
            not isinstance(exact_segment_id, str) or not exact_segment_id
        ):
            raise ValidationError(
                f"{context}.static_exact_segment_id is invalid"
            )
        if exact_digest is None and exact_segment_id is not None:
            raise ValidationError(
                f"{context} has an unbound static exact segment identity"
            )
        for field in ("system_path", "member_path", "start_pin", "end_pin"):
            if not isinstance(segment.get(field), str) or not segment[field]:
                raise ValidationError(f"{context}.{field} is invalid")
        object_kind_fields = {"start_object_kind", "end_object_kind"}
        present_object_kinds = object_kind_fields & set(segment)
        if present_object_kinds and (
            present_object_kinds != object_kind_fields
            or segment["start_object_kind"] not in {"pin", "port"}
            or segment["end_object_kind"] not in {"pin", "port"}
        ):
            raise ValidationError(f"{context} object kinds are invalid")
        anchor_fields = {
            "cone_anchor_object_kind",
            "cone_anchor_pin",
        }
        present_anchor_fields = anchor_fields & set(segment)
        if present_anchor_fields and (
            present_anchor_fields != anchor_fields
            or segment["kind"] == "capture"
            or segment["cone_anchor_object_kind"] not in {"pin", "port"}
            or not isinstance(segment["cone_anchor_pin"], str)
            or not segment["cone_anchor_pin"]
        ):
            raise ValidationError(f"{context} cone anchor is invalid")
        replacement = segment.get("replace_tx_endpoint")
        if (segment["kind"] == "capture") != (replacement is None):
            raise ValidationError(f"{context} replacement is invalid")
        members.add(segment["member_path"])
        paths.add(segment["system_path"])
    return {
        "status": "pass",
        "segments": len(ids),
        "member_paths": len(members),
        "system_paths": len(paths),
    }


def _indexed_logic_measurement_import(input_path, identity, output_path, *, provider, qualification,
                                       allow_missing, allow_legacy_timing_format):
    from .physical_measurements_native import import_physical_measurements
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


def import_vivado_logic_segment_timing(
    input_path: Path,
    identity_path: Path,
    output_path: Path,
    *,
    provider: str = "vivado-routed-logic-segment-datapath-v1",
    qualification: str = "routed-vendor-device-endpoint-chain",
    allow_missing: bool = False,
    allow_legacy_timing_format: bool = False,
) -> Dict[str, Any]:
    from .logic_segment_identity_storage import (
        LogicSegmentIdentityView,
        read_logic_segment_identity_storage,
    )

    identity = read_logic_segment_identity_storage(
        identity_path
    )
    if isinstance(identity, LogicSegmentIdentityView):
        try:
            return _indexed_logic_measurement_import(
                input_path,
                identity,
                output_path,
                provider=provider,
                qualification=qualification,
                allow_missing=allow_missing,
                allow_legacy_timing_format=allow_legacy_timing_format,
            )
        finally:
            identity.close()
    raise ValidationError("native physical timing import requires a current typed identity")


def validate_logic_segment_timing(
    database: Mapping[str, Any],
) -> Dict[str, Any]:
    indexed_validator = getattr(database, "validate_timing", None)
    if callable(indexed_validator):
        return indexed_validator()
    if database.get("schema") != LOGIC_SEGMENT_TIMING_SCHEMA:
        raise ValidationError("logic segment timing schema is invalid")
    if database.get("status") != "pass":
        raise ValidationError("logic segment timing did not pass")
    identity = {
        key: database[key]
        for key in (
            "status",
            "design",
            "platform",
            "fpga",
            "coverage",
            "unsupported_member_paths",
            "segments",
        )
    }
    for key in ("system_routes_sha256", "tdm_schedule_sha256"):
        if key in database:
            identity[key] = database[key]
    if "semantic_contract_sha256" in database:
        identity["semantic_contract_sha256"] = database[
            "semantic_contract_sha256"
        ]
    identity["schema"] = LOGIC_SEGMENT_IDENTITY_SCHEMA
    identity["provider"] = "timing-validation"
    validate_logic_segment_identity(identity, require_source_seals=False)
    maximum = 0.0
    endpoint_exact = 0
    cone_bound = 0
    collection_bound = 0
    for segment in database["segments"]:
        delay = segment.get("delay_ns")
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(float(delay))
            or float(delay) < 0.0
        ):
            raise ValidationError("logic segment timing delay is invalid")
        measurement = segment.get("measurement", "endpoint-exact")
        if measurement not in {
            "endpoint-exact",
            "cut-net-cone-upper-bound",
            "optimized-endpoint-collection-upper-bound",
        }:
            raise ValidationError("logic segment timing measurement is invalid")
        if measurement == "cut-net-cone-upper-bound":
            if segment.get("kind") == "capture" or not isinstance(
                segment.get("cone_anchor_pin"), str
            ):
                raise ValidationError(
                    "logic segment cone-bound trace is invalid"
                )
            cone_bound += 1
        elif measurement == "optimized-endpoint-collection-upper-bound":
            collection_bound += 1
        else:
            endpoint_exact += 1
        for field in ("actual_start_object", "actual_end_object"):
            if field in segment and (
                not isinstance(segment[field], str) or not segment[field]
            ):
                raise ValidationError(
                    "logic segment physical object trace is invalid"
                )
        maximum = max(maximum, float(delay))
    return {
        "status": "pass",
        "fpga": database["fpga"],
        "segments": len(database["segments"]),
        "endpoint_exact_segments": endpoint_exact,
        "cone_bound_segments": cone_bound,
        "endpoint_collection_bound_segments": collection_bound,
        "maximum_delay_ns": maximum,
    }
