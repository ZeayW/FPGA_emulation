"""Full-record projection and arithmetic differentials of native Phase5 timing."""

from contextlib import ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from emuflow.cross_layer_timing import (
    build_cross_layer_timing_contract,
    validate_cross_layer_timing_contract,
)
from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.phase5_timing_native import project_native_schedule_timing
from emuflow.system_route_storage import read_system_routes, write_system_route_storage
from emuflow.tdm import reconstruct_tdm_schedule_timing
from emuflow.tdm_schedule_storage import read_tdm_schedule
from tests.native_schedule_case import _case
from tests.schedule_reference import build_tdm_schedule
from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.test_native_schedule_generation import install_runtime


def _timed_case(tmp_path, monkeypatch, kind=False, count=9):
    case, original = _case(tmp_path, kind, kind == "wide-ratio")
    value = read_json(tmp_path / "routes.json")
    semantic = value.pop("semantic_contract")
    root = next(route for route in value["routes"] if not route["predecessor_cut_nets"])
    sink = sorted(root["sinks"])[0]
    paths = []
    for i in reversed(range(count)):
        identity = f"路径-{i:04d}-😀"
        paths.append(
            {
                "path": identity,
                "clock_domain": "时钟",
                "clock_period_ns": 5.0 + (i % 4),
                "fixed_delay_ns": [0.0, 1e-5, 0.1, 17.0, 1e4][i % 5],
                "required_time_ns": 6.0 + (i % 11),
                "cut_nets": [root["net"]],
                "cut_transitions": [
                    {"net": root["net"], "from": root["source"], "to": sink}
                ],
                "compressed_path_ids": [identity, identity + ':"member\\雪'],
            }
        )
    value["timing"] = {
        "normalization": {
            "positive_slack_scale_ns": 20.0,
            "negative_slack_scale_ns": 10.0,
            "max_clock_period_ns": 20.0,
        },
        "paths": paths,
    }
    return _publish_case(tmp_path, monkeypatch, case, value, semantic)


def _publish_case(tmp_path, monkeypatch, case, value, semantic):
    selector = tmp_path / "current-routes.json"
    write_system_route_storage(selector, value)
    routes = read_system_routes(selector)
    hydrated = {**routes, "semantic_contract": semantic}
    reference_schedule = build_tdm_schedule(hydrated, case.platform)
    schedule_path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(schedule_path, reference_schedule)
    schedule = read_tdm_schedule(schedule_path)
    install_runtime(tmp_path, monkeypatch)
    return case, value, semantic, routes, schedule, reference_schedule


@pytest.mark.parametrize(
    "kind,count",
    [(False, 9), (True, 9), ("tree", 9), ("banks", 9), (False, 137), ("wide-ratio", 9)],
)
def test_native_combined_summary_and_full_projection_match_independent_reference(
    tmp_path, monkeypatch, kind, count
):
    case, value, semantic, routes, schedule, reference_schedule = _timed_case(
        tmp_path, monkeypatch, kind, count
    )
    expected_timing = reconstruct_tdm_schedule_timing(
        value, case.platform, reference_schedule
    )
    expected_contract = build_cross_layer_timing_contract(
        value, reference_schedule, semantic
    )
    projection_sha = hashlib.sha256(
        json.dumps(
            expected_contract["path_bindings"],
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with ExitStack() as patches:
        for name in [
            "__iter__",
            "iter_cross_layer_route_metadata",
            "iter_cross_layer_hop_keys",
        ]:
            patches.enter_context(
                patch.object(
                    type(routes["routes"]),
                    name,
                    side_effect=AssertionError("Python R population"),
                )
            )
        patches.enter_context(
            patch.object(
                type(routes["timing"]["paths"]),
                "__iter__",
                side_effect=AssertionError("Python P population"),
            )
        )
        patches.enter_context(
            patch(
                "emuflow.tdm._iter_tdm_schedule_timing_paths_from_routes",
                side_effect=AssertionError("Python hop projection"),
            )
        )
        patches.enter_context(
            patch(
                "emuflow.cross_layer_timing._build_contract",
                side_effect=AssertionError("Python binding tree"),
            )
        )
        actual = project_native_schedule_timing(
            routes, schedule, case.platform, bind=True, analyze=True
        )
        assert (
            reconstruct_tdm_schedule_timing(routes, case.platform, schedule)
            == expected_timing
        )
        assert (
            build_cross_layer_timing_contract(routes, schedule, semantic)
            == actual["contract"]
        )
        assert validate_cross_layer_timing_contract(
            routes, actual["contract"], schedule, semantic
        ) == {"status": "pass", **expected_contract["metrics"]}
    assert actual["timing_validation"] == expected_timing
    contract = actual["contract"]
    assert contract["schema"] == "emuflow.cross-layer-timing/v3"
    assert "path_bindings" not in contract
    assert contract["path_binding"] == {
        "mode": "canonical-route-store-path-projection-v1",
        "source_user_version": 3,
        "records": count,
        "sha256": projection_sha,
    }
    for key in [
        "design",
        "platform",
        "transport_semantics",
        "route_binding",
        "schedule_binding",
        "semantic_binding",
        "logic_segment_binding",
        "logic_segment_bindings",
        "hop_binding",
        "metrics",
    ]:
        assert contract[key] == expected_contract[key], key
    import jsonschema

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/cross-layer-timing-v3.schema.json"
        ).read_text()
    )
    jsonschema.validate(contract, schema)
    assert len(json.dumps(contract).encode()) < 2500


