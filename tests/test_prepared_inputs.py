"""Completed indexed boundaries stay read-only and skip synthesis/timing."""
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH
from emuflow.ir.emuir_compact import write_compact_emuir
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json, write_json
from emuflow_tools.flow_validation import _checked_flow_member
from tests.frontend_input_fixture import input_request, write_timing, write_report
from tests.native_build import emuflow_native
import subprocess


def adopt(front, timing, root, platform=DEFAULT_PLATFORM_PATH, clocks=None):
    request = input_request(front, timing, root, platform, clocks or {"clk": 10.0})
    result = subprocess.run([str(emuflow_native()), "phase1", "import-inputs", str(request), str(root)], capture_output=True, text=True)
    if result.returncode:
        raise ValidationError(result.stderr)
    return root / "frontend/phase1"

from emuflow.platform.platform import Platform
from tests.yosys_reference import import_yosys_json

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
    write_report(front, "counter")
    write_timing(timing, "counter", {"clk": 10.0}, front / "design.emuir.sqlite")
    write_json(timing / "experiment-timing-report.json", {"status": "pass", "clocks": {"clk": 10.0}})
    return front, timing


def test_binds_payloads_without_copying_or_loading_graph(tmp_path, boundaries):
    front, timing = boundaries
    with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("whole graph load")):
        adopt(front, timing, tmp_path / "out")
    assert (tmp_path / "out/frontend/phase1/design.emuir.sqlite").resolve() == front / "design.emuir.sqlite"
    assert (tmp_path / "out/timing/paths.sqlite").is_symlink()
    assert read_json(tmp_path / "out/frontend/phase1/platform.normalized.json") == Platform.load(DEFAULT_PLATFORM_PATH).to_dict()


def test_rebinds_legacy_platform_snapshot_without_parsing_obsolete_links(
    tmp_path, boundaries
):
    front, timing = boundaries
    legacy = read_json(front / "platform.normalized.json")
    # Historical prepared inputs contribute only device identity. Make their
    # saved link model deliberately invalid for today's full BoardDB reader to
    # prove the binder does not infer or reuse obsolete interconnect semantics.
    for link in legacy["links"]:
        model = link.get("tdm_delay_model")
        if model is not None:
            model.pop("ratio_coefficient_ns")
    write_json(front / "platform.normalized.json", legacy)

    adopt(front, timing, tmp_path / "out")

    assert read_json(tmp_path / "out/frontend/phase1/platform.normalized.json") == (
        Platform.load(DEFAULT_PLATFORM_PATH).to_dict()
    )
def test_terminal_validation_accepts_only_the_declared_prepared_emuir(tmp_path, boundaries):
    root = tmp_path / "flow"
    front = root / "frontend/phase1"
    adopt(*boundaries, root)
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
        adopt(*boundaries, tmp_path / "out", clocks={"clk": 20.0})


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
    adopt(front, timing, tmp_path / "out")
    assert read_json(tmp_path / "out/frontend/phase1/platform.normalized.json") == Platform.load(DEFAULT_PLATFORM_PATH).to_dict()
    assert read_json(front / "platform.normalized.json") == board


@pytest.mark.parametrize("part", [None, "", "xcvu9p-flga2104-2-e"])
def test_saved_device_identity_still_enforced(tmp_path, boundaries, part):
    front, timing = boundaries
    board = read_json(front / "platform.normalized.json")
    board["fpgas"][0]["part"] = part
    write_json(front / "platform.normalized.json", board)
    with pytest.raises(ValidationError, match="device identity|device part differs"):
        adopt(front, timing, tmp_path / "out")


def test_native_published_phase2_checks_clocks_counts_and_source_seals(tmp_path, boundaries):
    front, timing = boundaries
    (timing / "experiment-timing-report.json").unlink()
    root = tmp_path / "checked"
    adopt(front, timing, root)
    checked = read_json(root / "timing/prepared-timing-summary.json")["validation"]
    assert checked["publication_validation"] == "native-typed-source-endpoint-net-clock-count-and-content-seals-v2"
    assert checked["path_source_validation"]["status"] == "pass"
    assert checked["path_source_validation"]["paths"] == 1
    with pytest.raises(ValidationError, match="clock constraints mismatch"):
        adopt(front, timing, tmp_path / "wrong-clock", clocks={"clk": 20.0})
    with patch("emuflow.ir.ir.EmuIR.load", side_effect=AssertionError("graph replay")):
        adopt(front, timing, tmp_path / "valid")
    selector = read_json(timing / "path-database.json")
    selector["summary"]["paths"] += 1
    write_json(timing / "path-database.json", selector)
    with pytest.raises(ValidationError, match="count mismatch"):
        adopt(front, timing, tmp_path / "invalid")
    assert not (tmp_path / "invalid/frontend/phase1").exists()
    assert not (tmp_path / "invalid/timing").exists()


