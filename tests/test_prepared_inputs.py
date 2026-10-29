"""Completed indexed boundaries stay read-only and skip synthesis/timing."""
from contextlib import closing
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

    bind_prepared_inputs(
        front,
        timing,
        tmp_path / "out/front",
        tmp_path / "out/timing",
        DEFAULT_PLATFORM_PATH,
        {"clk": 10.0},
        PROFILE,
    )

    assert read_json(tmp_path / "out/front/platform.normalized.json") == (
        Platform.load(DEFAULT_PLATFORM_PATH).to_dict()
    )
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
        with closing(sqlite3.connect(payload)) as connection, connection:
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


@pytest.fixture
def clock_boundaries(tmp_path,boundaries):
    """Use the actual v4 importer, including full/half-cycle clock rows."""
    import sqlite3
    from tests.test_sta_clock_budgets import prepare
    from emuflow.sta import sta_path_database_payload_path
    front,timing=boundaries
    _source,manifest,value=prepare(tmp_path/'clock-source')
    # Keep the importer-owned payload and expose it through the normal
    # continuation layout; no fake v4 clock records are manufactured.
    payload=sta_path_database_payload_path(manifest,value)
    (timing/payload.name).symlink_to(payload)
    write_json(timing/'path-database.json',value)
    weights=read_json(timing/'partition-net-weights.json')
    weights.update(design='counter',summary=dict(weighted_nets=1))
    weight_payload=timing/weights['payloads']['weights']['path'];weight_payload.unlink()
    with closing(sqlite3.connect(weight_payload))as db,db:
        db.executescript("PRAGMA user_version=1; CREATE TABLE net_weights(net_id TEXT); INSERT INTO net_weights VALUES('n0')")
    write_json(timing/'partition-net-weights.json',weights)
    return front,timing,payload,value


def test_v4_continuation_keeps_half_cycle_rows_and_never_replays_source(clock_boundaries,tmp_path):
    import sqlite3
    front,timing,payload,value=clock_boundaries
    with patch('emuflow.ir.EmuIR.load',side_effect=AssertionError('whole graph load')):
        bind_prepared_inputs(front,timing,tmp_path/'continued/front',tmp_path/'continued/timing',
                             DEFAULT_PLATFORM_PATH,{'clk':10.},PROFILE)
        checked=validate_prepared_timing_boundary(front,timing,{'clk':10.})
    assert checked['publication_validation']=='sqlite-version-clocks-budget-coverage-v2'
    retained=tmp_path/'continued/timing'/payload.name
    assert retained.is_symlink() and retained.resolve()==payload.resolve()
    with closing(sqlite3.connect(retained))as db:
        assert db.execute('SELECT launch_edge_ns,capture_edge_ns,target_cycle_time_ns FROM clock_budgets '
                          'ORDER BY path_ordinal').fetchall()==[(0.,5.,5.),(5.,0.,10.),(0.,0.,10.)]


def test_v4_mainline_continuation_reaches_v15_partition_without_rerunning_phase1_or_phase2(clock_boundaries,tmp_path):
    front,timing,_payload,_value=clock_boundaries
    architecture=tmp_path/'architecture.json'
    write_json(architecture,dict(schema='emuflow.xilinx-preplacement-timing-db/v1'))
    class ReachedPartition(Exception):
        pass
    context=object()
    with patch('emuflow.multi_fpga_flow.run_phase3',side_effect=ReachedPartition) as phase3, \
         patch('emuflow.opensta_source_context.emit_opensta_source_context',return_value=context), \
         patch('emuflow.multi_fpga_flow.run_phase1',side_effect=AssertionError('repeated synthesis')), \
         patch('emuflow.multi_fpga_flow.run_opensta_path_database',side_effect=AssertionError('repeated Phase2')), \
         patch('emuflow.multi_fpga_flow.resolve_native_executable',return_value='/usr/bin/true'), \
         patch('emuflow.multi_fpga_flow.check_opensta_engine_version'):
        with pytest.raises(ReachedPartition):
            run_multi_fpga_flow(output_dir=tmp_path/'continued',prepared_frontend=front,prepared_timing=timing,
                                clock_periods={'clk':10.},patron_algorithm_version=15,
                                architecture_timing_db=architecture)
    assert phase3.call_args.kwargs['patron_algorithm_version']==15
    assert phase3.call_args.kwargs['patron_source_context'] is context
    assert read_json(tmp_path/'continued/timing/path-database.json')['schema']=='emuflow.sta-path-database/v4'


