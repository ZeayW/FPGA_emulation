"""Build first-party native test tools once per Python test process."""

from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
_BUILD_ROOT = tempfile.TemporaryDirectory(prefix="emuflow-native-tests-")


@lru_cache(maxsize=1)
def tlr_router() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for routing tests")
    executable = Path(_BUILD_ROOT.name) / "emuflow_tlr_router"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-pthread",
            str(ROOT / "src" / "native" / "tlr_router.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def tdm_ratio_optimizer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for TDM tests")
    executable = Path(_BUILD_ROOT.name) / "emuflow_tdm_ratio_optimizer"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src" / "native" / "tdm_ratio_optimizer.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def tdm_slot_optimizer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for TDM tests")
    executable = Path(_BUILD_ROOT.name) / "emuflow_tdm_slot_optimizer"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src" / "native" / "tdm_slot_optimizer.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def tdm_timing_dag_optimizer() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for TDM tests")
    executable = Path(_BUILD_ROOT.name) / (
        "emuflow_tdm_timing_dag_optimizer"
    )
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(
                ROOT
                / "src"
                / "native"
                / "tdm_timing_dag_optimizer.cpp"
            ),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def tdm_partition_feedback() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for partition-feedback tests"
        )
    executable = Path(_BUILD_ROOT.name) / "emuflow_tdm_partition_feedback"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/tdm_partition_feedback.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def patron_refiner() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for PATRON tests")
    executable = Path(_BUILD_ROOT.name) / "emuflow_patron_refiner"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/patron_refiner.cpp"),
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
