"""Independent exact validation uses owners, not the producer's decisions."""

from contextlib import ExitStack, closing
import copy
import os
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from emuflow.common.errors import ValidationError
from emuflow.scheduling.tdm import build_native_exact_schedule_request
from tests.tdm_reference import validate_tdm_schedule
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from tests.schedule_store_fixture import write_tdm_schedule_storage
from tests.native_schedule_case import _case
from tests.schedule_reference import build_tdm_schedule
from tests.schedule_store_fixture import write_tdm_schedule_storage as write_fixture
from tests.test_native_schedule_generation import install_runtime


@pytest.mark.parametrize(
    "affine,repeated",
    [
        (False, False),
        (True, False),
        (True, True),
        ("banks", False),
        ("tree", False),
        ("wide-ratio", True),
    ],
)
def test_native_independent_validation_matches_host_oracle_without_host_records(
    tmp_path, monkeypatch, affine, repeated
):
    case, routes = _case(tmp_path, affine, repeated)
    reference = build_tdm_schedule(routes, case.platform)
    path = tmp_path / "schedule.json"
    write_fixture(path, reference)
    schedule = read_tdm_schedule(path)
    host_routes = {**routes, "routes": list(routes["routes"])}
    expected = validate_tdm_schedule(
        host_routes, case.platform, read_tdm_schedule(path, materialize=True)
    )
    install_runtime(tmp_path, monkeypatch)
    # Input validation may use bounded SQL metadata, never a Python route/hop
    # cursor, logical-seal replay, dependency graph, or temporary hop index.
    with ExitStack() as patches:
        for name in (
            "__iter__",
            "ordered",
            "iter_native_exact_records",
            "iter_routes_by_net_rank_order",
            "iter_demands_by_net_rank_order",
        ):
            patches.enter_context(
                patch.object(
                    type(routes["routes"]),
                    name,
                    side_effect=AssertionError("Python route population"),
                )
            )
        for name in (
            "validate_logical_seals",
            "iter_canonical_section_records",
        ):
            patches.enter_context(
                patch.object(
                    type(schedule),
                    name,
                    side_effect=AssertionError("Python schedule population"),
                )
            )
        patches.enter_context(
            patch(
                'tests.tdm_reference._exact_dependency_order',
                side_effect=AssertionError("Python dependency graph"),
            )
        )
        actual = validate_tdm_schedule(routes, case.platform, schedule)
    assert (
        actual.pop("implementation_id")
        == "native-independent-exact-schedule-validation-v1"
    )
    assert actual == expected


