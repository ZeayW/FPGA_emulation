"""Overlapping path incidences must not change candidate scores or coverage."""
from emuflow.partition import build_clusters, build_partition_assignment, normalize_partition_constraints
from emuflow.partition_pressure import (
    build_partition_pressure_model, run_partition_pressure_native,
    validate_partition_pressure_native_bundle,
)
from emuflow.platform import Platform
from emuflow.routing import normalize_route_constraints
from tests.native_build import emuflow_native
from tests.test_partition_pressure import _chain_ir, _endpoint
from tests.test_phase5 import _link, _platform_value


def overlap_case(members=512, clusters_count=24, lanes=2):
    ir = _chain_ir(clusters_count)
    value = _platform_value("overlap", ["a", "b"], [_link("ab", "a", "b", lanes=lanes, latency=1)])
    for fpga in value["fpgas"]:
        fpga["capacity"] = {"lut": 1000, "ff": 1000}
    platform = Platform.from_dict(value)
    constraints = normalize_partition_constraints({
        "schema": "emuflow.partition-constraints/v1", "min_used_fpgas": 2,
        "balance_tolerance": 0.25,
    }, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode="static-exact-combinational")
    initial = build_partition_assignment(ir, platform, clusters, constraints, {
        cluster["id"]: ("a" if i % 2 == 0 else "b")
        for i, cluster in enumerate(clusters["clusters"])
    }, provider="fixture", seed=1)
    routes = normalize_route_constraints({
        "schema": "emuflow.system-route-constraints/v1", "frame_slots": 64,
        "tdm_ratio_quantum": 1, "max_route_hops": 1,
    }, platform)
    paths = []
    for i in range(members):
        # Full-chain members overlap every net; shorter members exercise
        # candidate-dependent unions and membership reset between moves.
        start = 0 if i % 3 == 0 else i % (clusters_count - 1)
        end = clusters_count - 1 if i % 3 == 0 else min(clusters_count - 1, start + 1 + i % 7)
        paths.append({
            "id": f"p{i:08d}", "startpoint": _endpoint(f"u{start:05d}", "O"),
            "endpoint": _endpoint(f"u{end:05d}", "I"), "clock_domain": "clk",
            "clock_period_ns": 10.0, "slack_ns": 3.0, "fixed_delay_ns": 7.0,
            "path_nets": [f"n{j:05d}" for j in range(start, end)], "normalized_slack": 1.0,
        })
    timing = {
        "schema": "emuflow.sta-path-database/v1", "design": "pressure_chain",
        "source": {"provider": "fixture", "input": "fixture"},
        "normalization": {"positive_slack_scale_ns": 3.0, "negative_slack_scale_ns": 1.0,
                          "max_clock_period_ns": 10.0}, "paths": paths,
    }
    model = build_partition_pressure_model(ir, platform, clusters, constraints, timing, routes)
    return ir, platform, clusters, constraints, timing, routes, model, initial


def test_overlapping_net_and_ratio_memberships_replay_every_candidate():
    ir, platform, clusters, constraints, timing, routes, model, initial = overlap_case()
    final, trace = run_partition_pressure_native(
        ir, platform, clusters, constraints, routes, model, initial,
        algorithm_version=14, executable=str(emuflow_native()), max_moves=12,
    )
    assert len(trace["moves"]) > 1
    checked = validate_partition_pressure_native_bundle(
        ir, platform, clusters, constraints, timing, routes, model, initial, final, trace,
    )
    assert checked["status"] == "pass"
