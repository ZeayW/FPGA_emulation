"""Optional tools must not become a dependency of the production runtime."""
from __future__ import annotations

import ast
from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import subprocess
import sys

import pytest

from emuflow.cli import _build_parser
from emuflow_tools.cli import _build_parser as tools_parser

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_imports_without_optional_tools_on_search_path(tmp_path):
    code = """import importlib.util, sys
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec('emuflow_tools') is None
import emuflow.cli, emuflow.phase6, emuflow.phase6_native, emuflow.opensta
assert 'emuflow_tools' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT / "src")],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_core_has_no_tool_package_imports():
    for source in (ROOT / "src/emuflow").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("emuflow_tools"), source
            elif isinstance(node, ast.Import):
                assert all(not item.name.startswith("emuflow_tools") for item in node.names), source


@pytest.mark.parametrize("command", [
    ["contest", "--help"], ["benchmark", "--help"],
    ["platform", "arm-mps4-materialize", "--help"],
    ["multi-fpga", "bsp", "--help"], ["multi-fpga", "board-implement", "--help"],
    ["multi-fpga", "board-timing", "--help"], ["phy-provider", "--help"],
    ["runtime-sync", "--help"], ["phase6b", "--help"], ["phase6c", "--help"],
    ["package-pin", "--help"],
    ["multi-fpga", "compile", "--out", "unused", "--serial-bsp-phy-provider", "retired.json"],
])
def test_retired_core_tool_or_bsp_entry_is_rejected(command):
    with redirect_stderr(io.StringIO()), pytest.raises(SystemExit) as error:
        _build_parser().parse_args(command)
    assert error.value.code == 2


def test_standalone_tools_keep_their_own_command_tree():
    args = tools_parser().parse_args(["contest", "matrix-validate", "matrix.json"])
    assert args.contest_command == "matrix-validate"
    args = tools_parser().parse_args(["platform", "arm-mps4-materialize", "--output", "board.json", "--fabric-clock-mhz", "100", "--payload-bits-per-lane-per-cycle", "8", "--latency-cycles", "1"])
    assert args.platform_command == "arm-mps4-materialize"
    for name in ("multi_fpga_bsp_flow", "physical_pins", "serial_wrapper", "vivado_board_flow",
                 "runtime_sync", "serial_phy_provider", "chimew_correlation"):
        assert importlib.util.find_spec("emuflow." + name) is None
        assert importlib.util.find_spec("emuflow_tools." + name) is None


def test_installed_fetch_cli_forwards_explicit_resource_root(tmp_path, capsys):
    from unittest.mock import patch
    from emuflow_tools.cli import main
    from emuflow_tools.contest_public import _resolve_fetcher

    script = tmp_path / "scripts/fetch_eda2023_benchmarks.py"
    script.parent.mkdir()
    script.write_text("# fixed test fetcher path\n")
    assert _resolve_fetcher("scripts/fetch_eda2023_benchmarks.py", tmp_path) == script.resolve()
    with patch("emuflow_tools.cli.fetch_public_contest_case", return_value={"status": "pass"}) as fetch:
        assert main(["contest", "fetch-public", "--matrix", "matrix.json",
                     "--case-id", "eda2023.case1", "--out", str(tmp_path / "out"),
                     "--fetcher-root", str(tmp_path)]) == 0
    assert fetch.call_args.kwargs == {"fetcher_root": tmp_path}
    assert fetch.call_args.args == (Path("matrix.json"), "eda2023.case1", tmp_path / "out")
    assert '"status": "pass"' in capsys.readouterr().out


def test_installed_fetch_cli_rejects_missing_resource_before_launch(tmp_path, monkeypatch, capsys):
    from unittest.mock import patch
    from emuflow_tools.cli import main

    monkeypatch.setattr("emuflow_tools.contest_public._validate_bound_ssl_cert_environment",
                        lambda: {"status": "not-set"})
    output = tmp_path / "output"
    with patch("emuflow_tools.contest_public.subprocess.run") as run:
        assert main(["contest", "fetch-public",
                     "--matrix", str(ROOT / "benchmarks/contest_validation_matrix.json"),
                     "--case-id", "eda2023.case1", "--out", str(output),
                     "--fetcher-root", str(tmp_path / "missing")]) == 1
        run.assert_not_called()
    assert "fetcher is missing" in capsys.readouterr().err
    assert not output.exists()
