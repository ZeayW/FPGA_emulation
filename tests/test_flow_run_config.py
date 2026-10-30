"""Source/configuration and entry integration; not NVDLA terminal qualification."""

import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from emuflow.common.errors import EmuFlowError
from emuflow.common.json_io import file_sha256, write_json
from emuflow.flow import environment as env
from emuflow.flow import run_config as run
from emuflow_tools.designs import prepare


@pytest.fixture
def profile(tmp_path):
    root = tmp_path / "runtime"
    for name in ("emuflow-native", "yosys", "sta", "openroad"):
        p = root / "bin" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    files = {}
    for name in ("architecture", "native_constraints", "provider_manifest", "jar"):
        p = tmp_path / (name + ".json")
        p.write_text("{}\n")
        files[name] = str(p)
    for name in ("timing_data_dir", "device_data", "openparf_install"):
        p = tmp_path / name
        p.mkdir()
        files[name] = str(p)
    for name in ("java", "javac", "openparf_python", "vivado"):
        p = tmp_path / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        files[name] = str(p)
    source = root / "share/emuflow/scripts/rapidwright/EmuFlowRWRoute.java"
    source.parent.mkdir(parents=True)
    source.write_text("// explicit runtime fixture\n")
    timing = tmp_path / "preplacement.json"
    timing.write_text("{}\n")
    value = {
        "schema": env.SCHEMA,
        "native_root": str(root),
        **{k: files[k] for k in env._PATH_KEYS if k != "native_root"},
        "backends": {
            "rapidwright": {
                k: files[k]
                for k in env._BACKEND_KEYS["rapidwright"]
                if k != "java_source"
            },
            "vivado": {"executable": files["vivado"]},
        },
        "preplacement_timing": {"path": str(timing), "sha256": file_sha256(timing)},
        "environment": {"LM_LICENSE_FILE": "explicit-fixture-license"},
    }
    path = tmp_path / "environment.json"
    write_json(path, value)
    data = tmp_path / "timing_data_dir" / "source.txt"
    data.write_text("pinned source fixture")
    with (
        patch.object(
            env, "RAPIDWRIGHT_TIMING_DATA_SHA256", {data.name: file_sha256(data)}
        ),
        patch.object(
            env,
            "validate_xilinx_preplacement_timing_db",
            return_value={"status": "pass"},
        ),
    ):
        yield path


@pytest.fixture
def design(tmp_path, profile):
    from tests.test_nvdla import NvdlaRamStubTest
    from emuflow.flow.designs.nvdla_contract import _catalog_entry, CATALOG_PATH

    source, _, _ = NvdlaRamStubTest()._prepared_fixture(tmp_path)
    catalog = _catalog_entry(CATALOG_PATH)
    write_json(
        source / ".emuflow-source.json",
        {
            "schema": "emuflow.source-archive/v1",
            "design_id": "nvdla",
            "revision": catalog["revision"],
            "archive_sha256": catalog["archive_sha256"],
        },
    )
    output = tmp_path / "nvdla.run.json"
    prepare.prepare_nvdla_run(source, output, environment_path=profile)
    return output, source


def test_environment_reports_every_missing_input_without_launching_tools(tmp_path):
    profile = {"schema": env.SCHEMA, "backends": {"rapidwright": {}, "vivado": {}}}
    with patch("subprocess.run", side_effect=AssertionError("engine launched")):
        report = env.inspect_environment(profile)
    names = {item["item"] for item in report["missing"]}
    assert report["status"] == "fail"
    assert {
        "native_root",
        "emuflow-native",
        "yosys",
        "sta",
        "openroad",
        "architecture",
        "native_constraints",
        "provider_manifest",
        "timing_data_dir",
        "rapidwright.jar",
        "vivado.executable",
        "preplacement_timing",
    } <= names


def test_environment_rejects_tampered_internal_timing(profile):
    p = env.load_environment(profile)
    Path(p["preplacement_timing"]["path"]).write_text("changed")
    assert env.inspect_environment(p)["status"] == "fail"