@pytest.mark.parametrize('damage',['missing','extra','source-clock','source-period','nonfinite','absolute-check',
                                  'schema-format','storage-version','summary-count'])
def test_passing_standalone_report_cannot_hide_broken_v4_budgets(clock_boundaries,tmp_path,damage):
    import sqlite3
    front,timing,payload,value=clock_boundaries
    with closing(sqlite3.connect(payload))as db,db:
        if damage=='missing':db.execute('DELETE FROM clock_budgets WHERE path_ordinal=1')
        elif damage=='extra':db.execute('UPDATE clock_budgets SET path_ordinal=99 WHERE path_ordinal=1')
        elif damage=='source-clock':db.execute("UPDATE clock_budgets SET source_clock='unknown' WHERE path_ordinal=1")
        elif damage=='source-period':db.execute('UPDATE clock_budgets SET source_period_ns=20 WHERE path_ordinal=1')
        elif damage=='nonfinite':db.execute('UPDATE clock_budgets SET check_margin_ns=? WHERE path_ordinal=1',(float('inf'),))
        elif damage=='absolute-check':db.execute('UPDATE clock_budgets SET path_delay=1 WHERE path_ordinal=1')
        elif damage=='storage-version':db.execute('PRAGMA user_version=1')
    if damage=='schema-format':value['payloads']['paths']['format']='sqlite-sta-path/v1'
    elif damage=='summary-count':value['summary']['clock_budget_paths']=2
    write_json(timing/'path-database.json',value)
    for operation in [lambda:bind_prepared_inputs(front,timing,tmp_path/'bad/front',tmp_path/'bad/timing',
                                                DEFAULT_PLATFORM_PATH,{'clk':10.},PROFILE),
                      lambda:validate_prepared_timing_boundary(front,timing,{'clk':10.})]:
        with pytest.raises(ValidationError,match='budget|version|payload'):
            operation()
    assert not(tmp_path/'bad/front').exists()


def test_frontend_only_continuation_reuses_mapping_and_enters_fresh_sta(tmp_path, boundaries):
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    class ReachedTiming(Exception):pass
    timing_model=write_mock_xilinx_timing_contract(tmp_path/'device-timing.json')
    with patch('emuflow.multi_fpga_flow.run_phase1',side_effect=AssertionError('repeated mapping')), \
         patch('emuflow.multi_fpga_flow.run_xilinx_ultrascaleplus_yosys_stream',side_effect=AssertionError('repeated synthesis')), \
         patch('emuflow.multi_fpga_flow.run_opensta_path_database',side_effect=ReachedTiming)as sta, \
         patch('emuflow.multi_fpga_flow.resolve_native_executable',return_value='/usr/bin/true'), \
         patch('emuflow.multi_fpga_flow.check_opensta_engine_version'):
        with pytest.raises(ReachedTiming):
            run_multi_fpga_flow(output_dir=tmp_path/'continued',prepared_frontend=boundaries[0],
                clock_periods={'clk':10.},architecture_timing_db=timing_model)
    assert sta.call_args.kwargs['architecture_timing_db_path']==timing_model
    assert (tmp_path/'continued/frontend/phase1/design.emuir.sqlite').resolve()==boundaries[0]/'design.emuir.sqlite'
    assert not (tmp_path/'continued/timing/path-database.json').exists()


def test_missing_device_controls_is_rejected_before_synthesis(tmp_path):
    from tests.xilinx_timing_fixture import write_mock_xilinx_timing_contract
    from tests.partition_fixture import run_multi_fpga_flow_fixture
    path=write_mock_xilinx_timing_contract(tmp_path/'old-device-timing.json')
    value=read_json(path);value.pop('ff_control_timing');write_json(path,value)
    with patch('emuflow.multi_fpga_flow.run_xilinx_ultrascaleplus_yosys_stream')as synth:
        with pytest.raises(ValidationError,match='FF control timing'):
            run_multi_fpga_flow_fixture(platform_path=DEFAULT_PLATFORM_PATH,output_dir=tmp_path/'flow',
                sources=[ROOT/'examples/rtl/counter.v'],top='counter',clocks=['clk'],clock_periods={'clk':10.},
                architecture_timing_db=path,partition_provider='tritonpart')
        synth.assert_not_called()
    assert not (tmp_path/'flow').exists()