def test_native_analysis_preserves_implicit_multicast_worst_sink_semantics(
    tmp_path, monkeypatch
):
    case, value, semantic, _routes, _schedule, _reference = _timed_case(
        tmp_path, monkeypatch, "tree"
    )
    for path in value["timing"]["paths"]:
        path.pop("cut_transitions")
    fresh = tmp_path / "implicit"
    fresh.mkdir()
    case, value, semantic, routes, schedule, reference = _publish_case(
        fresh, monkeypatch, case, value, semantic
    )
    expected = reconstruct_tdm_schedule_timing(value, case.platform, reference)
    assert reconstruct_tdm_schedule_timing(routes, case.platform, schedule) == expected
    with pytest.raises(ValidationError, match="multicast"):
        project_native_schedule_timing(routes, schedule, bind=True)


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_net",
        "transition_from",
        "transition_sink",
        "transition_count",
        "members_empty",
        "members_duplicate",
        "members_null",
        "required",
        "fixed",
        "clock",
        "cut_type",
    ],
)
def test_freshly_sealed_malformed_path_contract_is_rejected(
    tmp_path, monkeypatch, corruption
):
    case, value, semantic, _routes, _schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    path = value["timing"]["paths"][0]
    if corruption == "unknown_net":
        path["cut_nets"] = ["unknown"]
    elif corruption == "transition_from":
        path["cut_transitions"][0]["from"] = "unknown"
    elif corruption == "transition_sink":
        path["cut_transitions"][0]["to"] = "unknown"
    elif corruption == "transition_count":
        path["cut_transitions"] = []
    elif corruption == "members_empty":
        path["compressed_path_ids"] = []
    elif corruption == "members_duplicate":
        path["compressed_path_ids"] *= 2
    elif corruption == "members_null":
        path["compressed_path_ids"] = None
    elif corruption == "required":
        path["required_time_ns"] = False
    elif corruption == "fixed":
        path["fixed_delay_ns"] = -1
    elif corruption == "clock":
        path["clock_domain"] = ""
    else:
        path["cut_nets"] = [False]
    # The schedule is unchanged; only the RouteStore path population is
    # regenerated/sealed to avoid relying on a stale byte/logical hash.
    fresh = tmp_path / "bad-routes.json"
    write_system_route_storage(fresh, value)
    routes = read_system_routes(fresh)
    with pytest.raises(ValidationError, match="native Phase5 timing projection failed"):
        project_native_schedule_timing(
            routes, _schedule, case.platform, bind=True, analyze=True
        )


def test_v3_binding_rejects_forged_projection_and_requires_its_typed_owner(
    tmp_path, monkeypatch
):
    case, value, semantic, routes, schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    contract = build_cross_layer_timing_contract(routes, schedule, semantic)
    corrupt = copy.deepcopy(contract)
    corrupt["path_binding"]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="does not match"):
        validate_cross_layer_timing_contract(routes, corrupt, schedule, semantic)
    with pytest.raises(ValidationError, match="does not match"):
        validate_cross_layer_timing_contract(value, contract, _reference, semantic)


def test_native_route_only_binding_does_not_invent_schedule_evidence(
    tmp_path, monkeypatch
):
    _case_value, value, semantic, routes, _schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    expected = build_cross_layer_timing_contract(value, None, semantic)
    actual = build_cross_layer_timing_contract(routes, None, semantic)
    assert actual["metrics"] == expected["metrics"]
    assert "schedule_binding" not in actual and "hop_binding" not in actual


