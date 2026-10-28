from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Optional

from emuflow.common.errors import EmuFlowError
from emuflow.common.native_tools import native_install_roots


def resolve_openparf_install(
    explicit: Optional[Path] = None,
) -> Path:
    """Resolve only an explicit or root-build OpenPARF installation."""
    if explicit is not None:
        root = explicit.expanduser()
        # Accept both the OpenPARF package directory itself and the monorepo
        # CMake install prefix that contains it.  The CLI option is named
        # --openparf-install, so both layouts are natural and unambiguous.
        candidates = (root, root / "openparf")
    else:
        candidates = tuple(root / "openparf" for root in native_install_roots())
    for candidate in candidates:
        resolved = candidate.resolve()
        if (
            (resolved / "openparf.py").is_file()
            and (resolved / "openparf").is_dir()
        ):
            return resolved
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise EmuFlowError(
        "in-tree OpenPARF build product was not found; searched: "
        f"{searched}. Build the monorepo with `cmake --preset release && "
        "cmake --build --preset release`."
    )


def _openparf_environment(
    installation: Path,
    python_executable: Optional[Path] = None,
) -> Dict[str, str]:
    environment = os.environ.copy()
    python_path = str(installation)
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment["PYTHONPATH"] = python_path
    configured_runtime = [
        item
        for item in environment.get(
            "EMUFLOW_OPENPARF_LIBRARY_PATH", ""
        ).split(os.pathsep)
        if item
    ]
    if python_executable is not None:
        # OpenPARF is a compiled PyTorch extension and must load the Torch ABI
        # belonging to the interpreter used when it was built.  A coordinator
        # may itself run from a different Conda environment, whose inherited
        # LD_LIBRARY_PATH would otherwise win before the child interpreter's
        # own environment and load an incompatible libtorch_python.so.
        python_prefix = python_executable.expanduser().absolute().parent.parent
        prefix_lib = python_prefix / "lib"
        torch_libs = sorted(
            path for path in prefix_lib.glob(
                "python*/site-packages/torch/lib"
            ) if path.is_dir()
        )
        inherited = [
            item for item in environment.get(
                "LD_LIBRARY_PATH", ""
            ).split(os.pathsep) if item
        ]

        def is_foreign_torch_library(item: str) -> bool:
            path = Path(item)
            return (
                path.name == "lib"
                and path.parent.name == "torch"
                and "site-packages" in path.parts
                and not any(path == own for own in torch_libs)
            )

        # A build may require a newer C++ runtime than the Conda interpreter
        # ships.  Let the launcher provide that runtime explicitly without
        # baking a machine-specific GCC path into the repository.  It must
        # precede ``prefix_lib`` so the dynamic loader does not select Conda's
        # older libstdc++ first.
        paths = [
            *configured_runtime,
            str(prefix_lib),
            *(str(path) for path in torch_libs),
        ]
        paths.extend(
            item for item in inherited
            if not is_foreign_torch_library(item)
        )
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(
            dict.fromkeys(paths)
        )
    return environment


def _runtime_location(install_root, python_executable):
    installation = resolve_openparf_install(install_root)
    # Preserve the virtual-environment launcher rather than its symlink target.
    python = Path(python_executable if python_executable is not None else
                  os.environ.get("EMUFLOW_OPENPARF_PYTHON", sys.executable)).expanduser().absolute()
    if not python.is_file():
        raise EmuFlowError(f"OpenPARF Python interpreter does not exist: {python}")
    environment = _openparf_environment(installation, python)
    return installation, python, {key: environment[key] for key in ("PYTHONPATH", "LD_LIBRARY_PATH") if key in environment}


def validate_openparf_runtime(*, install_root=None, python_executable=None):
    from tempfile import TemporaryDirectory
    from emuflow.physical.openparf.runtime_native import invoke_runtime
    installation, python, environment = _runtime_location(install_root, python_executable)
    with TemporaryDirectory(prefix="openparf-preflight-") as temporary:
        return invoke_runtime("preflight", Path(temporary)/"runtime.json", python=python,
                              installation=installation, environment=environment)["runtime"]
