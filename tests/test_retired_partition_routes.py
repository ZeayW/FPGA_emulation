from tests.compile_adapter_fixture import preflight as run_preflight
"""Retired providers fail before loading a circuit or invoking an engine."""
from pathlib import Path
from unittest.mock import patch

import pytest

from emuflow.flow.cli import _build_parser
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.flow.multi_fpga_flow import run_multi_fpga_flow
from emuflow.partition.phase3 import run_phase3


@pytest.mark.parametrize("provider", ["repart", "repart-replication", "mfspart", "greedy"])
def test_retired_provider_rejected_before_phase_input(tmp_path, provider):
    with patch("emuflow.partition.partition_ir.load_partition_ir", side_effect=AssertionError("circuit loaded")):
        with pytest.raises(ValidationError, match="unsupported Phase 3 provider"):
            run_phase3(tmp_path / "missing.ir", tmp_path / "missing.platform",
                       tmp_path / "partition", provider=provider)
    assert not (tmp_path / "partition/assignment.json").exists()


@pytest.mark.parametrize("provider", ["repart", "repart-replication", "mfspart", "greedy"])
def test_retired_provider_rejected_before_compile_frontend(tmp_path, provider):
    with patch("emuflow.frontend.frontend_native.prepare_frontend", side_effect=AssertionError("frontend entered")):
        with pytest.raises(EmuFlowError, match="unsupported Phase 3 provider"):
            run_preflight(output_dir=tmp_path / "flow", partition_provider=provider)
    assert not (tmp_path / "flow").exists()


@pytest.mark.parametrize("provider", ["repart", "repart-replication", "mfspart", "greedy"])
def test_retired_provider_has_no_cli_route(tmp_path, provider):
    parser = _build_parser()
    for command in (
        ["phase3", "--ir", "ir", "--platform", "board", "--out", "out", "--provider", provider],
        ["multi-fpga", "compile", "design.v", "--out", "out", "--partition-provider", provider],
    ):
        with pytest.raises(SystemExit) as rejected:
            parser.parse_args(command)
        assert rejected.value.code == 2




def test_chimew_and_fixed_binding_entry_remain_available():
    parser = _build_parser()
    args = parser.parse_args([
        "multi-fpga", "compile", "design.v", "--out", "out", "--phase6-provider", "chimew",
    ])
    assert args.phase6_provider == "chimew"
    from emuflow.transport.phase6 import run_phase6
    from emuflow.transport.chimew.chimew_phase6 import run_chimew_phase6_adapter
    assert callable(run_phase6) and callable(run_chimew_phase6_adapter)


@pytest.mark.parametrize("entry", ["phase3", "compile", "native-request"])
@pytest.mark.parametrize("version", [6, 7, 8, 9, 10, 11])
def test_retired_patron_version_is_rejected_before_any_frontend_or_graph_loading(tmp_path, entry, version):
    from emuflow.flow.native_compile import _request
    with patch("emuflow.partition.partition_ir.load_partition_ir", side_effect=AssertionError("graph loaded")), patch(
        "emuflow.frontend.frontend_native.prepare_frontend", side_effect=AssertionError("frontend entered")
    ), pytest.raises(EmuFlowError, match="version|only PATRON"):
        if entry == "phase3":
            run_phase3(tmp_path / "missing.ir", tmp_path / "missing.platform",
                       tmp_path / "partition", patron_algorithm_version=version)
        elif entry == "compile":
            run_multi_fpga_flow(output_dir=tmp_path / "flow", patron_algorithm_version=version)
        else:
            _request((), {"output_dir": tmp_path / "flow", "patron_algorithm_version": version})
    assert not (tmp_path / "flow").exists()


@pytest.mark.parametrize("options", [[], ["--typed-input"],
    ["--typed-input", "--summary-output", "--typed-output"], ["--trace-output"]])
def test_retired_native_snapshot_command_rejects_all_formats_without_opening_input(tmp_path, options):
    import subprocess
    from tests.native_build import emuflow_native
    output = tmp_path / "published.bin"
    output.write_bytes(b"previous published result")
    result = subprocess.run([str(emuflow_native()), "phase3", "refine-patron", *options,
                             str(tmp_path / "missing-input"), str(output)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert output.read_bytes() == b"previous published result"
    assert not Path(str(output) + ".order").exists()
    assert not Path(str(output) + ".summary.json").exists()


@pytest.mark.parametrize('option', ['retain_diagnostics', 'retain_patron_baseline',
                                  'patron_initial_assignment_path', 'patron_initial_clusters_path'])
def test_retired_phase3_snapshot_routes_are_absent_from_public_api(tmp_path, option):
    import inspect
    assert option not in inspect.signature(run_phase3).parameters
    with pytest.raises(TypeError, match=option):
        run_phase3(tmp_path/'missing.ir', tmp_path/'missing.board', tmp_path/'partition', **{option: True})
    assert not (tmp_path/'partition').exists()


def test_plain_graph_phase3_has_no_python_fallback(tmp_path):
    from emuflow.common.io import write_json
    from tests.yosys_reference import import_yosys_json
    root = Path(__file__).parents[1]
    graph = tmp_path/'design.json'
    write_json(graph, import_yosys_json(root/'examples/yosys/counter.json', top='counter', clocks=['clk']).value)
    with patch('emuflow.partition.partition_ir.load_partition_ir', side_effect=AssertionError('Python graph route')):
        with pytest.raises(ValidationError, match='compact EmuIR'):
            run_phase3(graph, root/'platforms/virtual/xcvu3p_2fpga_p2p.json', tmp_path/'partition')
    assert not (tmp_path/'partition/assignment.json').exists()