def test_native_producer_and_independent_validator_share_no_solver_decisions(
    tmp_path, monkeypatch
):
    case, routes = _case(tmp_path, "tree", False)
    install_runtime(tmp_path, monkeypatch)
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(
        path, build_native_exact_schedule_request(routes, case.platform)
    )
    assert (
        validate_tdm_schedule(routes, case.platform, read_tdm_schedule(path))["status"]
        == "pass"
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "net",
        "arc",
        "capacity",
        "slot",
        "lane",
        "ready",
        "arrival",
        "hop",
        "ratio",
        "entry_missing",
        "foreign_demand",
        "duplicate_net",
        "foreign_completion",
        "entry_extra",
        "collision",
        "completion",
        "source_ready",
        "sink_arrival",
        "sink_missing",
        "route_source",
        "route_sink",
        "route_edge",
        "domain",
        "certificate",
        "metrics",
    ],
)
def test_fully_resealed_wrong_schedule_is_rejected_independently(
    tmp_path, monkeypatch, corruption
):
    case, routes = _case(
        tmp_path, True if corruption == "ratio" else False, corruption == "collision"
    )
    schedule = copy.deepcopy(build_tdm_schedule(routes, case.platform))
    schedule["routes"] = copy.deepcopy(list(schedule["routes"]))
    entry = schedule["entries"][0]
    if corruption == "net":
        entry["net"] = "other-net"
    elif corruption == "arc":
        entry["link"] = "other-link"
    elif corruption == "capacity":
        entry["capacity_key"] = "other-capacity"
    elif corruption == "slot":
        entry["slot"] = schedule["route_constraints"]["frame_slots"]
    elif corruption == "lane":
        entry["lane"] = 2**31
    elif corruption == "ready":
        entry["ready_slot"] += 1
    elif corruption == "arrival":
        entry["arrival_slot"] += 1
    elif corruption == "hop":
        entry["hop"] += 1
    elif corruption == "ratio":
        entry["tdm_ratio"] += 8
    elif corruption == "foreign_demand":
        entry['demand'] = 'foreign-demand'
    elif corruption == "duplicate_net":
        schedule['routes'][1]['net'] = schedule['routes'][0]['net']
    elif corruption == "foreign_completion":
        schedule['demand_completions'][0]['demand'] = 'foreign-demand'
    elif corruption == "entry_missing":
        schedule["entries"].pop()
    elif corruption == "entry_extra":
        extra = {**entry, "id": "extra", "from": entry["to"], "to": entry["from"]}
        schedule["entries"].append(extra)
    elif corruption == "collision":
        schedule["entries"][1].update(slot=entry["slot"], lane=entry["lane"])
    elif corruption == "completion":
        schedule["demand_completions"][0]["completion_slot"] += 1
    elif corruption == "source_ready":
        schedule["demand_completions"][0]["source_ready_slot"] += 1
    elif corruption == "sink_arrival":
        arrivals = schedule["demand_completions"][0]["sink_arrival_slots"]
        arrivals[next(iter(arrivals))] += 1
    elif corruption == "sink_missing":
        schedule["demand_completions"][0]["sink_arrival_slots"] = {}
    elif corruption == "route_source":
        schedule["routes"][0]["source"] = "other-fpga"
    elif corruption == "route_sink":
        schedule["routes"][0]["sinks"] = ["other-fpga"]
    elif corruption == "route_edge":
        schedule["routes"][0]["tree_edges"][0]["link"] = "other-link"
    elif corruption == "domain":
        schedule["domain_schedules"][0]["scheduled_bit_hops"] += 1
    elif corruption == "certificate":
        schedule["schedule_dependency_certificate"]["dependency_edges"] += 1
    elif corruption == "metrics":
        schedule["metrics"]["completion_slot"] += 1
    path = tmp_path / "schedule.json"
    # Recompute file, logical and section hashes after the corruption: the
    # rejection must come from semantics, not a stale producer hash.
    write_fixture(path, schedule)
    install_runtime(tmp_path, monkeypatch)
    with pytest.raises(
        ValidationError, match="native independent schedule validation failed"
    ):
        validate_tdm_schedule(routes, case.platform, read_tdm_schedule(path))


@pytest.mark.parametrize(
    "corruption", ["compact", "pretty", "section", "row", "orphan"]
)
def test_native_logical_seals_and_child_coverage_reject_corruption(
    tmp_path, monkeypatch, corruption
):
    case, routes = _case(tmp_path, "tree", False)
    path = tmp_path / "schedule.json"
    manifest = write_fixture(path, build_tdm_schedule(routes, case.platform))
    install_runtime(tmp_path, monkeypatch)
    schedule = read_tdm_schedule(path)
    if corruption in {"row", "orphan"}:
        # Reopen a bound view only after mutation so the native body itself
        # checks coverage. Bypass the constructor's redundant structure gate.
        with closing(sqlite3.connect(schedule.store_path)) as db, db:
            if corruption == "row":
                db.execute(
                    "UPDATE schedule_entries SET entry_id='different' WHERE seq=0"
                )
            else:
                db.execute(
                    "INSERT INTO schedule_route_edges VALUES(999,0,'link_0_1','fpga0','fpga1')"
                )
        from emuflow.storage.store_publication import bind_store_source

        schedule._source_bindings = (
            bind_store_source(schedule.store_path, "tdm-schedule-store-v4"),
        )
    elif corruption == "section":
        schedule.manifest["sections"]["entries"]["sha256"] = "0" * 64
    else:
        key = "logical_sha256" if corruption == "compact" else "logical_pretty_sha256"
        schedule.manifest[key] = "0" * 64
    with pytest.raises(ValidationError):
        validate_tdm_schedule(routes, case.platform, schedule)


def test_route_cursor_keeps_original_source_binding_after_document_projection(tmp_path):
    _case_object, routes = _case(tmp_path, False, False)
    raw = routes["routes"]
    expected = routes.bound_store_sources()
    assert raw.bound_store_sources() == expected
    assert raw.ordered("demand").bound_store_sources() == expected
    status = raw.store_path.stat()
    os.utime(raw.store_path, ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000))
    for cursor in (raw, raw.ordered("net")):
        with pytest.raises(ValidationError, match="generation changed"):
            cursor.bound_store_sources()


