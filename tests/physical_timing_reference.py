"""Small independent physical timing import oracle; never a production route."""
from __future__ import annotations
import math
from pathlib import Path
from typing import Any, Dict
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.store_publication import bind_store_source
from emuflow.logic_segment_timing import (
    LOGIC_SEGMENT_TIMING_SCHEMA, VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
    validate_logic_segment_identity,
)
_LEGACY_VIVADO_LOGIC_SEGMENT_TIMING_HEADER = "endpoint_hex\tkind\tdelay_ns\tstart_object_hex\tend_object_hex"


def import_reference_logic_timing(input_path,identity_path,output_path,*,
        provider="vivado-routed-logic-segment-datapath-v1",
        qualification="routed-vendor-device-endpoint-chain",allow_missing=False,
        allow_legacy_timing_format=False):
    identity=read_json(identity_path)
    source_bindings = (
        bind_store_source(input_path, "physical-timing-measurements-v1"),
    )
    validate_logic_segment_identity(identity)
    expected = {item["id"]: item for item in identity["segments"]}
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] not in {
        VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
        _LEGACY_VIVADO_LOGIC_SEGMENT_TIMING_HEADER,
    }:
        raise ValidationError("Vivado logic segment timing header is invalid")
    extended_format = lines[0] == VIVADO_LOGIC_SEGMENT_TIMING_HEADER
    if not extended_format and not allow_legacy_timing_format:
        raise ValidationError(
            "legacy Vivado logic timing format requires explicit compatibility"
        )
    measurements = {}
    for line_number, line in enumerate(lines[1:], start=2):
        if not line:
            continue
        fields = line.split("\t")
        try:
            segment_id = bytes.fromhex(fields[0]).decode("utf-8")
            start_object = bytes.fromhex(fields[3]).decode("utf-8")
            end_object = bytes.fromhex(fields[4]).decode("utf-8")
            measurement = fields[5] if extended_format else "endpoint-exact"
            actual_start = (
                bytes.fromhex(fields[6]).decode("utf-8")
                if extended_format
                else start_object
            )
            actual_end = (
                bytes.fromhex(fields[7]).decode("utf-8")
                if extended_format
                else end_object
            )
        except (IndexError, ValueError, UnicodeDecodeError) as error:
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} is invalid"
            ) from error
        expected_fields = 8 if extended_format else 5
        segment = (
            expected.get(segment_id)
            if len(fields) == expected_fields
            else None
        )
        if (
            segment is None
            or segment_id in measurements
            or fields[1] != segment["kind"]
            or start_object != segment["start_pin"]
            or end_object != segment["end_pin"]
            or measurement not in {
                "endpoint-exact",
                "cut-net-cone-upper-bound",
                "optimized-endpoint-collection-upper-bound",
            }
            or not actual_start
            or not actual_end
        ):
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} is invalid"
            )
        try:
            delay = float(fields[2])
        except ValueError as error:
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} delay is invalid"
            ) from error
        if not math.isfinite(delay) or delay < 0.0:
            raise ValidationError(
                f"Vivado logic segment timing line {line_number} delay is invalid"
            )
        measurements[segment_id] = {
            "delay_ns": delay,
            "measurement": measurement,
            "actual_start_object": actual_start,
            "actual_end_object": actual_end,
        }
    missing = sorted(set(expected) - set(measurements))
    if missing and not allow_missing:
        raise ValidationError(
            "Vivado logic segment timing coverage is incomplete: "
            f"{missing[:10]}"
        )
    unsupported = {
        item["path"]: item["reason"]
        for item in identity["unsupported_member_paths"]
    }
    for segment_id in missing:
        segment = expected[segment_id]
        unsupported[segment["member_path"]] = (
            "no-routed-vivado-board-path"
        )
    header = {
        "schema": LOGIC_SEGMENT_TIMING_SCHEMA,
        "status": "pass",
        "design": identity["design"],
        "platform": identity["platform"],
        "fpga": identity["fpga"],
        "provider": provider,
        "qualification": qualification,
        "coverage": identity["coverage"],
        "system_routes_sha256": identity["system_routes_sha256"],
        "tdm_schedule_sha256": identity["tdm_schedule_sha256"],
        **(
            {
                "semantic_contract_sha256": identity[
                    "semantic_contract_sha256"
                ]
            }
            if "semantic_contract_sha256" in identity
            else {}
        ),
    }
    from emuflow.logic_segment_timing_storage import write_logic_segment_timing_storage

    view = write_logic_segment_timing_storage(
        output_path,
        header=header,
        segments=(
            {**segment, **measurements[segment["id"]]}
            for segment in identity["segments"]
            if segment["id"] in measurements
        ),
        unsupported_member_paths=(
            {"path": path, "reason": reason}
            for path, reason in sorted(unsupported.items())
        ),
        unmeasured_segments=(
            {
                "id": segment_id,
                "kind": expected[segment_id]["kind"],
                "system_path": expected[segment_id]["system_path"],
                "member_path": expected[segment_id]["member_path"],
                "reason": "no-routed-vivado-board-path",
            }
            for segment_id in missing
        ),
        source_bindings=source_bindings,
    )
    try:
        validation = view.validate_timing()
    finally:
        view.close()
    return {
        **validation,
        "missing_segments": len(missing),
        "output": str(output_path),
    }

