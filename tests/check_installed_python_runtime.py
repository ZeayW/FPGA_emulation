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
    # The package import is forced to the installed prefix even when CTest
    # inherits a developer PYTHONPATH or the repository has an editable install.
    sys.path.insert(0, str(install_root / "lib"))
    import emuflow
    from emuflow.default_platform import DEFAULT_PLATFORM_PATH
    from emuflow.platform import Platform
    from emuflow.xilinx_primitives import (
        DEFAULT_XILINX_PRIMITIVE_LIBRARY, load_xilinx_primitive_library,
    )
    assert Path(emuflow.__file__).resolve().parent == package.resolve()
    data = [p for p in (source / "data").rglob("*") if p.suffix in {".json", ".sql"}]
    for path in data:
        installed = package / path.relative_to(source)
        assert installed.is_file(), f"installed runtime data missing: {installed}"
        assert digest(installed) == digest(path), f"installed runtime data changed: {installed}"
    _, primitives = load_xilinx_primitive_library()
    platform = Platform.load(DEFAULT_PLATFORM_PATH)
    assert DEFAULT_XILINX_PRIMITIVE_LIBRARY.parent.parent == package / "data"
    assert len(platform.fpgas) == 4
    assert {"LUT6", "FDRE", "CARRY8", "DSP48E2", "RAMB36E2", "URAM288"} <= set(primitives["cells"])
    print(json.dumps({"status": "pass", "scope": "installed Python/default resource contract",
                      "python_modules": len(actual), "runtime_data_files": len(data),
                      "primitive_types": len(primitives["cells"]), "default_fpgas": len(platform.fpgas)}))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