def test_profile_scope_restores_license_and_tool_context_even_after_failure(
    profile, monkeypatch
):
    monkeypatch.setenv("LM_LICENSE_FILE", "original")
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", "original-root")
    with pytest.raises(RuntimeError):
        with env.environment_scope(env.load_environment(profile)):
            assert os.environ["LM_LICENSE_FILE"] == "explicit-fixture-license"
            raise RuntimeError()
    assert os.environ["LM_LICENSE_FILE"] == "original"
    assert os.environ["EMUFLOW_NATIVE_ROOT"] == "original-root"


def test_environment_path_prefers_explicit_then_installation(tmp_path, monkeypatch):
    monkeypatch.delenv(env.ENVIRONMENT_VARIABLE, raising=False)
    native_root = tmp_path / "native"
    monkeypatch.setenv("EMUFLOW_NATIVE_ROOT", str(native_root))
    assert (
        env.default_environment_path()
        == (native_root / "share/emuflow/environment.json").resolve()
    )
    explicit = tmp_path / "managed/environment.json"
    monkeypatch.setenv(env.ENVIRONMENT_VARIABLE, str(explicit))
    assert env.default_environment_path() == explicit.resolve()


def test_environment_path_requires_complete_installation(monkeypatch):
    monkeypatch.delenv(env.ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.delenv("EMUFLOW_NATIVE_ROOT", raising=False)
    with pytest.raises(EmuFlowError, match=r"run ./install\.sh"):
        env.default_environment_path()


def test_manual_environment_registration_cli_is_retired():
    from emuflow.flow.cli import _build_parser

    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            ["environment", "prepare", "machine.json"]
        )
    with pytest.raises(SystemExit):
        _build_parser().parse_args(
            [
                "run",
                "top.sv",
                "--top",
                "top",
                "--clock",
                "clk=10",
                "--environment",
                "machine.json",
            ]
        )


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
def test_complete_run_configuration_uses_the_same_native_controller(
    design, backend, tmp_path
):
    config, _ = design
    with patch(
        "emuflow.flow.native_compile.run_native_multi_fpga_flow",
        return_value={"status": "pass"},
    ) as native:
        assert (
            run.execute_run(config, tmp_path / "flow", backend=backend)["status"]
            == "pass"
        )
    native.assert_called_once()
    options = native.call_args.kwargs
    assert options["top"] == "NV_nvdla"
    assert options["clocks"] == ["dla_core_clk", "dla_csb_clk"]
    assert options["clock_periods"] == {"dla_core_clk": 10.0, "dla_csb_clk": 10.0}
    assert options["physical"] is True and options["physical_backend"] == backend
    assert (
        options["partition_provider"] == "patron"
        and options["patron_algorithm_version"] == 14
    )
    assert len(options["sources"]) >= 250
    assert set(str(path) for path in options["sources"]) <= set(
        options["source_checksums"]
    )
    if backend == "vivado":
        assert options["physical_vivado"] == str(tmp_path / "vivado")
        from emuflow.flow.native_compile import _request
        request, _ = _request((), options)
        assert request["options"]["physical_openparf_python"] is None
    else:
        assert options["physical_openparf_python"] == tmp_path / "openparf_python"
        assert options["physical_rapidwright_java_source"].is_file()


def test_repeated_design_preparation_does_not_rewrite_live_generated_rtl(
    design, profile, tmp_path
):
    config, source = design
    _, v, benchmark, _ = run.load_run(config)
    generated = [
        source / item["path"]
        for item in benchmark.value["preparation"]["generated_files"]
    ]
    before = [p.stat().st_mtime_ns for p in generated]
    prepare.prepare_nvdla_run(
        source, tmp_path / "another.run.json", environment_path=profile
    )
    assert [p.stat().st_mtime_ns for p in generated] == before


