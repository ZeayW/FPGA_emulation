"""Check root-build package contents and defaults without a source-tree import."""
from __future__ import annotations

import hashlib
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
    source = source_root / "src/emuflow"
    package = install_root / "lib/emuflow"
    expected = {p.name for p in source.glob("*.py")}
    actual = {p.name for p in package.glob("*.py")}
    if not expected or actual != expected:
        raise AssertionError(f"installed module inventory differs: missing={sorted(expected-actual)}, stale={sorted(actual-expected)}")
    for name in sorted(expected):
        assert digest(package / name) == digest(source / name), f"installed module changed: {name}"
    # The package import is forced to the installed prefix even when CTest
    # inherits a developer PYTHONPATH or the repository has an editable install.
    sys.path.insert(0, str(install_root / "lib"))
    import emuflow
    from emuflow.default_platform import DEFAULT_PLATFORM_PATH
    from emuflow.platform import Platform
    from emuflow.xilinx_primitives import (
        DEFAULT_XILINX_PRIMITIVE_LIBRARY, load_xilinx_primitive_library,
    )
    from emuflow.opensta import OPENSTA_RUNTIME_CHECK_SCRIPT
    from emuflow.vivado_physical import _vivado_script
    assert Path(emuflow.__file__).resolve().parent == package.resolve()
    data = [p for p in (source / "data").rglob("*") if p.suffix in {".json", ".sql"}]
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
    assert OPENSTA_RUNTIME_CHECK_SCRIPT.resolve() == (
        install_root / "share/emuflow/scripts/opensta/validate_runtime.tcl"
    ).resolve()
    assert _vivado_script("implement_indexed_partition.tcl").resolve() == (
        install_root / "scripts/vivado/implement_indexed_partition.tcl"
    ).resolve()
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
