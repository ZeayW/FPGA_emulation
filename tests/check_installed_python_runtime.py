"""Check root-build package contents and defaults without a source-tree import."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            result.update(chunk)
    return result.hexdigest()


def main(source_root: Path, install_root: Path) -> None:
    source = source_root / "src/python/emuflow"
    package = install_root / "lib/emuflow"
    expected = {p.relative_to(source) for p in source.rglob("*.py")}
    actual = {p.relative_to(package) for p in package.rglob("*.py")}
    if not expected or actual != expected:
        raise AssertionError(f"installed module inventory differs: missing={sorted(expected-actual)}, stale={sorted(actual-expected)}")
    for name in sorted(expected):
        assert digest(package / name) == digest(source / name), f"installed module changed: {name}"
    assert not (install_root / "share/emuflow/benchmarks/experiment_dag.schema.example.json").exists(), "retired DAG example remains installed"
    # The package import is forced to the installed prefix even when CTest
    # inherits a developer PYTHONPATH or the repository has an editable install.
    sys.path.insert(0, str(install_root / "lib"))
    import emuflow
    from emuflow.platform.default_platform import DEFAULT_PLATFORM_PATH
    from emuflow.platform.platform import Platform
    from emuflow.physical.xilinx_primitives import (
        DEFAULT_XILINX_PRIMITIVE_LIBRARY, load_xilinx_primitive_library,
    )
    from emuflow.timing.engine_runtime import OPENSTA_RUNTIME_CHECK_SCRIPT
    assert Path(emuflow.__file__).resolve().parent == package.resolve()
    for retired in ("cli", "phase3", "native_compile_adapter", "xilinx_rwroute", "frontend.yosys", "frontend.synthesis", "physical.xilinx_physical_macros", "physical.xilinx_packing", "scheduling.timing_routing", "scheduling.frame_search", "flow.multi_fpga_flow", "transport.runtime", "physical.physical_backend", "physical.vivado"):
        assert importlib.util.find_spec("emuflow." + retired) is None, retired
    for retired in ('partition.phase3', 'partition.phase3_native', 'routing.phase4', 'routing.phase4_native', 'scheduling.phase5', 'scheduling.phase5_native', 'transport.phase6', 'transport.phase6_native', 'timing.phase7c', 'flow.multi_fpga_physical_flow',):
        assert importlib.util.find_spec("emuflow." + retired) is None, retired
    assert importlib.util.find_spec("emuflow.timing.opensta") is None
    for retired in ("flow.designs.nvdla", "flow.designs.nvdla_memory"):
        assert importlib.util.find_spec("emuflow." + retired) is None, retired
    data = [p for p in (source / "data").rglob("*") if p.suffix in {".json", ".sql", ".tcl", ".v"}]
    for path in data:
        installed = package / path.relative_to(source)
        assert installed.is_file(), f"installed runtime data missing: {installed}"
        assert digest(installed) == digest(path), f"installed runtime data changed: {installed}"
    adapters = list((source_root / "scripts/vivado").glob("*.tcl"))
    assert {p.name for p in adapters} == {
        p.name for p in (install_root / "scripts/vivado").glob("*.tcl")
    }, "installed Vivado adapter inventory differs from current source"
    adapters += list((source_root / "scripts/opensta").glob("*.tcl"))
    adapters += [source_root / "scripts" / tool / "container_runtime.sh"
                 for tool in ("yosys", "opensta", "openroad", "patron")]
    adapters += list((source_root / "platforms/virtual").glob("*.wires.csv"))
    for path in adapters:
        relative = path.relative_to(source_root)
        # OpenSTA's complete script directory has one owner under share;
        # its launcher resolves the root Yosys launcher from that location.
        prefix = (install_root / "share/emuflow"
                  if relative.parts[:2] == ("scripts", "opensta") else install_root)
        installed = prefix / relative
        assert installed.is_file(), f"installed adapter resource missing: {installed}"
        assert digest(installed) == digest(path), f"installed adapter resource changed: {installed}"
    assert digest(install_root / "share/emuflow/scripts/rapidwright/EmuFlowRWRoute.java") == digest(source_root / "scripts/rapidwright/EmuFlowRWRoute.java")
    assert not (install_root / "scripts/yosys/nvdla_compat.v").exists()
    from emuflow.flow.cli import _build_parser
    import emuflow.transport.phase6_oracle
    assert "emuflow_tools" not in sys.modules, "installed runtime imported optional tools"
    assert importlib.util.find_spec("emuflow.common.io") is None
    from emuflow.ir import emuir_compact, emuir_storage
    from emuflow.ir.ir import EmuIR
    assert not hasattr(EmuIR, "save")
    assert not hasattr(emuir_compact, "write_compact_emuir")
    assert not hasattr(emuir_storage, "write_emuir_sqlite")
    assert not hasattr(emuir_storage, "_write_emuir_sqlite_v1")
    _build_parser().parse_args(["run", "top.sv", "--top", "top", "--clock", "clk=10"])
    _build_parser().parse_args(["run", "design.run.json", "--out", "unused", "--backend", "vivado"])
    from emuflow.flow.designs.nvdla_contract import CATALOG_PATH, COMPAT_PATH
    assert CATALOG_PATH.is_file() and COMPAT_PATH.is_file()
    assert OPENSTA_RUNTIME_CHECK_SCRIPT.resolve() == (
        install_root / "share/emuflow/scripts/opensta/validate_runtime.tcl"
    ).resolve()
    assert (install_root / "scripts/vivado/implement_indexed_partition.tcl").is_file()
    _, primitives = load_xilinx_primitive_library()
    platform = Platform.load(DEFAULT_PLATFORM_PATH)
    assert DEFAULT_XILINX_PRIMITIVE_LIBRARY.parent.parent == package / "data"
    assert len(platform.fpgas) == 4
    assert {"LUT6", "FDRE", "CARRY8", "DSP48E2", "RAMB36E2", "URAM288"} <= set(primitives["cells"])
    print(json.dumps({"status": "pass", "scope": "installed Python/default resource contract",
                      "python_modules": len(actual), "runtime_data_files": len(data),
                      "adapter_resource_files": len(adapters),
                      "primitive_types": len(primitives["cells"]), "default_fpgas": len(platform.fpgas)}))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