@pytest.mark.parametrize(
    "mutation",
    ["rtl", "include", "generated", "benchmark", "memory", "revision", "option"],
)
def test_source_or_policy_changes_reject_before_native_execution(
    design, tmp_path, mutation
):
    config, source = design
    value = json.loads(config.read_text())
    benchmark_path = Path(value["benchmark_run"])
    benchmark = json.loads(benchmark_path.read_text())
    if mutation == "rtl":
        next((source / "vmod/nvdla/top").glob("rtl_*.v")).write_text("changed RTL")
    elif mutation == "include":
        (source / "vmod/include/config.vh").write_text("changed include")
    elif mutation == "generated":
        (source / benchmark["preparation"]["generated_files"][0]["path"]).write_text(
            "changed generated"
        )
    elif mutation == "benchmark":
        benchmark_path.write_text(benchmark_path.read_text() + "\n")
    elif mutation == "option":
        value["options"]["sources"] = ["different-design.v"]
        write_json(config, value)
    else:
        key = "memory_policy" if mutation == "memory" else "upstream_revision"
        benchmark["preparation"][key] = (
            "black-box" if mutation == "memory" else "unknown-revision"
        )
        write_json(benchmark_path, benchmark)
        value["benchmark_sha256"] = file_sha256(benchmark_path)
        write_json(config, value)
    with patch(
        "emuflow.flow.native_compile.run_native_multi_fpga_flow",
        side_effect=AssertionError("native started"),
    ):
        with pytest.raises(EmuFlowError):
            run.execute_run(config, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_cli_configuration_check_is_explicitly_not_terminal_qualification(
    design, tmp_path
):
    from emuflow.flow.cli import _build_parser, _dispatch

    config, _ = design
    args = _build_parser().parse_args(
        [
            "run",
            str(config),
            "--backend",
            "vivado",
            "--out",
            str(tmp_path / "out"),
            "--check",
        ]
    )
    with (
        patch("emuflow.flow.cli._print_json") as output,
        patch(
            "emuflow.flow.native_compile.run_native_multi_fpga_flow",
            side_effect=AssertionError("design started"),
        ),
    ):
        assert _dispatch(args) == 0
    assert output.call_args.args[0]["terminal_qualification"] is False
    assert not (tmp_path / "out").exists()


def test_profile_rejects_unknown_fields_and_duplicate_keys(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema":"x","schema":"y"}')
    with pytest.raises(EmuFlowError, match="duplicate"):
        env.load_environment(path)


def test_registered_solver_probes_use_the_selected_backend_and_restore_environment(
    profile,
):
    p = env.load_environment(profile)
    from types import SimpleNamespace

    original = os.environ.get("LM_LICENSE_FILE")
    with (
        patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stderr="")),
        patch("emuflow.timing.engine_runtime.check_opensta_engine_version"),
        patch(
            "emuflow.physical.openparf.openparf.validate_openparf_runtime"
        ) as openparf,
        patch.object(env, "_probe_tool", return_value="Vivado v2024.2") as tool,
    ):
        assert env.probe_environment(p, "vivado")["engine_load"] == "pass"
        assert tool.call_args.args[1] == [
            p["backends"]["vivado"]["executable"],
            "-version",
        ]
        openparf.assert_not_called()
    assert os.environ.get("LM_LICENSE_FILE") == original


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_version_probe_uses_real_native_tool_supervision(channel):
    import sys
    from tests.native_build import emuflow_native

    assert "version-fixture" in env._probe_tool(
        str(emuflow_native()),
        [
            sys.executable,
            "-c",
            "import sys; print('version-fixture', file=sys." + channel + ")",
        ],
    )


def test_installed_nvdla_pin_matches_the_fetch_catalog():
    from emuflow.flow.designs.nvdla_contract import CATALOG_PATH

    root = Path(__file__).resolve().parents[1]
    canonical = json.loads((root / "benchmarks/rtl_catalog.json").read_text())
    selected = next(item for item in canonical["designs"] if item["id"] == "nvdla")
    installed = json.loads(CATALOG_PATH.read_text())
    assert installed["designs"] == [selected]


