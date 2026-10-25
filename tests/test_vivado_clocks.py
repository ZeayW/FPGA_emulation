import copy
import csv
import pytest

from emuflow.errors import ValidationError
from emuflow.io import read_json
from emuflow.vivado_clocks import implementation_clocks, implementation_timing_contract
from emuflow.vivado_session import prepare_manifest


def runtime():
    return {
        "fabric_clock": {"period_ns": 5.0},
        "virtual_dut_clocks": {
            "core": {"target_period_ns": 10.0, "nominal_period_ns": 4096.0},
            "csb": {"target_period_ns": 20.0, "nominal_period_ns": 8192.0},
        },
        "timing_model": {"fabric_clock_port": "fabric_clk", "dut_clock_ports": ["core", "csb"],
                         "fabric_to_dut_max_delay_ns": 3728.0},
    }


def test_frame_size_does_not_relax_implementation_targets():
    value = runtime()
    clocks = implementation_clocks(value, {"fabric_clk", "core", "csb"})
    assert clocks == [("emuflow_tdm_clk", "fabric_clk", 5.0),
                      ("emuflow_dut_clk_0", "core", 10.0), ("emuflow_dut_clk_1", "csb", 20.0)]
    changed = copy.deepcopy(value)
    for clock in changed["virtual_dut_clocks"].values():
        clock["nominal_period_ns"] *= 2
    changed["timing_model"]["fabric_to_dut_max_delay_ns"] *= 2
    assert implementation_timing_contract(changed) == implementation_timing_contract(value)
    assert implementation_clocks(changed, {"fabric_clk", "core", "csb"}) == clocks


@pytest.mark.parametrize("value", [None, True, 0, -10, float("nan"), float("inf"), "10"])
def test_missing_or_invalid_target_cannot_fall_back_to_frame_period(value):
    record = runtime()
    record["virtual_dut_clocks"]["csb"]["target_period_ns"] = value
    # Validate even a clock absent from this partition: the global input must
    # have explicit periods for all declared roots before native execution.
    with pytest.raises(ValidationError, match="explicit positive target period: csb"):
        implementation_clocks(record, {"fabric_clk", "core"})


def test_native_clock_admission_uses_original_targets_and_keeps_root_identity(tmp_path):
    prepare_manifest(tmp_path, fpga="fpga0", part="part", mapped_index=tmp_path/"mapped.sqlite",
                     functions=tmp_path/"functions.sqlite", mapped_sha256="a"*64, queries={},
                     runtime=runtime(), ports={"fabric_clk", "csb"})
    with (tmp_path/"clock-contract.tsv").open() as stream:
        rows = [(bytes.fromhex(row["clock_hex"]).decode(), bytes.fromhex(row["port_hex"]).decode(),
                 float(row["period_ns"])) for row in csv.DictReader(stream, delimiter="\t")]
    assert rows == [("emuflow_tdm_clk", "fabric_clk", 5.0), ("emuflow_dut_clk_1", "csb", 20.0)]
    assert read_json(tmp_path/"native-session.json")["implementation_timing"] == implementation_timing_contract(runtime())


def test_explicit_ten_mhz_target_scales_roots_and_is_independent_of_partition():
    value = runtime()
    original = copy.deepcopy(value)
    contract = implementation_timing_contract(value, 10.0)
    assert contract["dut_clock_periods_ns"] == {"core": 100.0, "csb": 200.0}
    assert contract["source_dut_clock_periods_ns"] == {"core": 10.0, "csb": 20.0}
    assert contract["tdm_clock"] == {"port": "fabric_clk", "period_ns": 5.0}
    assert contract["dut_period_scale"] == 10.0
    assert value == original
    for port, index, period in (("core", 0, 100.0), ("csb", 1, 200.0)):
        assert implementation_clocks(value, {"fabric_clk", port}, 10.0) == [
            ("emuflow_tdm_clk", "fabric_clk", 5.0), (f"emuflow_dut_clk_{index}", port, period)]
    value["virtual_dut_clocks"]["csb"]["target_period_ns"] = 10.0
    assert implementation_timing_contract(value, 10.0)["dut_clock_periods_ns"] == {"core": 100.0, "csb": 100.0}


