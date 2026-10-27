from tests.native_route_fixture import route_system_native
"""Shared HP-bank routing and ratio-delay prediction contracts."""

import copy
import json

import pytest

from emuflow.bank_allocation import allocate_shared_banks
from emuflow.errors import EmuFlowError, ValidationError
from emuflow.ir import EmuIR
from emuflow.partition import (
    build_clusters,
    build_partition_assignment,
    normalize_partition_constraints,
)
from emuflow.partition_pressure import (
    _predicted_route_delay,
    build_partition_pressure_model,
    evaluate_partition_pressure,
    run_partition_pressure_native,
    validate_partition_pressure_native_bundle,
)
from emuflow.platform import Platform
from emuflow.routing import (
    build_directed_graph,
    demands_from_assignment,
    normalize_route_constraints,
    route_hop_delay_ns,
)
from emuflow.timing_routing import (GLOBAL_CANDIDATE_PROVIDER, compress_sta_paths, normalize_sta_paths, validate_native_system_routes)
from tests.native_build import emuflow_native, tlr_router
from tests import test_partition_pressure as pressure_fixture
from tests.test_phase4 import _assignment, _link, _platform_value


def bank_link(
    link_id, left, right, *, banks=18, wires=20, mhz=250.0,
    min_ratio=8, max_ratio=256, ratio_quantum=8,
):
    value = _link(link_id, left, right, lanes=banks * wires)
    value.update(
        capacity_sharing="shared_bidirectional",
        bank_count=banks,
        wires_per_bank=wires,
        fabric_clock_mhz=mhz,
        tdm_delay_model={"model": "linear-ratio-v1", "intercept_ns": 30.0,
                         "ratio_coefficient_ns": 0.5,
                         "min_ratio": min_ratio, "max_ratio": max_ratio,
                         "ratio_quantum": ratio_quantum},
    )
    return value


def test_whole_bank_granularity_rejects_pooled_false_feasibility():
    # 359 + 1 fits 360 pooled wires, but needs 18 + 1 directional banks.
    assert allocate_shared_banks(359, 1, 18, 20, max_ratio=1) is None
    split = allocate_shared_banks(340, 20, 18, 20, max_ratio=1)
    assert (split["forward_banks"], split["reverse_banks"]) == (17, 1)
    assert split["forward_lanes"] + split["reverse_lanes"] == 360
    pressure = allocate_shared_banks(359, 1, 18, 20, ratio_quantum=1)
    assert max(pressure["forward_ratio"], pressure["reverse_ratio"]) == 2


def test_bank_prediction_is_not_capped_at_frame_budget():
    prediction = allocate_shared_banks(10000, 1, 18, 20, ratio_quantum=8)
    assert prediction["forward_ratio"] > 16
    assert allocate_shared_banks(10000, 1, 18, 20, max_ratio=16) is None


def test_quantized_ratio_ties_balance_remaining_wire_pressure():
    split = allocate_shared_banks(181, 179, 18, 20)
    assert (split["forward_banks"], split["reverse_banks"]) == (9, 9)


def test_per_hop_ratio_delay_does_not_use_fabric_slot_period():
    platform = Platform.from_dict(_platform_value(
        "ratio_line", ["a", "b", "c"],
        [bank_link("ab", "a", "b", mhz=100),
         bank_link("bc", "b", "c", mhz=250)],
    ))
    constraints = normalize_route_constraints(None, platform)
    _, arcs, capacities = build_directed_graph(platform, constraints)
    domain_by_arc = {key: arc["capacity_key"] for key, arc in arcs.items()}
    ratios = {key: 1 for key in capacities}
    ratios["ab:a->b"] = 8
    ratios["bc:b->c"] = 16
    route = [arcs[("ab", "a", "b")], arcs[("bc", "b", "c")]]
    assert route_hop_delay_ns(platform, "ab", "a", "b", constraints, 8) == 34.0
    with pytest.raises(ValidationError, match="supported ratio"):
        route_hop_delay_ns(platform, "ab", "a", "b", constraints, 1)
    assert _predicted_route_delay(
        platform, constraints, domain_by_arc, ratios, route,
        include_tdm_wait=True,
    ) == 72.0
    assert _predicted_route_delay(
        platform, constraints, domain_by_arc, ratios, route,
        include_tdm_wait=False,
    ) == 61.0