@pytest.mark.parametrize("kind", [False, True, "banks"])
def test_native_timing_preserves_absolute_multi_cut_event_composition(
    tmp_path, monkeypatch, kind
):
    case, value, semantic, _routes, _schedule, _reference = _timed_case(
        tmp_path, monkeypatch, kind
    )
    root = next(route for route in value["routes"] if not route["predecessor_cut_nets"])
    child = next(
        route
        for route in value["routes"]
        if root["net"] in route["predecessor_cut_nets"]
    )
    for path in value["timing"]["paths"]:
        path["cut_nets"] = [root["net"], child["net"]]
        path["cut_transitions"] = [
            {"net": root["net"], "from": root["source"], "to": child["source"]},
            {"net": child["net"], "from": child["source"], "to": child["sinks"][0]},
        ]
    fresh = tmp_path / "chain"
    fresh.mkdir()
    case, value, semantic, routes, schedule, reference = _publish_case(
        fresh, monkeypatch, case, value, semantic
    )
    expected = reconstruct_tdm_schedule_timing(value, case.platform, reference)
    assert (
        project_native_schedule_timing(routes, schedule, case.platform, analyze=True)[
            "timing_validation"
        ]
        == expected
    )


def test_large_member_group_is_hashed_without_expanding_a_second_python_population(
    tmp_path, monkeypatch
):
    case, value, semantic, _routes, _schedule, _reference = _timed_case(
        tmp_path, monkeypatch, count=1
    )
    value["timing"]["paths"][0]["compressed_path_ids"] = [
        f"member-{i}-雪😀" for i in range(10000)
    ]
    fresh = tmp_path / "many-members"
    fresh.mkdir()
    case, value, semantic, routes, schedule, reference = _publish_case(
        fresh, monkeypatch, case, value, semantic
    )
    expected = build_cross_layer_timing_contract(value, reference, semantic)
    sha = hashlib.sha256(
        json.dumps(
            expected["path_bindings"],
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with patch.object(
        type(routes["timing"]["paths"]),
        "__iter__",
        side_effect=AssertionError("Python path/member population"),
    ):
        actual = build_cross_layer_timing_contract(routes, schedule, semantic)
    assert actual["path_binding"]["sha256"] == sha
    assert len(json.dumps(actual)) < 2500


def test_timing_statistics_preserve_signed_zero_after_external_numeric_sort(
    tmp_path, monkeypatch
):
    case, value, semantic, _routes, _schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    value["timing"]["normalization"]["negative_slack_scale_ns"] = 1e308
    for path in value["timing"]["paths"]:
        path["fixed_delay_ns"] = 1e4
    fresh = tmp_path / "signed-zero"
    fresh.mkdir()
    case, value, semantic, routes, schedule, reference = _publish_case(
        fresh, monkeypatch, case, value, semantic
    )
    expected = reconstruct_tdm_schedule_timing(value, case.platform, reference)
    actual = reconstruct_tdm_schedule_timing(routes, case.platform, schedule)
    assert json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
    assert str(actual["worst_normalized_slack"]) == "-0.0"


@pytest.mark.parametrize("corruption", ["crc", "trailing", "generation", "alias"])
def test_native_timing_request_ownership_and_atomic_summary(
    tmp_path, monkeypatch, corruption
):
    from emuflow.tool_runtime import NativeToolProcess

    case, _value, _semantic, routes, schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    captured = []
    original = NativeToolProcess.__init__

    def capture(process, executable, command, **kwargs):
        captured.append(
            (
                list(command),
                Path(command[-2]).read_bytes(),
                Path(command[-3]).read_bytes(),
            )
        )
        return original(process, executable, command, **kwargs)

    with patch.object(NativeToolProcess, "__init__", capture):
        project_native_schedule_timing(routes, schedule, case.platform, analyze=True)
    command, request_bytes, board_bytes = captured[0]
    request, board, output = (
        tmp_path / "request.bin",
        tmp_path / "board.bin",
        tmp_path / "summary.json",
    )
    request.write_bytes(request_bytes)
    board.write_bytes(board_bytes)
    output.write_bytes(b"previous summary")
    command[-3:] = [str(board), str(request), str(output)]
    if corruption == "generation":
        status = schedule.store_path.stat()
        os.utime(
            schedule.store_path,
            ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000),
        )
    elif corruption == "alias":
        for source in command[3:-1]:
            p = Path(source)
            before = p.read_bytes()
            trial = list(command)
            trial[-1] = source
            result = subprocess.run(trial, capture_output=True, text=True)
            assert result.returncode != 0
            assert p.read_bytes() == before
        assert output.read_bytes() == b"previous summary"
        return
    else:
        payload = bytearray(request_bytes)
        if corruption == "crc":
            payload[-1] ^= 1
        else:
            payload += b"x"
        request.write_bytes(payload)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_bytes() == b"previous summary"


def test_phase5_rejects_missing_native_timing_schema_before_reading_circuit_inputs(
    tmp_path,
):
    from emuflow.errors import EmuFlowError
    from emuflow.phase5 import run_phase5

    with (
        patch(
            "emuflow.native_tools.require_native_schema",
            side_effect=EmuFlowError("timing schema unavailable"),
        ),
        patch(
            "emuflow.phase5.read_system_routes",
            side_effect=AssertionError("circuit opened before preflight"),
        ),
    ):
        with pytest.raises(EmuFlowError, match="timing schema unavailable"):
            run_phase5(
                tmp_path / "missing-routes",
                tmp_path / "missing-board",
                tmp_path / "output",
            )
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "corruption", ["statistics", "projection", "oversized", "json"]
)
def test_native_timing_adapter_rejects_malformed_success_payload(
    tmp_path, monkeypatch, corruption
):
    from emuflow.tool_runtime import NativeToolProcess

    case, _value, _semantic, routes, schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    original = NativeToolProcess.finish

    def malformed(process):
        result = original(process)
        output = Path(result.args[-1])
        value = json.loads(output.read_text())
        if corruption == "statistics":
            value["timing_validation"] = {}
        elif corruption == "projection":
            value["contract"]["path_binding"]["records"] = True
        elif corruption == "oversized":
            output.write_bytes(b" " * 8193)
            return result
        else:
            output.write_text("{")
            return result
        output.write_text(json.dumps(value))
        return result

    with patch.object(NativeToolProcess, "finish", malformed):
        with pytest.raises(ValidationError):
            project_native_schedule_timing(
                routes, schedule, case.platform, analyze=True
            )


