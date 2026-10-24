"""Build first-party native test tools once per Python test process."""

from functools import lru_cache
from pathlib import Path
import re
import sys
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
_BUILD_ROOT = tempfile.TemporaryDirectory(prefix="emuflow-native-tests-")


@lru_cache(maxsize=None)
def _standalone_fixture_program(name: str) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for native fixture tools")
    executable = Path(_BUILD_ROOT.name) / ("emuflow_" + name)
    subprocess.run(
        [compiler, "-std=c++17", "-O2", "-pthread",
         str(ROOT / "src/native" / (name + ".cpp")), "-o", str(executable)],
        check=True,
    )
    return executable


def chimew_bank_channel_assigner() -> Path:
    return _standalone_fixture_program("chimew_bank_channel_assigner")


def tlr_router() -> Path:
    """Routing fixtures use the unified stage entry and shared core."""
    return emuflow_native()


@lru_cache(maxsize=1)
def static_exact_analyzer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for Static Exact tests"
        )
    root = Path(_BUILD_ROOT.name)
    executable = root / "bin" / "emuflow_static_exact_analyzer"
    executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/static_exact_analyzer.cpp"),
            str(ROOT / "src/native/emuflow_core/store.cpp"),
            str(ROOT / "src/native/emuflow_core/emuir_view.cpp"),
            str(ROOT / "src/native/emuflow_core/stage_runtime.cpp"),
            str(ROOT / "src/native/emuflow_core/artifact_io.cpp"),
            str(ROOT / "src/native/emuflow_core/sqlite.cpp"),
            "-lsqlite3",
            "-I",
            str(ROOT / "src/native"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    # Compact native semantic analysis now also owns candidate production in
    # the unified core. Model the installed sibling dependency in this fixture.
    unified = executable.parent / "emuflow-native"
    if not unified.exists():
        unified.symlink_to(emuflow_native())
    return executable


_NATIVE_STAGE_SOURCES = tuple(
    line.strip() for line in (ROOT / "src/native/emuflow_core/sources.txt").read_text().splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
if (len(set(_NATIVE_STAGE_SOURCES)) != len(_NATIVE_STAGE_SOURCES) or
        any(not re.fullmatch(r"(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.cpp", name)
            or not (ROOT / "src/native" / name).is_file() for name in _NATIVE_STAGE_SOURCES)):
    raise RuntimeError("invalid shared native source manifest")

def _build_native_stage_program(entry: Path, executable: Path) -> Path:
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for native API tests")
    executable.parent.mkdir(parents=True, exist_ok=True)
    generated = Path(_BUILD_ROOT.name) / "generated"
    subprocess.run([sys.executable, str(ROOT / 'scripts/generate_assignment_schema_header.py'),
        str(generated / 'emuflow_core/assignment_schema.hpp')], check=True)
    subprocess.run(
        [compiler, "-std=c++17", "-O2", "-pthread",
         "-DEMUFLOW_NATIVE_STAGE_LIBRARY=1", str(entry),
         *(str(ROOT / "src/native" / source) for source in _NATIVE_STAGE_SOURCES),
         "-I", str(ROOT / "src/native"), "-I", str(generated), "-lsqlite3", "-o", str(executable)],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def emuflow_native() -> Path:
    return _build_native_stage_program(
        ROOT / "src/native/emuflow_native.cpp",
        Path(_BUILD_ROOT.name) / "unified/emuflow-native",
    )


@lru_cache(maxsize=1)
def patron_ingress_oracle() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_patron_ingress_test.cpp",
        Path(_BUILD_ROOT.name) / "native_patron_ingress_test",
    )


@lru_cache(maxsize=1)
def native_json_record() -> Path:
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for JSON record tests")
    executable = Path(_BUILD_ROOT.name) / "native-json-record"
    subprocess.run([compiler, "-std=c++17", "-O2", str(ROOT / "tests/native_json_record_test.cpp"),
                    "-I", str(ROOT / "src/native"), "-o", str(executable)], check=True)
    return executable


@lru_cache(maxsize=1)
def native_pipeline_api_test() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_pipeline_api_test.cpp",
        Path(_BUILD_ROOT.name) / "native-pipeline-api-test",
    )


@lru_cache(maxsize=1)
def hop_partition_refiner() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for partition-hop tests"
        )
    executable = Path(_BUILD_ROOT.name) / "emuflow_hop_partition_refiner"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/hop_partition_refiner.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def eda2025_topology_optimizer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for topology tests")
    executable = Path(_BUILD_ROOT.name) / "emuflow_eda2025_topology_optimizer"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/eda2025_topology_optimizer.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def vtr_architecture_importer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for VTR architecture tests"
        )
    executable = Path(_BUILD_ROOT.name) / "emuflow_vtr_arch_importer"
    pugixml = ROOT / "engines/vtr/libs/EXTERNAL/libpugixml/src"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            f"-I{pugixml}",
            str(ROOT / "src/native/vtr_architecture_importer.cpp"),
            str(pugixml / "pugixml.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def vpr_packed_netlist_importer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for packed-netlist tests"
        )
    executable = Path(_BUILD_ROOT.name) / (
        "emuflow_vpr_packed_netlist_importer"
    )
    pugixml = ROOT / "engines/vtr/libs/EXTERNAL/libpugixml/src"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            f"-I{pugixml}",
            str(ROOT / "src/native/vpr_packed_netlist_importer.cpp"),
            str(pugixml / "pugixml.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def vpr_route_checker() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for VPR route-check tests"
        )
    executable = Path(_BUILD_ROOT.name) / "emuflow_vpr_route_checker"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/vpr_route_checker.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable
