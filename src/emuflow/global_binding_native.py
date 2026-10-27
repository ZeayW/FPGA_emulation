"""Bounded selection/configuration adapter for native physical event binding."""
from collections.abc import Mapping
import json
import math
from pathlib import Path

from .errors import ValidationError
from .global_event_store import _Owner, _native, EventChecks, PathMetadata
from .native_protocol import CheckedRequestWriter
from .store_publication import bind_view_sources, require_store_sources_unchanged


def has_native_physical_inputs(routes, schedule, physical):
    from .phase5_timing_native import has_native_timing_inputs
    from .boundary_timing_storage import BoundaryTimingView
    from .boundary_identity_storage import BoundaryIdentityView
    from .logic_segment_timing_storage import LogicSegmentTimingView
    from .local_path_timing_storage import LocalPathTimingView

    if not isinstance(physical, Mapping):
        return False
    groups = [(physical.get(key, {}), kind) for key, kind in (
        ("boundary_timing", BoundaryTimingView),
        ("boundary_identities", BoundaryIdentityView),
        ("logic_segment_timing", LogicSegmentTimingView),
        ("local_path_timing", LocalPathTimingView),
    )]
    selected = any(isinstance(value, kind) for group, kind in groups
                   if isinstance(group, Mapping) for value in group.values())
    if not selected:
        return False  # Explicit inline/reference facts, never a failed native fallback.
    if (not has_native_timing_inputs(routes, schedule)
            or any(not isinstance(group, Mapping) or any(not isinstance(value, kind) for value in group.values())
                   for group, kind in groups)):
        raise ValidationError("native physical binding requires complete current typed owners")
    boundary, identity, logic, _ = (group for group, _ in groups)
    if set(boundary) != set(identity) or set(boundary) != set(logic):
        raise ValidationError("native physical binding FPGA coverage disagrees")
    if any(len(group) > 256 for group, _ in groups):
        raise ValidationError("native physical binding FPGA catalog exceeds bound")
    return True


def bind_native_physical_checks(runtime, routes, schedule, physical, platform, *,
                                metadata=None, routes_artifact_sha256=None):
    from .routing import build_directed_graph, normalize_route_constraints
    from .tdm import _bank_direction_allocations, _write_native_schedule_board_request
    from .physical_timing_inputs import _board_link_delay_database
    from .board_link_timing import build_board_link_timing_model
    from .system_route_storage import pretty_system_routes_sha256

    raw = routes["routes"]
    boundary_fpgas = list(physical.get("boundary_timing", {}))
    local_fpgas = list(physical.get("local_path_timing", {}))
    views = [(raw, "system-route-store-v3"), (schedule, "tdm-schedule-store-v4")]
    for fpga in boundary_fpgas:
        for key, schema in (("boundary_timing", "boundary-timing-store-v1"),
                            ("boundary_identities", "boundary-identity-store-v1"),
                            ("logic_segment_timing", "logic-segment-timing-store-v1")):
            views.append((physical[key][fpga], schema))
    views.extend((physical["local_path_timing"][fpga], "local-path-timing-store-v1") for fpga in local_fpgas)
    sources = bind_view_sources(*views)
    paths = [Path(view.store_path).resolve() for view, _ in views]
    originals = [next((source for source in sources if source.path == path), None) for path in paths]
    if any(source is None for source in originals):
        raise ValidationError("native physical binding source ownership is missing")
    constraints = normalize_route_constraints(routes.get("constraints"), platform)
    _, arcs, capacities = build_directed_graph(platform, constraints)
    allocations = _bank_direction_allocations(routes, platform, constraints)
    links = _board_link_delay_database(physical, platform)
    if links is None:
        links = _board_link_delay_database({"board_link_timing": build_board_link_timing_model(platform)}, platform)
    clocks = runtime.get("virtual_dut_clocks")
    if not isinstance(clocks, Mapping) or not clocks:
        raise ValidationError("virtual runtime lacks DUT clock domains")
    uncertainty = physical.get("static_exact_clock_uncertainty_ns", 0.0)
    if type(uncertainty) not in (int, float) or not math.isfinite(uncertainty) or uncertainty < 0:
        raise ValidationError("invalid global STA uncertainty")
    for name, value in clocks.items():
        period = value.get("nominal_period_ns") if isinstance(value, Mapping) else None
        if (not isinstance(name, str) or not name or type(period) not in (int, float)
                or not math.isfinite(period) or period <= 0):
            raise ValidationError("invalid virtual runtime clock")
    board_links = {link.id: link for link in platform.links}
    config = {
        "design": routes["design"], "platform": routes["platform"],
        "boundary_fpgas": boundary_fpgas, "local_fpgas": local_fpgas,
        "uncertainty": float(uncertainty),
        "clocks": [[name, value["nominal_period_ns"]] for name, value in clocks.items()],
        "links": [[key[0], key[1], key[2], delay,
                   board_links[key[0]].tdm_delay_model.ratio_coefficient_ns if board_links[key[0]].tdm_delay_model else None,
                   board_links[key[0]].tdm_delay_model.intercept_ns if board_links[key[0]].tdm_delay_model else None,
                   board_links[key[0]].tdm_delay_model.min_ratio if board_links[key[0]].tdm_delay_model else None,
                   board_links[key[0]].tdm_delay_model.max_ratio if board_links[key[0]].tdm_delay_model else None,
                   board_links[key[0]].tdm_delay_model.ratio_quantum if board_links[key[0]].tdm_delay_model else None]
                  for key, delay in sorted(links.items())],
    }
    encoded = json.dumps(config, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 4 * 1024 * 1024:
        raise ValidationError("native physical binding configuration exceeds bound")
    owner = _Owner()
    try:
        board, request = owner.root / "board.bin", owner.root / "request.bin"
        _write_native_schedule_board_request(
            board, originals[0].generation, platform, constraints, arcs, allocations,
            tuple(sorted(fpga.id for fpga in platform.fpgas)),
            tuple(sorted(link.id for link in platform.links)), tuple(sorted(capacities)),
        )
        from .store_publication import bind_store_source
        board_source = bind_store_source(board, "exact-schedule-board-request-v1")
        input_paths = paths[:2] + [board] + paths[2:]
        input_sources = originals[:2] + [board_source] + originals[2:]
        writer = CheckedRequestWriter(request)
        try:
            writer.put("QIII", 0x31424747464D5545, 1, len(boundary_fpgas), len(local_fpgas))
            for source in input_sources:
                writer.put("3Q4q", *source.generation)
            for value in (raw.native_logical_identity(), pretty_system_routes_sha256(routes),
                          schedule.manifest["logical_sha256"], schedule.manifest["logical_pretty_sha256"], encoded):
                writer.text(value)
            writer.finish()
        finally:
            writer.close()
        require_store_sources_unchanged(sources)
        _native(["bind-events", *input_paths, request, owner.primary], owner.root)
        require_store_sources_unchanged(sources)
        binding = owner.accept_native()
        rows, paths_metadata = EventChecks(owner), PathMetadata(owner)
        source = binding["source_binding"]
        if metadata is not None:
            if source is not None and (
                    source.get("routes_sha256") != routes_artifact_sha256
                    or source.get("original_paths") != len(paths_metadata)
                    or source.get("original_path_ids_sha256") != paths_metadata.path_id_set_sha256()):
                raise ValidationError("global STA original population/source binding disagrees")
            metadata.update(paths=paths_metadata, source_binding=source,
                            compressed_representative_paths=binding["compressed_representative_paths"])
        require_store_sources_unchanged(sources)
        board.unlink()
        request.unlink()
        return rows
    except BaseException:
        owner.close()
        raise
