"""Build first-party native test tools once per Python test process."""

from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
_BUILD_ROOT = tempfile.TemporaryDirectory(prefix="emuflow-native-tests-")


@lru_cache(maxsize=1)
def xilinx_bookshelf_exporter() -> Path:
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for Bookshelf export tests")
    executable = Path(_BUILD_ROOT.name) / "emuflow_xilinx_bookshelf_exporter"
    subprocess.run(
        [compiler, "-std=c++17", "-O2",
         str(ROOT / "src/native/xilinx_bookshelf_exporter.cpp"),
         "-lsqlite3", "-o", str(executable)],
        check=True,
    )
    return executable


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
            str(ROOT / "src/native/emuflow_core/sqlite.cpp"),
            "-I",
            str(ROOT / "src/native"),
            "-lsqlite3",
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
            str(ROOT / "src/native/emuflow_core/artifact_io.cpp"),
            "-I",
            str(ROOT / "src/native"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


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
    return executable


@lru_cache(maxsize=1)
def tdm_exact_scheduler() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError(
            "a C++17 compiler is required for Static Exact scheduling tests"
        )
    root = Path(_BUILD_ROOT.name)
    executable = root / "bin" / "emuflow_tdm_exact_scheduler"
    executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            str(ROOT / "src/native/tdm_exact_scheduler.cpp"),
            str(ROOT / "src/native/emuflow_core/artifact_io.cpp"),
            "-I",
            str(ROOT / "src/native"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def emuflow_native() -> Path:
    compiler = (
        shutil.which("c++")
        or shutil.which("g++")
        or shutil.which("clang++")
    )
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for native entry tests")
    root = Path(_BUILD_ROOT.name)
    executable = root / "unified" / "emuflow-native"
    executable.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-pthread",
            "-DEMUFLOW_NATIVE_STAGE_LIBRARY=1",
            str(ROOT / "src/native/emuflow_native.cpp"),
            str(ROOT / "src/native/static_exact_analyzer.cpp"),
            str(ROOT / "src/native/tlr_router.cpp"),
            str(ROOT / "src/native/tdm_exact_scheduler.cpp"),
            str(ROOT / "src/native/chimew_bank_channel_assigner.cpp"),
            str(ROOT / "src/native/chimew_rudy.cpp"),
            str(ROOT / "src/native/chimew_signal_grouper.cpp"),
            str(ROOT / "src/native/chimew_position_refiner.cpp"),
            str(ROOT / "src/native/emuflow_core/artifact_io.cpp"),
            str(ROOT / "src/native/emuflow_core/sqlite.cpp"),
            "-I",
            str(ROOT / "src/native"),
            "-lsqlite3",
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


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
