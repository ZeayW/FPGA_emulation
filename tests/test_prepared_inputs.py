"""Completed indexed boundaries stay read-only and skip synthesis/timing."""
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.default_platform import DEFAULT_PLATFORM_PATH
from emuflow.emuir_compact import write_compact_emuir
from emuflow.errors import ValidationError
from emuflow.io import read_json, write_json
from emuflow.multi_fpga_flow import _checked_flow_member, run_multi_fpga_flow
from emuflow.prepared_inputs import (
    bind_prepared_inputs,
    validate_prepared_timing_boundary,
)
from emuflow.platform import Platform
from emuflow.yosys import import_yosys_json

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "xilinx-ultrascaleplus-open-v1"


@pytest.fixture
def boundaries(tmp_path):
    front, timing = tmp_path / "front", tmp_path / "timing"
    front.mkdir(); timing.mkdir()
    ir = import_yosys_json(ROOT / "examples/yosys/counter.json", top="counter", clocks=["clk"])
    write_compact_emuir(front / "design.emuir.sqlite", ir.to_dict())
    board = read_json(DEFAULT_PLATFORM_PATH)
    write_json(front / "platform.normalized.json", board)
    write_json(front / "phase1_report.source.json", {
        "status": "pass", "design": "counter", "indexed_staging_transform": {
            "primitive_audit": {"status": "pass", "mapping_profile": PROFILE}}})
    write_json(timing / "experiment-timing-report.json", {"status": "pass", "clocks": {"clk": 10.0}})
    for name, value in [
        ("path-database.json", {"schema": "emuflow.sta-path-database/v3", "design": "counter"}),
        ("partition-net-weights.json", {"schema": "emuflow.partition-net-weights/v2"}),
    ]:
        payload_name = name + ".sqlite"
        (timing / payload_name).write_bytes(b"fixture-indexed-payload")
        value["payloads"] = {"paths" if name.startswith("path-") else "weights": {"path": payload_name, "format": "sqlite-sta-path/v1" if name.startswith("path-") else "sqlite-net-weights/v1", "records": 1, "sha256": "a" * 64}}
        write_json(timing / name, value)
    return front, timing


def test_binds_payloads_without_copying_or_loading_graph(tmp_path, boundaries):
    front, timing = boundaries
    with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("whole graph load")):
        bind_prepared_inputs(front, timing, tmp_path / "out/front", tmp_path / "out/timing",
                             DEFAULT_PLATFORM_PATH, {"clk": 10.0}, PROFILE)
    assert (tmp_path / "out/front/design.emuir.sqlite").resolve() == front / "design.emuir.sqlite"
    assert (tmp_path / "out/timing/path-database.json.sqlite").is_symlink()
    assert read_json(tmp_path / "out/front/platform.normalized.json") == Platform.load(DEFAULT_PLATFORM_PATH).to_dict()


def test_terminal_validation_accepts_only_the_declared_prepared_emuir(tmp_path, boundaries):
    root = tmp_path / "flow"
    front = root / "frontend/phase1"
    bind_prepared_inputs(*boundaries, front, root / "timing",
                         DEFAULT_PLATFORM_PATH, {"clk": 10.0}, PROFILE)
    emuir = Path("frontend/phase1/design.emuir.sqlite")
    assert _checked_flow_member(root, emuir, "EmuIR") == root / emuir
    report = read_json(front / "phase1_report.json")
    report["continuation"]["source"] = str(tmp_path / "other.sqlite")
    write_json(front / "phase1_report.json", report)
    with pytest.raises(ValidationError, match="missing"):
        _checked_flow_member(root, emuir, "EmuIR")
    board = front / "platform.normalized.json"
    board.unlink()
    board.symlink_to(boundaries[0] / "platform.normalized.json")
    with pytest.raises(ValidationError, match="missing"):
        _checked_flow_member(root, board, "platform")


def test_rejects_changed_clock_contract(tmp_path, boundaries):
    with pytest.raises(ValidationError, match="clock constraints mismatch"):
        bind_prepared_inputs(*boundaries, tmp_path / "out/front", tmp_path / "out/timing",
                             DEFAULT_PLATFORM_PATH, {"clk": 20.0}, PROFILE)


