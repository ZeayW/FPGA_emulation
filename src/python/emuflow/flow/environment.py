"""Installation-owned tool and device data for the native controller."""

from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path

from emuflow.common.errors import EmuFlowError
from emuflow.common.io import file_sha256
from emuflow.timing.xilinx_preplacement_timing import (
    validate_xilinx_preplacement_timing_db,
    RAPIDWRIGHT_TIMING_DATA_SHA256,
)

SCHEMA = "emuflow.environment/v1"
ENVIRONMENT_VARIABLE = "EMUFLOW_ENVIRONMENT"
_LICENSE_ENV = {
    "LM_LICENSE_FILE",
    "XILINXD_LICENSE_FILE",
    "LD_LIBRARY_PATH",
    "EMUFLOW_OPENPARF_RUNTIME_LIBRARY_PATH",
}
_BACKEND_KEYS = {
    "rapidwright": {
        "jar",
        "java",
        "device_data",
        "openparf_install",
        "openparf_python",
        "java_source",
    },
    "vivado": {"executable"},
}
_PATH_KEYS = {
    "native_root",
    "timing_data_dir",
    "architecture",
    "native_constraints",
    "provider_manifest",
}


def default_environment_path():
    """Return the installation-owned runtime manifest.

    The launcher sets EMUFLOW_NATIVE_ROOT.  An explicit variable remains only
    for managed/developer installations and tests.
    """
    configured = os.environ.get(ENVIRONMENT_VARIABLE)
    if configured:
        return Path(configured).expanduser().resolve()
    native_root = os.environ.get("EMUFLOW_NATIVE_ROOT")
    if native_root:
        return (
            Path(native_root).expanduser().resolve()
            / "share/emuflow/environment.json"
        )
    raise EmuFlowError(
        "EmuFlow was not launched from a complete installation; run ./install.sh"
    )


def resolve_environment_path(path=None):
    return (
        Path(path).expanduser().resolve()
        if path is not None
        else default_environment_path()
    )


def read_control(path):
    path = Path(path).expanduser().resolve()
    if path.stat().st_size > 1024 * 1024:
        raise EmuFlowError(f"configuration exceeds bound: {path}")

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EmuFlowError(f"duplicate configuration key: {key}")
            result[key] = value
        return result

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=unique,
        parse_constant=lambda token: (_ for _ in ()).throw(
            EmuFlowError("non-finite configuration number")
        ),
    )
    if type(value) is not dict:
        raise EmuFlowError("configuration must be an object")
    return path, value


def resolve_path(base, value):
    if type(value) is not str or not value:
        raise EmuFlowError("configuration paths must be nonempty strings")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_environment(path):
    path, value = read_control(path)
    allowed = {"schema", *_PATH_KEYS, "backends", "environment", "preplacement_timing"}
    if value.get("schema") != SCHEMA or set(value) - allowed:
        raise EmuFlowError("unsupported environment schema or fields")
    result = {"schema": SCHEMA}
    for key in _PATH_KEYS:
        if key in value:
            result[key] = str(resolve_path(path.parent, value[key]))
    backends = value.get("backends", {})
    if type(backends) is not dict or not backends or set(backends) - set(_BACKEND_KEYS):
        raise EmuFlowError("installation must contain rapidwright and/or vivado")
    result["backends"] = {}
    for name, settings in backends.items():
        if type(settings) is not dict or set(settings) - _BACKEND_KEYS[name]:
            raise EmuFlowError(f"unsupported {name} environment fields")
        result["backends"][name] = {
            key: str(resolve_path(path.parent, item)) for key, item in settings.items()
        }
    environment = value.get("environment", {})
    if (
        type(environment) is not dict
        or set(environment) - _LICENSE_ENV
        or any(type(v) is not str or "\0" in v for v in environment.values())
    ):
        raise EmuFlowError(
            "environment accepts only license/library variables with string values"
        )
    result["environment"] = environment
    if "preplacement_timing" in value:
        timing = value["preplacement_timing"]
        if type(timing) is not dict or set(timing) != {"path", "sha256"}:
            raise EmuFlowError("invalid prepared pre-placement timing reference")
        result["preplacement_timing"] = {
            "path": str(resolve_path(path.parent, timing["path"])),
            "sha256": timing["sha256"],
        }
    return result


