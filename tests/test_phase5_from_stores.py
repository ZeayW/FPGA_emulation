"""Native configuration ownership; no Python scheduling/configuration bridge."""
from tests.phase3_store_fixture import write_assignment_index_fixture
import json
import subprocess
from pathlib import Path

import pytest

from emuflow.scheduling.phase5_native import run_native_phase5
from emuflow.scheduling.tdm_schedule_storage import read_tdm_schedule
from emuflow.scheduling.phase5 import validate_phase5_generation
from tests.native_build import emuflow_native
from tests.test_phase5_native_stage import current_case
from tests.test_native_schedule_generation import install_runtime


def native_inputs(tmp_path, affine=False, repeated=False):
    case, routes, producer = current_case(tmp_path, affine, repeated)
    assignment = tmp_path / "assignment.sqlite"
    write_assignment_index_fixture(assignment, case.ir, case._phase5_assignment)
    board = tmp_path / "platform.normalized.json"
    board.write_text(json.dumps(case.platform.to_dict(), ensure_ascii=False))
    return case, routes, producer, [tmp_path / "current/routes.json", assignment, board]


@pytest.mark.parametrize("affine,repeated", [(False, False), (True, False), ("banks", False), ("tree", False), (True, True)])
def test_native_configuration_and_whole_stage_match_python_configuration(tmp_path, monkeypatch, affine, repeated):
    case, routes, producer, inputs = native_inputs(tmp_path, affine, repeated)
    install_runtime(tmp_path, monkeypatch)
    reference = tmp_path / "reference"
    expected = run_native_phase5(producer, reference)
    output = tmp_path / "native"
    result = subprocess.run([str(emuflow_native()), "phase5", "compile", *map(str, inputs), str(output / "phase5_report.json")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert read_tdm_schedule(output / "schedule.json", materialize=True) == read_tdm_schedule(reference / "schedule.json", materialize=True)
    assert json.loads((output / "cross_layer_timing.json").read_text()) == json.loads((reference / "cross_layer_timing.json").read_text())
    observed = validate_phase5_generation(output / "phase5_report.json")["report"]
    assert observed["validation"] == expected["validation"]
    assert observed.get("timing_validation") == expected.get("timing_validation")
    assert not list(output.glob(".phase5-generation-*"))


@pytest.mark.parametrize("damage", ["platform", "frequency", "unknown-link", "ratio", "assignment", "section", "orphan"])
def test_native_configuration_rejects_mixed_or_corrupt_inputs_before_replacing_generation(tmp_path, damage):
    import hashlib
    import sqlite3
    from contextlib import closing

    case, routes, producer, inputs = native_inputs(tmp_path)
    output = tmp_path / "native"
    command = [str(emuflow_native()), "phase5", "compile", *map(str, inputs), str(output / "phase5_report.json")]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    if damage in {"platform", "frequency", "unknown-link", "ratio"}:
        board = json.loads(inputs[2].read_text())
        if damage == "platform": board["platform"]["name"] += "-foreign"
        elif damage == "frequency": board["links"][0]["fabric_clock_mhz"] = 0
        elif damage == "unknown-link": board["links"][0]["id"] = "foreign"
        else: board["links"][0]["tdm_delay_model"] = {"model": "linear-ratio-v1", "intercept_ns": 1.0, "ratio_coefficient_ns": 0.5, "min_ratio": 8, "max_ratio": 256, "ratio_quantum": 8}
        inputs[2].write_text(json.dumps(board))
    elif damage == "assignment":
        with closing(sqlite3.connect(inputs[1])) as db, db:
            db.execute("UPDATE metadata SET value=? WHERE key='semantic_contract_sha256'", (json.dumps("0" * 64),))
    elif damage == "section":
        selector = json.loads(inputs[0].read_text()); selector["sections"]["routes"]["records"] += 1
        inputs[0].write_text(json.dumps(selector))
    else:
        selector = json.loads(inputs[0].read_text()); source = inputs[0].parent / selector["artifact"]
        forged = inputs[0].parent / "forged.sqlite"; forged.write_bytes(source.read_bytes())
        with closing(sqlite3.connect(forged)) as db, db:
            db.execute("INSERT INTO route_edges VALUES(99999,0,'link_0_1','fpga0','fpga1')")
        digest = hashlib.sha256(forged.read_bytes()).hexdigest(); artifact = "routes." + digest + ".sqlite"
        forged.rename(inputs[0].parent / artifact)
        selector.update(artifact=artifact, store_sha256=digest, store_bytes=(inputs[0].parent / artifact).stat().st_size)
        inputs[0].write_text(json.dumps(selector))
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before
    assert validate_phase5_generation(output / "phase5_report.json")["status"] == "pass"


@pytest.mark.parametrize("forward,reverse,banks,wires", [(0, 0, 3, 2), (12, 0, 3, 2), (0, 17, 3, 2), (13, 7, 3, 2), (21, 19, 5, 1), (40, 1, 4, 2)])
def test_native_bank_split_preserves_independent_direction_objective(tmp_path, monkeypatch, forward, reverse, banks, wires):
    import copy
    from dataclasses import replace
    from emuflow.common.io import read_json
    from emuflow.routing.routing import normalize_route_constraints
    from emuflow.routing.system_route_storage import read_system_routes, _demand_from_route
    from tests.native_route_seal_fixture import write_route_store_fixture
    from emuflow.scheduling.tdm import build_native_exact_schedule_request

    case, routes, producer, inputs = native_inputs(tmp_path, "banks")
    case.platform = replace(case.platform, links=(replace(case.platform.links[0], bank_count=banks, wires_per_bank=wires, data_lanes_per_direction=banks * wires),))
    value = read_json(inputs[0]); templates = {r["source"]: r for r in value["routes"]}
    population = []
    for source, count in (("fpga0", forward), ("fpga1", reverse)):
        for index in range(count):
            row = copy.deepcopy(templates[source])
            row.update(id=f"{source}-d{index:04d}", net=f"{source}-n{index:04d}", predecessor_cut_nets=[], dependency_level=0, combinational_dependency_depth=0)
            population.append(row)
    value["routes"] = population; value["demands"] = [_demand_from_route(r) for r in population]
    value["constraints"] = normalize_route_constraints(None, case.platform, frame_slots=256)
    write_route_store_fixture(inputs[0], value)
    inputs[2].write_text(json.dumps(case.platform.to_dict()))
    persisted = read_system_routes(inputs[0]); configured = {**persisted, "semantic_contract": routes["semantic_contract"]}
    producer = build_native_exact_schedule_request(configured, case.platform, source_bindings=persisted.bound_store_sources())
    install_runtime(tmp_path, monkeypatch)
    reference = tmp_path / "python-config"
    run_native_phase5(producer, reference)
    target = tmp_path / "native-config"
    result = subprocess.run([str(emuflow_native()), "phase5", "compile", *map(str, inputs), str(target / "phase5_report.json")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert read_tdm_schedule(target / "schedule.json", materialize=True) == read_tdm_schedule(reference / "schedule.json", materialize=True)
