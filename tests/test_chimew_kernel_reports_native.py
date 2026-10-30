"""Current file-owned production reports and independent small kernel oracles."""
import copy
import json
from pathlib import Path
import random
import subprocess
from unittest import mock

import pytest

from emuflow.common.errors import ValidationError
from emuflow.common.io import write_json
from emuflow.transport.chimew.kernel_reports_native import build_chimew_kernel_reports
from tests import test_chimew_qualification as qualification_fixture
from tests.chimew_grouping_fixture import build_chimew_initial_groups
from tests.chimew_refinement_fixture import refine_chimew_groups
from tests.chimew_rudy_fixture import evaluate_chimew_rudy
from tests.native_build import emuflow_native
from tests.schedule_store_fixture import write_tdm_schedule_storage


@pytest.fixture
def case(tmp_path):
    cls = qualification_fixture.ChimewQualificationTest
    cls.setUpClass()
    try:
        fixture = cls()
        fixture.setUp()
        sources = {}
        for role, value in (("schedule", fixture.schedule), ("crossings", fixture.crossings),
                            ("positions", fixture.positions), ("rudy_input", fixture.rudy_input)):
            sources[role] = tmp_path / (role + ".json")
            if role == "schedule":
                write_tdm_schedule_storage(sources[role], value)
            else:
                write_json(sources[role], value)
        yield fixture, sources
    finally:
        cls.tearDownClass()


def run(sources, output):
    return build_chimew_kernel_reports(*(sources[role] for role in ("schedule", "crossings", "positions", "rudy_input")), output)


@pytest.mark.parametrize("seed", range(8))
def test_native_reports_equal_independent_kernel_replay(case, tmp_path, seed):
    fixture, sources = case
    rng = random.Random(seed)
    entries, crossings, positions = [], [], []
    for index in range(32):
        entry = copy.deepcopy(fixture.schedule["entries"][index % 2])
        entry.update(id=f"s{index:04d}", demand=f"d{index}", net=f"n{index}", lane=index // 4,
                     slot=index % 4, tdm_ratio=4, ready_slot=index % 4, arrival_slot=index % 4 + 1)
        entries.append(entry)
        source = sorted(rng.sample(range(2), rng.randrange(3)))
        sink = sorted(rng.sample(range(2), rng.randrange(3)))
        crossings.append({"schedule_entry": entry["id"], "source_slls": source, "sink_slls": sink,
                          "encoding": sum(1 << bit for bit in source) | sum(1 << (2 + bit) for bit in sink)})
        positions.append({"schedule_entry": entry["id"], "source_y": float(rng.randrange(100))})
    fixture.schedule["entries"] = entries
    fixture.crossings.update(entries=crossings, metrics={"signals": len(entries),
        "physical_sll_crossings": sum(bin(row["encoding"]).count("1") for row in crossings)})
    if seed % 2:
        fixture.crossings["timing_guard"] = {"provider": "emuflow-chimew-critical-lane-guard-v1",
            "scope": "EmuFlow extension, not a Chimew paper claim", "protected_entries": [entries[0]["id"], entries[5]["id"]]}
    fixture.positions.update(entries=positions, metrics={"signals": len(entries)})
    for role in ("schedule", "crossings", "positions"):
        if role == "schedule":
            write_tdm_schedule_storage(sources[role], fixture.schedule)
        else:
            write_json(sources[role], getattr(fixture, role))
    initial = build_chimew_initial_groups(fixture.schedule, fixture.crossings)
    refined = refine_chimew_groups(fixture.schedule, fixture.crossings, initial, fixture.positions)
    rudy = evaluate_chimew_rudy(fixture.rudy_input)
    output = tmp_path / "reports"
    # Production exchanges file references; none of these Python report builders
    # or a ScheduleStore entry iterator may participate in the native handoff.
    with mock.patch("tests.chimew_grouping_fixture.build_chimew_initial_groups", side_effect=AssertionError("Python grouping")), \
         mock.patch("tests.chimew_refinement_fixture.refine_chimew_groups", side_effect=AssertionError("Python refinement")), \
         mock.patch("emuflow.scheduling.tdm_schedule_storage.TypedScheduleEntrySequence.__iter__", side_effect=AssertionError("Python entries")):
        summary = run(sources, output)
    for role, expected in (("initial_grouping", initial), ("refined_grouping", refined), ("rudy_report", rudy)):
        assert json.loads((output / (role + ".json")).read_text()) == expected
    from emuflow.transport.chimew.chimew_qualification import canonical_sha256
    assert summary["refined_grouping_sha256"] == canonical_sha256(refined)


@pytest.mark.parametrize("corruption", ("crossing", "position", "rudy", "raw-schedule"))
def test_invalid_input_cannot_publish_any_report(case, tmp_path, corruption):
    fixture, sources = case
    if corruption == "crossing":
        fixture.crossings["entries"][0]["encoding"] ^= 1
        write_json(sources["crossings"], fixture.crossings)
    elif corruption == "position":
        fixture.positions["entries"].pop()
        write_json(sources["positions"], fixture.positions)
    elif corruption == "rudy":
        fixture.rudy_input["nets"][0]["pins"][1]["y"] = 10.0
        write_json(sources["rudy_input"], fixture.rudy_input)
    else:
        write_json(sources["schedule"], fixture.schedule)
    output = tmp_path / "invalid"
    with pytest.raises(ValidationError):
        run(sources, output)
    assert not output.exists()


def test_receipt_cannot_replace_inputs_sidecars_or_outputs(case, tmp_path):
    _, sources = case
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema": "emuflow.chimew-kernel-reports-input/v1", "sources": {
        role: {"path": str(path), "schema": "physical-input-document-v1"} for role, path in sources.items()}}))
    output = tmp_path / "reports"
    selector = json.loads(sources["schedule"].read_text())
    payload = sources["schedule"].parent / selector["artifact"]
    for receipt in (request, *sources.values(), payload, output, output / "unlisted-receipt.json",
                    *(output / (role + ".json") for role in ("initial_grouping", "refined_grouping", "rudy_report", "kernel_reports"))):
        before = {path: path.read_bytes() for path in (*sources.values(), request, payload)}
        completed = subprocess.run([str(emuflow_native()), "--report", str(receipt), "phase6", "build-chimew-kernel-reports",
                                    str(request), str(output)], capture_output=True, text=True)
        assert completed.returncode
        assert before == {path: path.read_bytes() for path in before}
        assert not output.exists()
