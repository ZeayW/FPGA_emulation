from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

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


def validate_openparf_runtime(
    *,
    install_root: Optional[Path] = None,
    python_executable: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fail before physical work when the configured OpenPARF runtime is unusable."""

    installation = resolve_openparf_install(install_root)
    python = (
        python_executable
        if python_executable is not None
        else Path(os.environ.get("EMUFLOW_OPENPARF_PYTHON", sys.executable))
    ).expanduser()
    if not python.is_file():
        raise EmuFlowError(
            f"OpenPARF Python interpreter does not exist: {python}"
        )
    try:
        completed = subprocess.run(
            [
                str(python),
                "-c",
                "import torch; from openparf.flow import place, route",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=120,
            env=_openparf_environment(installation, python),
        )
    except subprocess.TimeoutExpired as error:
        raise EmuFlowError(
            "OpenPARF runtime preflight exceeded 120 seconds"
        ) from error
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-40:])
        raise EmuFlowError(
            "OpenPARF runtime preflight failed before physical execution"
            + (f":\n{tail}" if tail else "")
        )
    return {
        "status": "pass",
        "installation": str(installation),
        "python": str(python),
    }


def run_openparf(
    config_path: Path,
    *,
    log_path: Optional[Path] = None,
    install_root: Optional[Path] = None,
    python_executable: Optional[Path] = None,
    allow_detailed_legalization_handoff: bool = False,
) -> Path:
    """Run OpenPARF built from this repository and return its .pl output."""
    config_path = config_path.resolve()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EmuFlowError(
            f"cannot load OpenPARF configuration {config_path}: {error}"
        ) from error
    benchmark = config.get("benchmark_name")
    result_dir = config.get("result_dir")
    if (
        not isinstance(benchmark, str)
        or not benchmark
        or not isinstance(result_dir, str)
        or not result_dir
    ):
        raise EmuFlowError(
            "OpenPARF configuration requires benchmark_name and result_dir"
        )
    installation = resolve_openparf_install(install_root)
    python = (
        python_executable
        if python_executable is not None
        else Path(
            os.environ.get("EMUFLOW_OPENPARF_PYTHON", sys.executable)
        )
    ).expanduser()
    if not python.is_file():
        raise EmuFlowError(
            f"OpenPARF Python interpreter does not exist: {python}"
        )
    if log_path is None:
        log_path = config_path.with_suffix(".log")
    log_path = log_path.resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    console_log_path = log_path.with_name(
        f"{log_path.stem}.console{log_path.suffix}"
    )
    environment = _openparf_environment(installation, python)
    if "emuflow_continuous_global_guidance" in config:
        raise EmuFlowError("continuous guidance placement is retired; use stable native placement")
    stable_native = config.get("emuflow_stable_global_placement", False)
    if not isinstance(stable_native, bool):
        raise EmuFlowError(
            "emuflow_stable_global_placement must be a boolean"
        )
    command = (
        [str(python.absolute()), "-m", "emuflow.physical.openparf.openparf_native_driver"]
        if stable_native else [str(python.absolute()), str(installation / "openparf.py")]
    )
    with console_log_path.open("w", encoding="utf-8") as console_log:
        completed = subprocess.run(
            [
                # Preserve a virtual-environment launcher path. Resolving its
                # symlink to /usr/bin/python bypasses pyvenv.cfg and silently
                # loses the PyTorch environment used to build OpenPARF.
                *command,
                "--config",
                str(config_path),
                "--log",
                str(log_path),
            ],
            cwd=config_path.parent,
            env=environment,
            stdout=console_log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        try:
            lines = console_log_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            detail = "\n".join(lines[-40:])
        except OSError:
            detail = f"see {console_log_path}"
        raise EmuFlowError(
            "in-tree OpenPARF failed with exit code "
            f"{completed.returncode}: {detail}"
        )
    placement = Path(result_dir) / f"{benchmark}.pl"
    if not placement.is_file():
        raise EmuFlowError(
            "in-tree OpenPARF completed without expected placement: "
            f"{placement}"
        )
    if stable_native:
        from emuflow.physical.openparf.openparf_native_driver import native_metrics_path, validate_openparf_native_metrics

        try:
            validate_openparf_native_metrics(
                native_metrics_path(placement),
                allow_detailed_legalization_handoff=(
                    allow_detailed_legalization_handoff
                ),
            )
        except RuntimeError as error:
            raise EmuFlowError(str(error)) from error
    return placement.resolve()