def test_native_manifest_and_xdc_use_the_same_explicit_candidate(tmp_path):
    from emuflow.vivado_backend import vivado_runtime_xdc
    from emuflow.io import write_json
    from tests.test_vivado_backend import _ir
    value = runtime()
    value["virtual_dut_clocks"]["clk"] = value["virtual_dut_clocks"].pop("core")
    value["timing_model"]["dut_clock_ports"] = ["clk", "csb"]
    source = tmp_path / "ir.json"
    write_json(source, _ir().to_dict())
    xdc = vivado_runtime_xdc(source, value, 10.0)
    assert "emuflow_tdm_clk -period 5.000000000" in xdc
    assert "emuflow_dut_clk_0 -period 100.000000000" in xdc
    assert "4096" not in xdc and "set_max_delay" not in xdc
    prepare_manifest(tmp_path, fpga="fpga0", part="part", mapped_index=tmp_path/"mapped.sqlite",
                     functions=tmp_path/"functions.sqlite", mapped_sha256="a"*64, queries={},
                     runtime=value, ports={"fabric_clk", "clk"}, dut_frequency_mhz=10.0)
    assert read_json(tmp_path/"native-session.json")["implementation_timing"] == implementation_timing_contract(value, 10.0)
    with (tmp_path/"clock-contract.tsv").open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert [float(row["period_ns"]) for row in rows] == [5.0, 100.0]


@pytest.mark.parametrize("frequency", [True, 0.0, -10.0, float("nan"), float("inf"), "10"])
def test_invalid_implementation_frequency_fails_before_native_work(tmp_path, frequency):
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    from emuflow.multi_fpga_physical_flow import run_multi_fpga_physical_flow
    with pytest.raises(ValidationError, match="finite and positive"):
        run_multi_fpga_flow(output_dir=tmp_path, physical=True, physical_backend="vivado",
                            physical_vivado_dut_frequency_mhz=frequency)
    with pytest.raises(ValidationError, match="finite and positive"):
        run_multi_fpga_physical_flow(tmp_path, tmp_path, tmp_path, tmp_path,
                                    backend="vivado", vivado_dut_frequency_mhz=frequency)


def test_vivado_frequency_cannot_be_ignored_by_another_backend(tmp_path):
    from emuflow.multi_fpga_flow import run_multi_fpga_flow
    from emuflow.multi_fpga_physical_flow import run_multi_fpga_physical_flow
    from emuflow.cli import _build_parser
    with pytest.raises(ValidationError, match="requires Vivado physical execution"):
        run_multi_fpga_flow(output_dir=tmp_path, physical_vivado_dut_frequency_mhz=10.0)
    with pytest.raises(ValidationError, match="requires Vivado physical execution"):
        run_multi_fpga_physical_flow(tmp_path, tmp_path, tmp_path, tmp_path, vivado_dut_frequency_mhz=10.0)
    parser = _build_parser()
    compile_args = parser.parse_args(["multi-fpga", "compile", "design.v", "--out", str(tmp_path),
                                      "--physical", "--physical-backend", "vivado", "--physical-vivado-dut-frequency-mhz", "10"])
    physical_args = parser.parse_args(["multi-fpga", "physical", "--split", str(tmp_path), "--platform", "board",
                                       "--schedule", "schedule", "--out", str(tmp_path), "--backend", "vivado",
                                       "--vivado-dut-frequency-mhz", "10"])
    assert compile_args.physical_vivado_dut_frequency_mhz == physical_args.vivado_dut_frequency_mhz == 10.0


def test_native_compile_control_keeps_the_explicit_frequency(tmp_path):
    from emuflow.native_compile import _request
    from emuflow.native_compile_adapter import _initial_context
    options = dict(output_dir=tmp_path, physical=True, physical_backend="vivado",
                   physical_vivado_dut_frequency_mhz=10.0)
    request, _ = _request((), options)
    assert request["options"]["physical_vivado_dut_frequency_mhz"] == 10.0
    ctx = _initial_context(request)
    assert ctx.physical_vivado_dut_frequency_mhz == 10.0
    options["physical_vivado_dut_frequency_mhz"] = float("nan")
    with pytest.raises(ValidationError, match="finite and positive"):
        _request((), options)


def test_physical_result_schema_admits_distinct_clock_roles_and_rejects_zero_period():
    import json
    from pathlib import Path
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((Path(__file__).resolve().parents[1]/"schemas/physical-partition-result-v1.schema.json").read_text())
    clocks = {"fabric_period_ns": 5.0, "dut_period_ns": 100.0,
              "dut_clock_periods_ns": {"core": 100.0, "csb": 200.0},
              "virtual_runtime_dut_clock_periods_ns": {"core": 4096.0, "csb": 8192.0}}
    validator = jsonschema.Draft202012Validator(schema["properties"]["clocks"])
    validator.validate(clocks)
    clocks["dut_clock_periods_ns"]["core"] = 0.0
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(clocks)