@pytest.fixture
def generic_design(tmp_path):
    """A component fixture for the generic frontend, not a qualification design."""
    root = tmp_path / "user-rtl"
    (root / "rtl").mkdir(parents=True)
    (root / "include").mkdir()
    (root / "rtl/top.sv").write_text(
        "module user_top(input clk, input a, output reg q);\n"
        "always @(posedge clk) q <= a;\nendmodule\n"
    )
    (root / "include/config.vh").write_text("`define WIDTH 1\n")
    value = {
        "schema": prepare.DESIGN_SCHEMA,
        "name": "user-design",
        "source_root": "user-rtl",
        "sources": ["rtl/*.sv"],
        "include_dirs": ["include"],
        "defines": ["WIDTH=1"],
        "top": "user_top",
        "clock_periods_ns": {"clk": 12.5},
        "timing_io": {
            "input_groups": [{"clock": "clk", "delay_ns": 1, "ports": ["a"]}],
            "output_groups": [{"clock": "clk", "delay_ns": 2, "ports": ["q"]}],
        },
        "options": {
            "cut_mode": "static-exact-combinational",
            "phase6_provider": "baseline",
        },
    }
    config = tmp_path / "design.json"
    write_json(config, value)
    return config, root, value


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
def test_direct_cli_run_uses_registered_environment_and_native_controller(
    generic_design, profile, tmp_path, monkeypatch, backend
):
    from emuflow.flow.cli import _build_parser, _dispatch

    _, root, _ = generic_design
    monkeypatch.setenv(env.ENVIRONMENT_VARIABLE, str(profile))
    monkeypatch.chdir(tmp_path)
    arguments = [
        "run",
        str(root / "rtl/top.sv"),
        "--top",
        "user_top",
        "--clock",
        "clk=12.5",
        "--include-dir",
        str(root / "include"),
        "--define",
        "WIDTH=1",
        "--backend",
        backend,
    ]
    with (
        patch("emuflow.flow.cli._print_json") as printed,
        patch(
            "emuflow.flow.native_compile.run_native_multi_fpga_flow",
            return_value={"status": "pass", "summary": {"status": "pass"}},
        ) as native,
    ):
        assert _dispatch(_build_parser().parse_args(arguments)) == 0
    assert printed.call_args.args[0] == {"status": "pass"}
    options = native.call_args.kwargs
    assert options["sources"] == [root / "rtl/top.sv"]
    assert options["include_dirs"] == [root / "include"]
    assert options["defines"] == ["WIDTH=1"]
    assert options["top"] == "user_top"
    assert options["clocks"] == ["clk"]
    assert options["clock_periods"] == {"clk": 12.5}
    assert options["timing_io"] == {"input_groups": [], "output_groups": []}
    assert options["physical"] is True
    assert options["physical_backend"] == backend
    assert options["output_dir"] == (tmp_path / "runs/user_top").resolve()
    assert str(root / "rtl/top.sv") in options["source_checksums"]
    assert str(root / "include/config.vh") in options["source_checksums"]


def test_direct_cli_run_does_not_treat_an_rtl_file_as_a_prepared_config(
    generic_design,
):
    from emuflow.flow.cli import _build_parser, _dispatch

    _, root, _ = generic_design
    args = _build_parser().parse_args(["run", str(root / "rtl/top.sv")])
    with pytest.raises(EmuFlowError, match="--top.*--clock"):
        _dispatch(args)


