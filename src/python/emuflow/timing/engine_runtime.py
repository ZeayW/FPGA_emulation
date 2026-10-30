"""Finite OpenSTA engine identity and installed resource locations."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Dict

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256
from emuflow.common.paths import REPOSITORY_ROOT
from emuflow.common.process_output import run_with_bounded_output

FPGA_TIMING_MODEL_SCHEMA = "emuflow.fpga-timing-model/v1"

FPGA_TIMING_MODEL_SCHEMA_V2 = "emuflow.fpga-timing-model/v2"

OPENSTA_PROVIDER = "opensta-fpga-path-database-v1"

OPENSTA_THROUGH_COVERAGE_SCHEMA = "emuflow.opensta-through-net-coverage/v1"

MINIMUM_OPENSTA_VERSION = (3, 1, 0)

def check_opensta_engine_version(executable: str) -> str:
    """Fail closed on a legacy engine without hashing the executable.

    One-shot flows use this lightweight probe before synthesis so an invalid
    runtime cannot waste the expensive frontend.  The authoritative provider
    invocation still calls :func:`require_opensta_engine` once, where the
    executable is content-sealed for retained evidence.
    """
    version_result = run_with_bounded_output([executable, "-version"])
    version_match = re.search(
        r"(?:^|\s)(\d+)\.(\d+)\.(\d+)(?:\s|$)",
        version_result.stdout,
    )
    if version_result.returncode != 0 or version_match is None:
        raise ValidationError(
            "OpenSTA executable does not report a semantic version"
        )
    version_tuple = tuple(int(field) for field in version_match.groups())
    if version_tuple < MINIMUM_OPENSTA_VERSION:
        minimum = ".".join(str(field) for field in MINIMUM_OPENSTA_VERSION)
        actual = ".".join(str(field) for field in version_tuple)
        raise ValidationError(
            f"OpenSTA {actual} is unsupported; EmuFlow requires {minimum} or newer"
        )
    runtime_result = run_with_bounded_output(
        [executable, "-no_init", "-exit", str(OPENSTA_RUNTIME_CHECK_SCRIPT)]
    )
    if (runtime_result.returncode != 0 or
            "EMUFLOW_OPENSTA_RUNTIME status=pass" not in runtime_result.stdout):
        raise ValidationError(
            "OpenSTA runtime qualification failed; Tcl 8.6+ with try/finally "
            "is required: " + runtime_result.stdout[-1000:]
        )
    return ".".join(str(field) for field in version_tuple)

def require_opensta_engine(executable: str) -> Dict[str, str]:
    """Validate and content-seal the OpenSTA engine used for evidence."""
    version = check_opensta_engine_version(executable)
    selected = Path(executable).resolve()
    selected_digest = file_sha256(selected)
    identity = {
        "name": "OpenSTA",
        "version": version,
        "executable_sha256": selected_digest,
    }
    launcher = _runtime_data_path(Path("scripts/opensta/container_runtime.sh")).resolve()
    # CMake and wheel installs can expose identical repository launchers at
    # different paths. Recognize their exact bytes, never an ambient variable
    # alone or a merely similar shell-script filename.
    launcher_digest = selected_digest if selected == launcher else (
        file_sha256(launcher) if launcher.is_file() else None
    )
    if selected == launcher or selected_digest == launcher_digest:
        binding = os.environ.get("EMUFLOW_OPENSTA_CONTAINER_BINARY")
        if not binding:
            raise ValidationError("OpenSTA container engine identity lacks an explicit binary")
        binary = Path(binding).expanduser().resolve()
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValidationError("OpenSTA container engine identity is not executable")
        identity["executable_sha256"] = file_sha256(binary)
        identity["launcher_sha256"] = selected_digest
    return identity

def _runtime_data_path(relative: Path) -> Path:
    root = REPOSITORY_ROOT
    candidates = (
        root / relative,
        root / "share" / "emuflow" / relative,
        Path(sys.prefix) / "share" / "emuflow" / relative,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]

DEFAULT_TIMING_MODEL = _runtime_data_path(
    Path("resources/timing/ultrascaleplus-softlogic-v1.json")
)

OPENSTA_EXPORT_SCRIPT = _runtime_data_path(
    Path("scripts/opensta/export_timing_path_database.tcl")
)

OPENSTA_RUNTIME_CHECK_SCRIPT = _runtime_data_path(
    Path("scripts/opensta/validate_runtime.tcl")
)
