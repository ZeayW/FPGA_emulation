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


def _runtime_link_flags() -> list[str]:
    # Match the thin runtime: test tools must run with the compiler's C++
    # runtime even when the host system libstdc++ predates that compiler.
    return ["-static-libstdc++", "-static-libgcc"] if sys.platform.startswith("linux") else []


def chimew_bank_channel_assigner() -> Path:
    return emuflow_native()


def tlr_router() -> Path:
    """Routing fixtures use the unified stage entry and shared core."""
    return emuflow_native()


def static_exact_analyzer() -> Path:
    return emuflow_native()


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
    subprocess.run([sys.executable, str(ROOT / 'scripts/generate_store_schema_headers.py'),
        str(generated / 'emuflow_core')], check=True)
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            *_runtime_link_flags(),
            "-O2",
            "-pthread",
            "-DEMUFLOW_NATIVE_STAGE_LIBRARY=1",
            str(entry),
            *(str(ROOT / "src/native" / source) for source in _NATIVE_STAGE_SOURCES),
            "-I",
            str(ROOT / "src/native"),
            "-I",
            str(generated),
            "-lsqlite3",
            "-lz",
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable


@lru_cache(maxsize=1)
def emuflow_native() -> Path:
    return _build_native_stage_program(
        ROOT / "src/native/emuflow_native.cpp",
        Path(_BUILD_ROOT.name) / "bin/emuflow-native",
    )


@lru_cache(maxsize=1)
def patron_ingress_oracle() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_patron_ingress_test.cpp",
        Path(_BUILD_ROOT.name) / "native_patron_ingress_test",
    )


@lru_cache(maxsize=1)
def exact_semantic_view_test() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_exact_semantic_view_test.cpp",
        Path(_BUILD_ROOT.name) / "native-exact-semantic-view-test",
    )


@lru_cache(maxsize=1)
def original_timing_test() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_original_timing_test.cpp",
        Path(_BUILD_ROOT.name) / "native-original-timing-test",
    )


@lru_cache(maxsize=1)
def source_launch_clocks_test() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_source_launch_clocks_test.cpp",
        Path(_BUILD_ROOT.name) / "native-source-launch-clocks-test",
    )


@lru_cache(maxsize=1)
def word_physical_binding_test() -> Path:
    return _build_native_stage_program(
        ROOT / "tests/native_word_physical_binding_test.cpp",
        Path(_BUILD_ROOT.name) / "native-word-physical-binding-test",
    )


@lru_cache(maxsize=1)
def native_json_record() -> Path:
    compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        raise RuntimeError("a C++17 compiler is required for JSON record tests")
    executable = Path(_BUILD_ROOT.name) / "native-json-record"
    subprocess.run([compiler, "-std=c++17", *_runtime_link_flags(), "-O2", str(ROOT / "tests/native_json_record_test.cpp"),
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
    return emuflow_native()


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
            *_runtime_link_flags(),
            "-O2",
            str(ROOT / "src/native/eda2025_topology_optimizer.cpp"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    return executable