@pytest.mark.parametrize("corruption", ["negative_wait", "zero_ratio"])
def test_native_timing_rejects_freshly_resealed_invalid_schedule_events(
    tmp_path, monkeypatch, corruption
):
    case, value, semantic, routes, _schedule, reference = _timed_case(
        tmp_path, monkeypatch, True
    )
    reference = copy.deepcopy(reference)
    if corruption == "negative_wait":
        reference["entries"][0]["ready_slot"] = reference["entries"][0]["slot"] + 1
    else:
        reference["entries"][0]["tdm_ratio"] = 0
    selector = tmp_path / "bad-schedule.json"
    write_tdm_schedule_storage(selector, reference)
    with pytest.raises(ValidationError, match="native Phase5 timing projection failed"):
        project_native_schedule_timing(
            routes, read_tdm_schedule(selector), case.platform, analyze=True
        )


def test_native_timing_rejects_forged_route_population_before_rank_allocation(
    tmp_path, monkeypatch
):
    from contextlib import closing
    import sqlite3
    import struct
    import zlib
    from emuflow.native_tools import native_file_generation
    from emuflow.tool_runtime import NativeToolProcess

    case, _value, _semantic, routes, schedule, _reference = _timed_case(
        tmp_path, monkeypatch
    )
    captured = []
    original = NativeToolProcess.__init__

    def capture(process, executable, command, **kwargs):
        captured.append((list(command), Path(command[-2]).read_bytes()))
        return original(process, executable, command, **kwargs)

    with patch.object(NativeToolProcess, "__init__", capture):
        project_native_schedule_timing(routes, schedule)
    command, payload = captured[0]
    route_path = Path(command[3])
    with closing(sqlite3.connect(route_path)) as db, db:
        db.execute(
            "UPDATE metadata SET value=json_set(value,'$.routes.records',?) WHERE key='sections'",
            (2**32,),
        )
    request = tmp_path / "forged-count.bin"
    payload = bytearray(payload)
    struct.pack_into("<3Q4q", payload, 16, *native_file_generation(route_path))
    struct.pack_into("<I", payload, len(payload) - 4, zlib.crc32(payload[:-4]))
    request.write_bytes(payload)
    output = tmp_path / "previous-summary.json"
    output.write_bytes(b"previous")
    command[-2:] = [str(request), str(output)]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert "source/rank bounds" in result.stderr
    assert output.read_bytes() == b"previous"