@contextmanager
def environment_scope(profile):
    updates = {
        **profile.get("environment", {}),
        "EMUFLOW_NATIVE_ROOT": profile["native_root"],
    }
    previous = {key: os.environ.get(key) for key in updates}
    try:
        os.environ.update(updates)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def inspect_environment(profile, backend=None, *, prepared=True):
    """Report the complete missing inventory before launching an EDA process."""
    selected = [backend] if backend is not None else sorted(profile["backends"])
    issues = []

    def require(label, path, kind="file"):
        p = Path(path) if path else None
        okay = p is not None and (p.is_dir() if kind == "directory" else p.is_file())
        if kind == "executable":
            okay = okay and os.access(p, os.X_OK)
        if not okay:
            issues.append(
                {"item": label, "path": str(p) if p else None, "required": kind}
            )

    require("native_root", profile.get("native_root"), "directory")
    root = Path(profile.get("native_root", "/unconfigured"))
    for name in ("emuflow-native", "yosys", "sta", "openroad"):
        require(name, root / "bin" / name, "executable")
    require("timing_data_dir", profile.get("timing_data_dir"), "directory")
    if profile.get("timing_data_dir"):
        for name, digest in RAPIDWRIGHT_TIMING_DATA_SHA256.items():
            source = Path(profile["timing_data_dir"]) / name
            require("timing_data." + name, source)
            if source.is_file() and file_sha256(source) != digest:
                issues.append(
                    {
                        "item": "timing_data." + name,
                        "required": "pinned source checksum",
                    }
                )
    for key in ("architecture", "native_constraints", "provider_manifest"):
        require(key, profile.get(key))
    for name in selected:
        settings = profile["backends"].get(name)
        if settings is None:
            issues.append({"item": name, "required": "installed backend"})
            continue
        keys = _BACKEND_KEYS[name] - (
            {"java_source"} if name == "rapidwright" else set()
        )
        for key in sorted(keys):
            kind = (
                "directory"
                if key in {"device_data", "openparf_install"}
                else (
                    "executable"
                    if key in {"java", "openparf_python", "executable"}
                    else "file"
                )
            )
            require(name + "." + key, settings.get(key), kind)
        if name == "rapidwright":
            source = settings.get(
                "java_source",
                str(root / "share/emuflow/scripts/rapidwright/EmuFlowRWRoute.java"),
            )
            require("rapidwright.java_source", source)
            if settings.get("java"):
                require(
                    "rapidwright.javac",
                    Path(settings["java"]).with_name("javac"),
                    "executable",
                )
    if prepared:
        timing = profile.get("preplacement_timing", {})
        require("preplacement_timing", timing.get("path"))
        if timing.get("path") and Path(timing["path"]).is_file():
            if file_sha256(Path(timing["path"])) != timing.get("sha256"):
                issues.append(
                    {
                        "item": "preplacement_timing",
                        "required": "unchanged prepared timing source",
                    }
                )
            else:
                validate_xilinx_preplacement_timing_db(Path(timing["path"]))
    return {
        "schema": "emuflow.environment-check/v1",
        "status": "fail" if issues else "pass",
        "backends": selected,
        "missing": issues,
    }


def require_environment(profile, backend=None, *, prepared=True):
    report = inspect_environment(profile, backend, prepared=prepared)
    if report["missing"]:
        raise EmuFlowError(
            "environment is incomplete: "
            + ", ".join(item["item"] for item in report["missing"])
        )
    return report


def _probe_tool(native, arguments):
    from emuflow.common.tool_runtime import NativeToolProcess

    with NativeToolProcess(
        native, arguments, environment=dict(os.environ), timeout_ms=30000
    ) as process:
        result = process.finish()
    if result.returncode:
        raise EmuFlowError(
            "installed engine version probe failed: " + result.stdout[-4096:]
        )
    return result.stdout


def probe_environment(profile, backend=None):
    """Load the installed native libraries and selected solver host before use."""
    import subprocess
    from emuflow.timing.opensta import check_opensta_engine_version
    from emuflow.physical.openparf.openparf import validate_openparf_runtime

    report = require_environment(profile, backend)
    with environment_scope(profile):
        executable = str(Path(profile["native_root"]) / "bin/emuflow-native")
        try:
            process = subprocess.run(
                [executable, "check-runtime"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EmuFlowError("installed native runtime cannot load") from error
        if process.returncode:
            raise EmuFlowError(
                "native runtime rejected its installation: "
                + process.stderr[-4096:]
            )
        check_opensta_engine_version(str(Path(profile["native_root"]) / "bin/sta"))
        for name in report["backends"]:
            if name == "vivado":
                identity = _probe_tool(
                    executable, [profile["backends"][name]["executable"], "-version"]
                )
                if "Vivado" not in identity:
                    raise EmuFlowError(
                        "installed Vivado executable did not identify itself"
                    )
            if name == "rapidwright":
                settings = profile["backends"][name]
                identity = _probe_tool(executable, [settings["java"], "-version"])
                if "version" not in identity.lower():
                    raise EmuFlowError(
                        "installed Java executable did not identify itself"
                    )
                validate_openparf_runtime(
                    install_root=Path(settings["openparf_install"]),
                    python_executable=Path(settings["openparf_python"]),
                )
    return {
        **report,
        "engine_load": "pass",
        "license": "inherited-or-profile-environment;vendor-enforces-at-execution",
    }


def compile_environment(profile, backend):
    require_environment(profile, backend)
    settings = profile["backends"][backend]
    root = Path(profile["native_root"])
    options = {
        "architecture_timing_db": Path(profile["preplacement_timing"]["path"]),
        "physical_architecture": Path(profile["architecture"]),
        "global_sta_executable": str(root / "bin/sta"),
        "physical_rapidwright_native_constraints": Path(profile["native_constraints"]),
        "physical_rapidwright_provider_manifest": Path(profile["provider_manifest"]),
    }
    if backend == "vivado":
        options["physical_vivado"] = settings["executable"]
    else:
        options.update(
            physical_rapidwright_jar=Path(settings["jar"]),
            physical_rapidwright_java=Path(settings["java"]),
            physical_rapidwright_device_data=Path(settings["device_data"]),
            physical_rapidwright_timing_data=Path(profile["timing_data_dir"]),
            physical_rapidwright_java_source=Path(
                settings.get(
                    "java_source",
                    root / "share/emuflow/scripts/rapidwright/EmuFlowRWRoute.java",
                )
            ),
            physical_rapidwright_opensta=str(root / "bin/sta"),
            physical_openparf_install=Path(settings["openparf_install"]),
            physical_openparf_python=Path(settings["openparf_python"]),
        )
    return options