def test_saved_link_schema_is_not_a_current_routing_input(tmp_path, boundaries):
    front, timing = boundaries
    board = read_json(front / "platform.normalized.json")
    # Historical normalized boards predate the explicit TDM ratio domain.
    for link in board["links"]:
        link["tdm_delay_model"] = {"model": "linear-ratio-v1",
                                   "intercept_ns": 30.0,
                                   "ratio_coefficient_ns": 0.5}
    write_json(front / "platform.normalized.json", board)
    with pytest.raises(ValidationError, match="unexpected or missing fields"):
        Platform.load(front / "platform.normalized.json")
    bind_prepared_inputs(front, timing, tmp_path / "out/front", tmp_path / "out/timing",
                         DEFAULT_PLATFORM_PATH, {"clk": 10.0}, PROFILE)
    assert read_json(tmp_path / "out/front/platform.normalized.json") == Platform.load(DEFAULT_PLATFORM_PATH).to_dict()
    assert read_json(front / "platform.normalized.json") == board


@pytest.mark.parametrize("part", [None, "", "xcvu9p-flga2104-2-e"])
def test_saved_device_identity_still_enforced(tmp_path, boundaries, part):
    front, timing = boundaries
    board = read_json(front / "platform.normalized.json")
    board["fpgas"][0]["part"] = part
    write_json(front / "platform.normalized.json", board)
    with pytest.raises(ValidationError, match="device identity|device part differs"):
        bind_prepared_inputs(front, timing, tmp_path / "out/front", tmp_path / "out/timing",
                             DEFAULT_PLATFORM_PATH, {"clk": 10.0}, PROFILE)


def test_full_flow_continuation_reaches_phase3_without_synthesis_or_sta(tmp_path, boundaries):
    class ReachedPartition(Exception):
        pass
    timing_model = tmp_path / "architecture-timing.json"
    write_json(timing_model, {"schema": "emuflow.xilinx-preplacement-timing-db/v1"})
    with patch("emuflow.multi_fpga_flow.run_phase3", side_effect=ReachedPartition), \
         patch("emuflow.multi_fpga_flow.run_phase1", side_effect=AssertionError("synthesis")), \
         patch("emuflow.multi_fpga_flow.run_opensta_path_database", side_effect=AssertionError("Phase 2")), \
         patch("emuflow.multi_fpga_flow.resolve_native_executable", return_value="/usr/bin/true"), \
         patch("emuflow.multi_fpga_flow.check_opensta_engine_version"):
        with pytest.raises(ReachedPartition):
            run_multi_fpga_flow(output_dir=tmp_path / "continued", prepared_frontend=boundaries[0],
                               prepared_timing=boundaries[1], clock_periods={"clk": 10.0},
                               architecture_timing_db=timing_model)
    assert read_json(tmp_path / "continued/stage-metrics/phase3.json")["status"] == "failed"


def test_native_published_phase2_checks_clocks_and_counts_without_standalone_report(tmp_path, boundaries):
    import sqlite3
    front, timing = boundaries
    (timing / "experiment-timing-report.json").unlink()
    for name, schema, table, sql in (
        ("path-database.json", "emuflow.sta-path-database/v3", "paths",
         "CREATE TABLE paths(clock_domain TEXT,clock_period_ns REAL); INSERT INTO paths VALUES('clk',10.0)"),
        ("partition-net-weights.json", "emuflow.partition-net-weights/v2", "net_weights",
         "CREATE TABLE net_weights(net_id TEXT); INSERT INTO net_weights VALUES('n0')"),
    ):
        manifest = read_json(timing / name)
        payload = timing / next(iter(manifest["payloads"].values()))["path"]
        payload.unlink()
        with sqlite3.connect(payload) as connection:
            connection.executescript("PRAGMA user_version=1; " + sql)
        manifest["design"] = "counter"
        manifest["summary"] = {"paths" if table == "paths" else "weighted_nets": 1}
        write_json(timing / name, manifest)
    checked = validate_prepared_timing_boundary(
        front, timing, {"clk": 10.0}
    )
    assert checked["publication_validation"] == (
        "sqlite-version-clocks-row-counts-v1"
    )
    with pytest.raises(ValidationError, match="clock constraints mismatch"):
        validate_prepared_timing_boundary(front, timing, {"clk": 20.0})
    with patch("emuflow.ir.EmuIR.load", side_effect=AssertionError("graph replay")):
        bind_prepared_inputs(front, timing, tmp_path / "valid/front", tmp_path / "valid/timing",
                             DEFAULT_PLATFORM_PATH, {"clk": 10.0}, PROFILE)
    assert read_json(tmp_path / "valid/timing/experiment-timing-report.json")["publication_validation"] == "sqlite-version-clocks-row-counts-v1"
    with pytest.raises(ValidationError, match="clock constraints mismatch"):
        bind_prepared_inputs(front, timing, tmp_path / "invalid/front", tmp_path / "invalid/timing",
                             DEFAULT_PLATFORM_PATH, {"clk": 20.0}, PROFILE)