def test_partition_python_and_native_share_bank_delay_model():
    fixture = pressure_fixture.PartitionPressureTest()
    fixture.setUp()
    value = fixture.platform.to_dict()
    value["links"] = [bank_link("ab", "a", "b")]
    platform = Platform.from_dict(value)
    fixture.route_constraints = normalize_route_constraints(
        {"schema": "emuflow.system-route-constraints/v1", "frame_slots": 32},
        platform,
    )
    model = build_partition_pressure_model(
        fixture.ir, platform, fixture.clusters, fixture.constraints,
        fixture.timing, fixture.route_constraints,
    )
    fixture.initial = build_partition_assignment(
        fixture.ir, platform, fixture.clusters, fixture.constraints,
        fixture.initial["cluster_assignment"], provider="fixture", seed=7,
    )
    initial = evaluate_partition_pressure(
        fixture.ir, platform, fixture.clusters, fixture.constraints,
        fixture.route_constraints, model, fixture.initial["cluster_assignment"],
    )
    assert all(row["transport_delay_ns"] == 34.0 for row in initial["paths"])
    final, trace = run_partition_pressure_native(
        fixture.ir, platform, fixture.clusters, fixture.constraints,
        fixture.route_constraints, model, fixture.initial,
        algorithm_version=14,
        executable=str(emuflow_native()),
    )
    check = validate_partition_pressure_native_bundle(fixture.ir, platform, fixture.clusters, fixture.constraints, fixture.timing, fixture.route_constraints, model, fixture.initial, final, trace)
    assert check["status"] == "pass"


def test_native_router_preserves_direction_bank_pressure(tmp_path):
    platform = Platform.from_dict(_platform_value(
        "bank_pair", ["b", "a"], [bank_link(
            "ab", "b", "a", banks=2, wires=1,
            min_ratio=1, max_ratio=8, ratio_quantum=1,
        )],
    ))
    # Endpoint order is reversed intentionally; deterministic allocation follows
    # canonical direction keys in Python and native code.
    assignment = _assignment(platform, [
        ("n0", "a", ["b"]), ("n1", "a", ["b"]),
        ("n2", "a", ["b"]), ("back", "b", ["a"]),
    ])
    timing = compress_sta_paths(normalize_sta_paths({
        "schema": "emuflow.sta-paths/v1", "design": "route_test",
        "paths": [{"id": net, "clock_domain": "clk", "clock_period_ns": 100.0,
                   "slack_ns": 100.0, "fixed_delay_ns": 0.0, "cut_nets": [net]}
                  for net in ("n0", "n1", "n2", "back")],
    }, demands_from_assignment(assignment, platform)))
    constraints = normalize_route_constraints({
        "schema": "emuflow.system-route-constraints/v1", "frame_slots": 8,
        "tdm_ratio_quantum": 1,
    }, platform)
    routes = route_system_native(
        assignment, platform, constraints, timing, executable=str(tlr_router()),
        provider=GLOBAL_CANDIDATE_PROVIDER,
    )
    assert validate_native_system_routes(assignment, platform, routes, timing)["status"] == "pass"
    assert routes["metrics"]["estimated_max_tdm_ratio"] == 3


def test_native_router_rejects_fractional_direction_bank_capacity():
    platform = Platform.from_dict(_platform_value(
        "indivisible_banks", ["a", "b"], [bank_link(
            "ab", "a", "b", banks=2, wires=1,
            min_ratio=1, max_ratio=2, ratio_quantum=1,
        )],
    ))
    assignment = _assignment(platform, [
        ("n0", "a", ["b"]), ("n1", "a", ["b"]),
        ("n2", "a", ["b"]), ("back", "b", ["a"]),
    ])
    constraints = normalize_route_constraints({
        "schema": "emuflow.system-route-constraints/v1",
        "frame_slots": 2, "tdm_ratio_quantum": 1,
    }, platform)
    with pytest.raises(EmuFlowError, match="infeasible"):
        route_system_native(assignment, platform, constraints, executable=str(tlr_router()))


def test_partition_move_recomputes_opposite_direction_pressure():
    # Removing the one forward net frees a bank for three reverse nets.  The
    # native incremental evaluator must update those other nets' path costs.
    original = pressure_fixture.PartitionPressureTest()
    original.setUp()
    value = copy.deepcopy(original.ir.value)
    value["instances"] = [{"id": f"u{i}", "type": "LUT1", "resources": {"lut": 1}}
                          for i in range(8)]
    value["nets"] = [{
        "id": f"n{i}", "cut_class": "register_output",
        "drivers": [pressure_fixture._endpoint(f"u{2*i}", "O")],
        "sinks": [pressure_fixture._endpoint(f"u{2*i+1}", "I")],
    } for i in range(4)]
    ir = EmuIR(value)
    platform = Platform.from_dict(_platform_value(
        "bank_move", ["a", "b"], [bank_link(
            "ab", "a", "b", banks=2, wires=1,
            min_ratio=1, max_ratio=8, ratio_quantum=1,
        )],
    ))
    constraints = normalize_partition_constraints({
        "schema": "emuflow.partition-constraints/v1", "min_used_fpgas": 2,
        "balance_tolerance": 1.0,
    }, ir, platform)
    clusters = build_clusters(ir, constraints, cut_mode="static-exact-combinational")
    by_instance = {c["instances"][0]: c["id"] for c in clusters["clusters"]}
    assignment = {by_instance[f"u{i}"]: ("a" if i == 0 or i > 1 and i % 2 else "b")
                  for i in range(8)}
    initial = build_partition_assignment(ir, platform, clusters, constraints,
                                        assignment, provider="fixture", seed=7)
    route_constraints = normalize_route_constraints({
        "schema": "emuflow.system-route-constraints/v1", "frame_slots": 8,
        "tdm_ratio_quantum": 1,
    }, platform)
    timing = copy.deepcopy(original.timing)
    timing["paths"] = [{
        "id": f"p{i}", "startpoint": pressure_fixture._endpoint(f"u{2*i}", "O"),
        "endpoint": pressure_fixture._endpoint(f"u{2*i+1}", "I"),
        "clock_domain": "clk", "clock_period_ns": 20.0,
        "slack_ns": 0.0 if i == 0 else 20.0, "fixed_delay_ns": 0.0,
        "path_nets": [f"n{i}"], "normalized_slack": 0.0 if i == 0 else 1.0,
    } for i in range(4)]
    model = build_partition_pressure_model(ir, platform, clusters, constraints,
                                          timing, route_constraints)
    evaluation = evaluate_partition_pressure(ir, platform, clusters, constraints,
                                             route_constraints, model, assignment)
    assert sorted(d["predicted_tdm_ratio"] for d in evaluation["capacity_domains"]) == [1, 3]
    final, trace = run_partition_pressure_native(
        ir, platform, clusters, constraints, route_constraints, model, initial,
        algorithm_version=14,
        executable=str(emuflow_native()),
    )
    assert validate_partition_pressure_native_bundle(ir, platform, clusters, constraints, timing, route_constraints, model, initial, final, trace)["status"] == "pass"