@pytest.mark.parametrize("damage", ["endpoint", "net"])
def test_prepared_timing_from_another_netlist_is_rejected_before_publication(
    tmp_path, boundaries, damage
):
    import hashlib
    import sqlite3
    from emuflow.common.native_tools import native_file_generation
    front, timing = boundaries
    selector = read_json(timing / "path-database.json")
    store = timing / selector["payloads"]["paths"]["path"]
    with sqlite3.connect(store) as db:
        if damage == "endpoint":
            # The selected mapped source has only scalar clk. A freshly sealed
            # old timing certificate for clk[2] must not be admitted.
            db.execute("UPDATE paths SET path_id='clk->clk__2#00000000',"
                "start_object='clk',start_port='clk',start_bit=0,"
                "end_object='clk__2',end_port='clk',end_bit=2")
            selector["summary"]["structured_endpoint_paths"] = 1
        else:
            db.execute("UPDATE path_nets SET net_id='not-in-selected-source'")
    selector["payloads"]["paths"]["sha256"] = hashlib.sha256(store.read_bytes()).hexdigest()
    write_json(timing / "path-database.json", selector)
    sources = [front / "design.emuir.sqlite", store, timing / "path-database.json"]
    generations = [native_file_generation(path) for path in sources]
    with pytest.raises(ValidationError, match="endpoint identity|unknown EmuIR nets"):
        adopt(front, timing, tmp_path / "rejected")
    assert [native_file_generation(path) for path in sources] == generations
    assert not (tmp_path / "rejected/frontend/phase1").exists()
    assert not (tmp_path / "rejected/timing").exists()
    assert not list((tmp_path / "rejected").glob(".frontend-input-work-*"))


@pytest.mark.parametrize("damage", ["content", "schema", "missing", "escape"])
def test_native_prepared_input_rejects_unsealed_or_incomplete_publication(tmp_path, boundaries, damage):
    front, timing = boundaries
    if damage == "content":
        with (timing / "paths.sqlite").open("ab") as stream:
            stream.write(b"changed")
    else:
        selector = read_json(timing / "path-database.json")
        if damage == "schema": selector["schema"] = "unknown"
        if damage == "missing": selector["payloads"]["paths"]["path"] = "missing.sqlite"
        if damage == "escape": selector["payloads"]["paths"]["path"] = "../paths.sqlite"
        write_json(timing / "path-database.json", selector)
    with pytest.raises((ValidationError, FileNotFoundError)):
        adopt(front, timing, tmp_path / "invalid")
    assert not (tmp_path / "invalid/frontend/phase1").exists()


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("top", [None, "counter"])
def test_supplied_json_is_imported_natively_without_staging_copy(tmp_path, compressed, top):
    import gzip
    from types import SimpleNamespace
    from emuflow.frontend.inputs import prepare_inputs
    source = ROOT / "examples/yosys/counter.json"
    if compressed:
        packed = tmp_path / "source.json.gz"
        packed.write_bytes(gzip.compress(source.read_bytes()))
        source = packed
    ctx = SimpleNamespace(prepared_frontend=None, prepared_timing=None, sources=(),
        yosys_json=source, include_dirs=(), defines=(), platform_path=DEFAULT_PLATFORM_PATH,
        output_dir=tmp_path / "out", top=top, clocks=("clk",), clock_periods={"clk": 10.0}, mapping_profile=PROFILE)
    from emuflow.common.native_tools import native_file_generation
    generation = native_file_generation(source)
    request = prepare_inputs(ctx)
    result = subprocess.run([str(emuflow_native()), "phase1", "import-inputs", str(request), str(ctx.output_dir)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert native_file_generation(source) == generation
    assert (ctx.output_dir / "frontend/phase1/design.emuir.sqlite").is_file()
    assert not list((ctx.output_dir / "frontend").glob("synthesized*"))
    assert not (ctx.output_dir / "timing").exists()


def test_prepared_clock_without_paths_is_preserved(tmp_path, boundaries):
    import json
    import sqlite3
    front, timing = boundaries
    path = front / "design.emuir.sqlite"
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("SELECT record_json FROM clocks LIMIT 1").fetchone()
        clock = json.loads(row[0]);clock["id"] = "unused_clk"
        connection.execute("INSERT INTO clocks(seq,record_json) SELECT MAX(seq)+1,? FROM clocks", (json.dumps(clock),))
        connection.commit()
    finally:
        connection.close()
    clocks = {"clk": 10.0, "unused_clk": 20.0}
    write_json(timing / "experiment-timing-report.json", {"status": "pass", "clocks": clocks})
    adopt(front, timing, tmp_path / "out", clocks=clocks)
    checked = read_json(tmp_path / "out/timing/prepared-timing-summary.json")["validation"]
    assert checked["clocks"] == clocks
    assert checked["sta"]["paths"] == 1