def test_native_failure_never_retries_host_validator(tmp_path, monkeypatch):
    case, routes = _case(tmp_path, False, False)
    path = tmp_path / "schedule.json"
    write_fixture(path, build_tdm_schedule(routes, case.platform))
    install_runtime(tmp_path, monkeypatch)
    from emuflow.common.tool_runtime import NativeToolProcess

    original = NativeToolProcess.finish

    def fail(process):
        result = original(process)
        return subprocess.CompletedProcess(result.args, 2, "injected native rejection")

    with (
        patch.object(NativeToolProcess, "finish", fail),
        patch(
            'tests.tdm_reference._exact_dependency_order',
            side_effect=AssertionError("host fallback"),
        ),
    ):
        with pytest.raises(ValidationError, match="injected native rejection"):
            validate_tdm_schedule(routes, case.platform, read_tdm_schedule(path))


def test_empty_typed_schedule_is_validated_without_a_host_special_case(
    tmp_path, monkeypatch
):
    from emuflow.common.io import read_json
    from emuflow.routing.system_route_storage import read_system_routes
    from tests.native_route_seal_fixture import write_route_store_fixture

    case, _routes = _case(tmp_path, False, False)
    empty = read_json(tmp_path / "routes.json")
    empty["routes"] = []
    empty["demands"] = []
    selector = tmp_path / "empty.json"
    write_route_store_fixture(selector, empty)
    routes = read_system_routes(selector)
    routes["semantic_contract"] = _routes["semantic_contract"]
    install_runtime(tmp_path, monkeypatch)
    path = tmp_path / "schedule.json"
    write_tdm_schedule_storage(
        path, build_native_exact_schedule_request(routes, case.platform)
    )
    result = validate_tdm_schedule(routes, case.platform, read_tdm_schedule(path))
    assert (
        result["demands"] == result["routed_sinks"] == result["scheduled_bit_hops"] == 0
    )
    assert result["minimum_capture_slack_slots"] is None


@pytest.mark.parametrize(
    "corruption",
    ["crc", "version", "truncated", "trailing", "generation", "alias", "report_alias"],
)
def test_native_protocol_source_and_atomic_summary_contract(
    tmp_path, monkeypatch, corruption
):
    from emuflow.common.tool_runtime import NativeToolProcess

    case, routes = _case(tmp_path, False, False)
    path = tmp_path / "schedule.json"
    write_fixture(path, build_tdm_schedule(routes, case.platform))
    schedule = read_tdm_schedule(path)
    install_runtime(tmp_path, monkeypatch)
    captured = []
    original = NativeToolProcess.__init__

    def capture(process, executable, command, **kwargs):
        captured.append(
            (
                list(command),
                Path(command[4]).read_bytes(),
                Path(command[6]).read_bytes(),
            )
        )
        return original(process, executable, command, **kwargs)

    with patch.object(NativeToolProcess, "__init__", capture):
        assert (
            validate_tdm_schedule(routes, case.platform, schedule)["status"] == "pass"
        )
    command, board_bytes, request_bytes = captured[0]
    board, request, output = (
        tmp_path / "board.bin",
        tmp_path / "request.bin",
        tmp_path / "summary.json",
    )
    board.write_bytes(board_bytes)
    request.write_bytes(request_bytes)
    output.write_bytes(b"previous summary")
    command[4], command[6], command[7] = str(board), str(request), str(output)
    if corruption == "generation":
        status = schedule.store_path.stat()
        os.utime(
            schedule.store_path,
            ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000),
        )
    elif corruption in {"alias", "report_alias"}:
        snapshots = {Path(value): Path(value).read_bytes() for value in command[3:7]}
        for source in snapshots:
            trial = list(command)
            if corruption == "alias":
                trial[-1] = str(source)
            else:
                trial[1:1] = ["--report", str(source)]
            result = subprocess.run(trial, capture_output=True, text=True)
            assert result.returncode != 0
            assert source.read_bytes() == snapshots[source]
        assert output.read_bytes() == b"previous summary"
        return
    else:
        payload = bytearray(request.read_bytes())
        if corruption == "crc":
            payload[-1] ^= 1
        elif corruption == "version":
            payload[8:12] = b"\xff" * 4
        elif corruption == "truncated":
            payload = payload[:-1]
        else:
            payload += b"x"
        request.write_bytes(payload)
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert output.read_bytes() == b"previous summary"