def test_v14_validator_merges_independently_constructed_batch_then_move():
    """A tiny semantic oracle, not a partition-search or NVDLA qualification."""
    import pytest
    from emuflow.errors import ValidationError
    from emuflow.partition_pressure import _canonical_digest, _ranked_key
    fixture = pressure_fixture.PartitionPressureTest()
    fixture.setUp()
    model = build_partition_pressure_model(
        fixture.ir, fixture.platform, fixture.clusters, fixture.constraints,
        fixture.timing, fixture.route_constraints,
    )
    _, trace = run_partition_pressure_native(
        fixture.ir, fixture.platform, fixture.clusters, fixture.constraints,
        fixture.route_constraints, model, fixture.initial,
        algorithm_version=14, max_moves=8, executable=str(emuflow_native()),
    )
    by_instance = {row["instances"][0]: row["id"] for row in fixture.clusters["clusters"]}
    original = dict(fixture.initial["cluster_assignment"])
    middle = {**original, by_instance["u0"]: "b"}
    last = {**middle, by_instance["u3"]: "a"}
    def facts(assignment):
        return evaluate_partition_pressure(
            fixture.ir, fixture.platform, fixture.clusters, fixture.constraints,
            fixture.route_constraints, model, assignment, path_transition_objective=True,
        )
    first, second, third = (facts(assignment) for assignment in (original, middle, last))
    assert _ranked_key(third) < _ranked_key(second) < _ranked_key(first)
    final = build_partition_assignment(
        fixture.ir, fixture.platform, fixture.clusters, fixture.constraints,
        last, provider=trace["provider"], seed=7,
    )
    trace.update(
        initial_metrics=first["metrics"], final_metrics=third["metrics"],
        final_cluster_assignment_sha256=_canonical_digest(last),
        move_count=1, batch_count=1, change_count=1,
        batches=[{"index": 0, "changes": [{"cluster": by_instance["u0"], "source": "a", "target": "b"}],
                  "before_objective_key": first["metrics"]["objective_key"],
                  "after_objective_key": second["metrics"]["objective_key"],
                  "ranked_objective_key": list(_ranked_key(second))}],
        moves=[{"index": 0, "kind": "move", "phase": 0, "sweep": 0,
                "cluster": by_instance["u3"], "source": "b", "target": "a",
                "partner": None, "partner_source": None, "partner_target": None,
                "before_objective_key": second["metrics"]["objective_key"],
                "after_objective_key": third["metrics"]["objective_key"],
                "ranked_objective_key": list(_ranked_key(third))}],
    )
    def check(evidence):
        return validate_partition_pressure_native_bundle(
            fixture.ir, fixture.platform, fixture.clusters, fixture.constraints,
            fixture.timing, fixture.route_constraints, model, fixture.initial, final, evidence,
        )
    assert check(trace)["status"] == "pass"
    for mutation in ("source", "rank", "before", "duplicate", "missing", "version", "index", "identity-type"):
        changed = copy.deepcopy(trace)
        if mutation == "source": changed["moves"][0]["source"] = "a"
        elif mutation == "rank": changed["moves"][0]["ranked_objective_key"] = changed["batches"][0]["ranked_objective_key"]
        elif mutation == "before": changed["moves"][0]["before_objective_key"][0] += 1
        elif mutation == "duplicate": changed["batches"][0]["changes"] *= 2
        elif mutation == "missing": changed["batches"] = []
        elif mutation == "version": del changed["configuration"]["algorithm_version"]
        elif mutation == "index": changed["moves"][0]["index"] = False
        else: changed["batches"][0]["changes"][0]["cluster"] = []
        with pytest.raises(ValidationError): check(changed)
