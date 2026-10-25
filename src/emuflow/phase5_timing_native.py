"""Typed-owner adapter; no path, member, route or hop populations cross Python."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile

from .errors import ValidationError
from .native_protocol import CheckedRequestWriter
from .native_tools import resolve_native_executable
from .store_publication import bind_view_sources, require_store_sources_unchanged
from .tdm_schedule_storage import TDMScheduleView
from .tool_runtime import NativeToolProcess


def has_native_timing_inputs(routes, schedule=None):
    return getattr(routes.get("routes"), "_user_version", None) == 3 and (
        schedule is None or isinstance(schedule, TDMScheduleView)
    )


def project_native_schedule_timing(
    routes, schedule=None, platform=None, *, bind=True, analyze=False
):
    if not has_native_timing_inputs(routes, schedule) or (not bind and not analyze):
        raise ValidationError("native timing projection requires current typed owners")
    if analyze and (schedule is None or platform is None):
        raise ValidationError("native timing analysis requires schedule and board")
    raw = routes["routes"]
    views = [(raw, "system-route-store-v3")]
    if schedule is not None:
        views.append((schedule, "tdm-schedule-store-v4"))
    sources = bind_view_sources(*views)
    route_path = Path(raw.store_path).resolve()
    store_paths = [route_path]
    if schedule is not None:
        store_paths.append(Path(schedule.store_path).resolve())
    originals = [
        next((source for source in sources if source.path == path), None)
        for path in store_paths
    ]
    if any(source is None for source in originals):
        raise ValidationError("native timing projection source ownership is missing")
    route_identity = raw.native_logical_identity()
    executable = resolve_native_executable("emuflow-native")
    with tempfile.TemporaryDirectory(prefix="emuflow-phase5-timing-") as temporary:
        root = Path(temporary)
        request, output = root / "request.bin", root / "summary.json"
        inputs = [str(path) for path in store_paths]
        affine = False
        if analyze:
            from .routing import build_directed_graph, normalize_route_constraints
            from .tdm import (
                _bank_direction_allocations,
                _write_native_schedule_board_request,
            )

            constraints = normalize_route_constraints(
                routes.get("constraints"), platform
            )
            _, arcs, capacities = build_directed_graph(platform, constraints)
            allocations = _bank_direction_allocations(routes, platform, constraints)
            board = root / "board.bin"
            _write_native_schedule_board_request(
                board,
                originals[0].generation,
                platform,
                constraints,
                arcs,
                allocations,
                tuple(sorted(fpga.id for fpga in platform.fpgas)),
                tuple(sorted(link.id for link in platform.links)),
                tuple(sorted(capacities)),
            )
            inputs.append(str(board))
            affine = any(link.tdm_delay_model is not None for link in platform.links)
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QII", 0x31544354464D5545, 1, int(bind) | (2 * int(analyze)))
            for original in originals:
                writer.put("3Q4q", *original.generation)
            writer.text(route_identity)
            if schedule is not None:
                writer.text(schedule.manifest["logical_sha256"])
                writer.text(schedule.manifest["logical_pretty_sha256"])
            writer.put("B", int(affine))
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        with NativeToolProcess(
            executable,
            [
                executable,
                "phase5",
                "timing-contract",
                *inputs,
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
                "native Phase5 timing projection failed: " + completed.stdout
            )
        require_store_sources_unchanged(sources)
        try:
            if output.stat().st_size > 8192:
                raise ValidationError("native timing summary exceeds bound")
            result = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValidationError("native timing summary is unreadable") from error
    if (
        not isinstance(result, dict)
        or set(result)
        != {"schema", "status", "implementation_id", "contract", "timing_validation"}
        or result["schema"] != "emuflow.native-phase5-timing/v1"
        or result["status"] != "pass"
        or result["implementation_id"] != "native-route-schedule-timing-v1"
        or (bind != isinstance(result["contract"], dict))
        or (analyze != isinstance(result["timing_validation"], dict))
        or (not bind and result["contract"] is not None)
        or (not analyze and result["timing_validation"] is not None)
    ):
        raise ValidationError("native timing summary disagrees")
    if bind and (
        result["contract"].get("schema") != "emuflow.cross-layer-timing/v3"
        or result["contract"].get("route_binding", {}).get("sha256") != route_identity
    ):
        raise ValidationError("native cross-layer source binding disagrees")
    if bind:
        contract = result["contract"]
        metrics = contract.get("metrics", {})
        path_binding = contract.get("path_binding", {})
        if (
            not isinstance(metrics, dict)
            or not isinstance(path_binding, dict)
            or set(metrics)
            != {"paths", "path_cut_bindings", "logic_segments", "scheduled_hops"}
            or any(type(value) is not int or value < 0 for value in metrics.values())
            or metrics["logic_segments"] != 0
            or path_binding.get("records") != metrics["paths"]
            or type(path_binding.get("records")) is not int
            or path_binding.get("mode") != "canonical-route-store-path-projection-v1"
            or path_binding.get("source_user_version") != 3
            or not isinstance(path_binding.get("sha256"), str)
            or len(path_binding["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in path_binding["sha256"])
            or contract.get("design") != routes.get("design")
            or contract.get("platform") != routes.get("platform")
        ):
            raise ValidationError("native cross-layer projection summary disagrees")
    if analyze:
        timing = result["timing_validation"]
        integers = {"timing_paths", "negative_slack_paths"}
        numbers = {
            "worst_delay_ns",
            "worst_slack_ns",
            "worst_normalized_slack",
            "total_negative_slack_ns",
            "total_negative_normalized_slack",
            "p01_normalized_slack",
            "median_normalized_slack",
        }
        if (
            set(timing) != integers | numbers | {"status", "timing_model", "worst_path"}
            or timing.get("status") != "pass"
            or timing.get("timing_model")
            != ("per-hop-ratio-delay-v1" if affine else "projected-fixed-slot-event-v1")
            or any(
                type(timing.get(key)) is not int or timing[key] < 0 for key in integers
            )
            or any(
                type(timing.get(key)) not in (int, float)
                or not math.isfinite(timing[key])
                for key in numbers
            )
            or timing["timing_paths"] != len(routes["timing"]["paths"])
            or timing["timing_paths"] <= 0
            or not 0 <= timing["negative_slack_paths"] <= timing["timing_paths"]
            or not isinstance(timing.get("worst_path"), str)
            or not timing["worst_path"]
        ):
            raise ValidationError("native timing statistics summary disagrees")
    return result