@pytest.mark.parametrize("backend", ["rapidwright", "vivado"])
def test_generic_cli_prepare_and_run_preserve_user_inputs(
    generic_design, profile, tmp_path, backend, monkeypatch
):
    from emuflow_tools.cli import _build_parser, _dispatch

    config, root, description = generic_design
    monkeypatch.setenv(env.ENVIRONMENT_VARIABLE, str(profile))
    output = tmp_path / "prepared/design.run.json"
    args = _build_parser().parse_args(
        [
            "design",
            "prepare",
            str(config),
            "--out",
            str(output),
        ]
    )
    with patch("emuflow_tools.cli._print_json") as printed:
        assert _dispatch(args) == 0
    assert printed.call_args.args[0]["top"] == "user_top"
    assert printed.call_args.args[0]["source_files"] == 1
    _, value, benchmark, source_root = run.load_run(output)
    assert source_root == root
    assert benchmark.value["design_id"] == "user-design"
    assert benchmark.value["synthesis"]["defines"] == ["WIDTH=1"]
    with patch(
        "emuflow.flow.native_compile.run_native_multi_fpga_flow",
        return_value={"status": "pass"},
    ) as native:
        assert (
            run.execute_run(output, tmp_path / "flow", backend=backend)["status"]
            == "pass"
        )
    kwargs = native.call_args.kwargs
    assert kwargs["sources"] == [root / "rtl/top.sv"]
    assert kwargs["top"] == "user_top"
    assert kwargs["clock_periods"] == {"clk": 12.5}
    assert kwargs["timing_io"] == description["timing_io"]
    assert kwargs["include_dirs"] == [root / "include"]
    assert str(root / "include/config.vh") in kwargs["source_checksums"]
    assert kwargs["physical_backend"] == backend
    assert kwargs["cut_mode"] == "static-exact-combinational"
    assert value["options"]["phase6_provider"] == "baseline"


@pytest.mark.parametrize(
    "change",
    [
        "clock",
        "io",
        "source",
        "escape",
        "unknown",
        "override",
        "option",
        "schema",
        "symlink",
    ],
)
def test_generic_prepare_rejects_invalid_inputs_without_publishing(
    generic_design, profile, tmp_path, change
):
    config, root, value = generic_design
    if change == "clock":
        value["clock_periods_ns"] = {"clk": -1}
    elif change == "io":
        value["timing_io"]["input_groups"][0]["clock"] = "missing"
    elif change == "source":
        value["sources"] = ["absent/*.v"]
    elif change == "escape":
        value["sources"] = ["../design.json"]
    elif change == "unknown":
        value["typo"] = True
    elif change == "override":
        value["options"]["sources"] = ["wrong.v"]
    elif change == "option":
        value["options"]["not_a_compile_option"] = True
    elif change == "schema":
        value["schema"] = "emuflow.design/v999"
    else:
        (root / "include/escape.vh").symlink_to(config)
    write_json(config, value)
    output = tmp_path / "rejected.run.json"
    with pytest.raises(EmuFlowError):
        prepare.prepare_design_run(config, output, environment_path=profile)
    assert not output.exists()
    assert not output.with_name(output.stem + ".benchmark.json").exists()


def test_generic_prepare_rejects_overwrites_and_source_tree_outputs(
    generic_design, profile, tmp_path
):
    config, root, _ = generic_design
    with pytest.raises(EmuFlowError, match="outside"):
        prepare.prepare_design_run(config, root / "run.json", environment_path=profile)
    output = tmp_path / "ready.json"
    prepare.prepare_design_run(config, output, environment_path=profile)
    before = output.read_bytes()
    with pytest.raises(EmuFlowError, match="new run"):
        prepare.prepare_design_run(config, output, environment_path=profile)
    assert output.read_bytes() == before
    (root / "include/config.vh").write_text("changed\n")
    with pytest.raises(EmuFlowError, match="bytes differ"):
        run.load_run(output)


def test_generic_template_is_a_valid_design_description(profile, tmp_path):
    template = (
        Path(__file__).resolve().parents[1] / "examples/designs/design.example.json"
    )
    value = json.loads(template.read_text())
    root = tmp_path / "example"
    (root / "rtl/blocks").mkdir(parents=True)
    (root / "include").mkdir()
    (root / "rtl/top.sv").write_text("module top; endmodule\n")
    (root / "rtl/blocks/block.sv").write_text("module block; endmodule\n")
    value["source_root"] = str(root)
    config = tmp_path / "template.json"
    write_json(config, value)
    report = prepare.prepare_design_run(
        config, tmp_path / "template.run.json", environment_path=profile
    )
    assert report["source_files"] == 2
    assert report["terminal_qualification"] is False
