"""Bounded adapter for independent native Static Exact validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import bind_view_sources, require_store_sources_unchanged
from .tool_runtime import NativeToolProcess


def validate_native_exact_schedule(
    routes, platform, schedule, constraints, arcs, allocations
):
    """Pass immutable owners and board-sized configuration, never route records."""
    from .tdm import _write_native_schedule_board_request
    from .routing import build_directed_graph

    raw = routes["routes"]
    sources = bind_view_sources(
        (raw, "system-route-store-v3"), (schedule, "tdm-schedule-store-v4")
    )
    route_path, store_path = (
        Path(raw.store_path).resolve(),
        Path(schedule.store_path).resolve(),
    )
    route_source = next(
        (source for source in sources if source.path == route_path), None
    )
    store_source = next(
        (source for source in sources if source.path == store_path), None
    )
    if route_source is None or store_source is None:
        raise ValidationError("native schedule validation source ownership is missing")
    require_store_sources_unchanged(sources)
    header = schedule.native_validation_header()
    _, _, capacities = build_directed_graph(platform, constraints)
    catalogs = (
        tuple(sorted(fpga.id for fpga in platform.fpgas)),
        tuple(sorted(link.id for link in platform.links)),
        tuple(sorted(capacities)),
    )
    links = {link.id: link for link in platform.links}
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix="emuflow-exact-validation-") as temporary:
        root = Path(temporary)
        board, request, output = (
            root / "board.bin",
            root / "request.bin",
            root / "summary.json",
        )
        _write_native_schedule_board_request(
            board,
            route_source.generation,
            platform,
            constraints,
            arcs,
            allocations,
            *catalogs,
        )
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QI3Q4q", 0x3156534D44544545, 1, *store_source.generation)
            writer.put("I", len(header))
            for key, value in sorted(header.items()):
                writer.text(key)
                writer.text(
                    json.dumps(
                        value,
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    )
                )
            writer.put("I", len(capacities))
            for key in sorted(capacities):
                capacity = capacities[key]
                lanes = links[capacity["link"]].transport_bits_per_cycle_per_direction
                writer.text(
                    json.dumps(
                        {
                            "key": key,
                            "link": capacity["link"],
                            "direction": capacity["direction"],
                            "lanes": lanes,
                            "frame_slots": constraints["frame_slots"],
                            "capacity_bit_hops": lanes * constraints["frame_slots"],
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                        allow_nan=False,
                    )
                )
            for key in ("demand_completions", "domain_schedules", "entries", "routes"):
                section = schedule.manifest["sections"][key]
                writer.put("Q", section["records"])
                writer.text(section["sha256"])
            writer.text(schedule.manifest["logical_sha256"])
            writer.text(schedule.manifest["logical_pretty_sha256"])
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        with NativeToolProcess(
            executable,
            [
                executable,
                "phase5",
                "validate-schedule",
                str(route_path),
                str(board),
                str(store_path),
                str(request),
                str(output),
            ],
            environment=dict(os.environ),
            timeout_ms=21600000,
            scratch_parent=root,
        ) as process:
            completed = process.finish()
        if completed.returncode:
            raise ValidationError(
                "native independent schedule validation failed: " + completed.stdout
            )
        require_store_sources_unchanged(sources)
        try:
            if output.stat().st_size > 4096:
                raise ValidationError(
                    "native schedule validation summary exceeds bound"
                )
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValidationError(
                "native schedule validation summary is unreadable"
            ) from error
    expected = {
        **header["metrics"],
        "schema": "emuflow.exact-schedule-validation/v1",
        "implementation_id": "native-independent-exact-schedule-validation-v1",
        "status": "pass",
        "qualification": "dependency-schedule-readiness-pass",
        "routed_sinks": header["schedule_dependency_certificate"][
            "sink_readiness_checks"
        ],
    }
    if (
        not isinstance(result, dict)
        or result != expected
        or result["demands"] != len(raw)
    ):
        raise ValidationError("native schedule validation summary disagrees")
    result.pop("schema")
    return result
