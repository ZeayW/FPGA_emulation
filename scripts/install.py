#!/usr/bin/env python3
"""Build a self-contained EmuFlow runtime and its XCVU19P device contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import platform
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/native"
INSTALL = BUILD / "install"
PROVIDER_SOURCE = ROOT / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"
RAPIDWRIGHT_VERSION = "2026.1.0"
RAPIDWRIGHT_TAG = "v2026.1.0-beta"
RAPIDWRIGHT_ASSET = f"rapidwright-{RAPIDWRIGHT_VERSION}-standalone-lin64.jar"
RAPIDWRIGHT_URL = (
    "https://github.com/Xilinx/RapidWright/releases/download/"
    f"{RAPIDWRIGHT_TAG}/{RAPIDWRIGHT_ASSET}"
)
RAPIDWRIGHT_SHA256 = (
    "18f81833595ef8a5191a72f9431727e17602de2fe4dddbba2d281358801a1fc9"
)
TIMING_MEMBERS = {
    "timing/ultrascaleplus/intersite_delay_terms.txt": (
        "3b122837c4a1b5f3c212fc6353bee3a0854a204f2f69e2bc1cac4a2a1f9d7333"
    ),
    "timing/ultrascaleplus/intrasite_delay_terms.txt": (
        "ff08ce9041da649f2bf886d900f033dd23de8ad54eb4db55d37ce032dc0ec0a0"
    ),
}


class InstallError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(arguments, *, cwd: Path = ROOT, environment=None) -> None:
    rendered = " ".join(str(item) for item in arguments)
    print(f"+ {rendered}", flush=True)
    try:
        subprocess.run(
            [str(item) for item in arguments],
            cwd=cwd,
            env=environment,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise InstallError(f"installation command failed: {rendered}") from error


def program(name: str) -> Path:
    result = shutil.which(name)
    if result is None:
        raise InstallError(f"required program is missing: {name}")
    return Path(result).resolve()


def python_is_ready(python: Path) -> bool:
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import numpy, torch, yaml; "
            "assert tuple(map(int, torch.__version__.split('+')[0].split('.')[:2])) >= (1, 6)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def prepare_python(python: Path) -> Path:
    """Return a Python with the build dependencies, creating one when needed."""
    if not python.is_file() or not os.access(python, os.X_OK):
        raise InstallError(f"selected Python is not executable: {python}")
    if python_is_ready(python):
        return python
    runtime = BUILD / "python-runtime"
    runtime_python = runtime / "bin/python"
    if not runtime_python.is_file():
        command([python, "-m", "venv", runtime])
    command([runtime_python, "-m", "pip", "install", "--upgrade", "pip"])
    command(
        [
            runtime_python,
            "-m",
            "pip",
            "install",
            "numpy==2.0.2",
            "PyYAML==6.0.3",
        ]
    )
    command(
        [
            runtime_python,
            "-m",
            "pip",
            "install",
            "torch==2.8.0",
            "--index-url",
            "https://download.pytorch.org/whl/cpu",
        ]
    )
    if not python_is_ready(runtime_python):
        raise InstallError("could not prepare the bundled Python build environment")
    return runtime_python.resolve()


def download_rapidwright(target: Path) -> None:
    if target.is_file() and sha256(target) == RAPIDWRIGHT_SHA256:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".download")
    if staged.exists():
        staged.unlink()
    print(f"Downloading pinned RapidWright {RAPIDWRIGHT_VERSION}...", flush=True)
    try:
        with urllib.request.urlopen(RAPIDWRIGHT_URL, timeout=120) as response:
            with staged.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
    except OSError as error:
        if staged.exists():
            staged.unlink()
        raise InstallError(f"RapidWright download failed: {error}") from error
    if sha256(staged) != RAPIDWRIGHT_SHA256:
        staged.unlink()
        raise InstallError("RapidWright download checksum does not match the pinned release")
    os.replace(staged, target)


def extract_rapidwright_runtime(jar: Path, rapidwright_root: Path) -> Path:
    timing = rapidwright_root / "data/timing"
    timing.mkdir(parents=True, exist_ok=True)
    with ZipFile(jar) as archive:
        parts = archive.read("data/parts.db")
        parts_path = rapidwright_root / "data/parts.db"
        parts_path.parent.mkdir(parents=True, exist_ok=True)
        parts_path.write_bytes(parts)
        for member, expected in TIMING_MEMBERS.items():
            content = archive.read(member)
            observed = hashlib.sha256(content).hexdigest()
            if observed != expected:
                raise InstallError(f"RapidWright timing member changed: {member}")
            (timing / Path(member).name).write_bytes(content)
    return timing


def installed_environment() -> dict:
    path = INSTALL / "share/emuflow/environment.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def device_contract_is_current(provider: Path, architecture: Path, constraints: Path) -> bool:
    current = installed_environment()
    return (
        current.get("schema") == "emuflow.environment/v1"
        and current.get("provider_manifest") == str(provider)
        and current.get("architecture") == str(architecture)
        and current.get("native_constraints") == str(constraints)
        and architecture.is_file()
        and constraints.is_file()
    )


def build_runtime(python: Path, jobs: int) -> None:
    command(
        [
            "cmake",
            "--preset",
            "release",
            f"-DPython3_EXECUTABLE={python}",
            f"-DEMUFLOW_OPENPARF_PYTHON={python}",
        ]
    )
    command(["cmake", "--build", "--preset", "release", "--parallel", str(jobs)])


def runtime_environment(rapidwright_root: Path) -> dict:
    result = dict(os.environ)
    result["RAPIDWRIGHT_PATH"] = str(rapidwright_root)
    result["EMUFLOW_NATIVE_ROOT"] = str(INSTALL)
    result["PYTHONPATH"] = str(INSTALL / "lib")
    result["PATH"] = os.pathsep.join((str(INSTALL / "bin"), result.get("PATH", "")))
    return result


def generate_device_contract(
    *,
    python: Path,
    java: Path,
    javac: Path,
    jar: Path,
    rapidwright_root: Path,
    java_heap: str,
) -> tuple[Path, Path, Path]:
    provider = INSTALL / "share/emuflow/device/xcvu19p.provider.json"
    architecture = INSTALL / "share/emuflow/device/xcvu19p.architecture.json"
    constraints = INSTALL / "share/emuflow/device/xcvu19p.native-constraints.json"
    provider.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PROVIDER_SOURCE, provider)
    if device_contract_is_current(provider, architecture, constraints):
        return provider, architecture, constraints

    work = BUILD / "device"
    work.mkdir(parents=True, exist_ok=True)
    base_architecture = work / "xcvu19p.base.architecture.json"
    route_certificate = work / "xcvu19p.route-certificate.json"
    sidecar = work / "xcvu19p.physical-regions.json"
    device = work / "xcvu19p.device"
    environment = runtime_environment(rapidwright_root)

    command(
        [
            java,
            f"-Xmx{java_heap}",
            "-cp",
            jar,
            "com.xilinx.rapidwright.interchange.DeviceResourcesExample",
            "xcvu19p",
            "--skip_route_resources",
        ],
        cwd=work,
        environment=environment,
    )
    if not device.is_file():
        raise InstallError("RapidWright did not generate xcvu19p.device")
    command(
        [
            python,
            ROOT / "scripts/rapidwright/export_route_resource_certificate.py",
            "--rapidwright-jar",
            jar,
            "--provider-manifest",
            provider,
            "--output",
            route_certificate,
            "--java",
            java,
            "--javac",
            javac,
            "--java-heap",
            java_heap,
        ],
        environment=environment,
    )
    emuflow = INSTALL / "bin/emuflow"
    command(
        [
            emuflow,
            "arch",
            "import-rapidwright-device",
            device,
            "--provider-manifest",
            provider,
            "--route-certificate",
            route_certificate,
            "--output",
            base_architecture,
        ],
        environment=environment,
    )
    command(
        [
            java,
            f"-Xmx{java_heap}",
            "-cp",
            jar,
            "org.python.util.jython",
            ROOT / "scripts/rapidwright/export_physical_regions.py",
            "xcvu19p-fsva3824-2-e",
            "xcvu19p",
            "fsva3824",
            base_architecture,
            sidecar,
        ],
        environment=environment,
    )
    command(
        [
            emuflow,
            "arch",
            "merge-physical-regions",
            "--arch",
            base_architecture,
            "--sidecar",
            sidecar,
            "--output",
            architecture,
        ],
        environment=environment,
    )
    command(
        [
            python,
            ROOT / "scripts/rapidwright/export_native_device_constraints.py",
            "--rapidwright-jar",
            jar,
            "--provider-manifest",
            provider,
            "--architecture",
            architecture,
            "--output",
            constraints,
            "--java",
            java,
            "--javac",
            javac,
            "--java-heap",
            java_heap,
        ],
        environment=environment,
    )
    return provider, architecture, constraints


def publish_environment(
    *,
    python: Path,
    java: Path,
    jar: Path,
    rapidwright_root: Path,
    timing_data: Path,
    provider: Path,
    architecture: Path,
    constraints: Path,
) -> Path:
    output = INSTALL / "share/emuflow/environment.json"
    preplacement = INSTALL / "share/emuflow/device/xilinx-preplacement-timing.json"
    environment = runtime_environment(rapidwright_root)
    emuflow = INSTALL / "bin/emuflow"
    command(
        [
            emuflow,
            "arch",
            "build-xilinx-preplacement-timing",
            "--timing-data-dir",
            timing_data,
            "--output",
            preplacement,
        ],
        environment=environment,
    )
    profile = {
        "schema": "emuflow.environment/v1",
        "native_root": str(INSTALL),
        "timing_data_dir": str(timing_data),
        "architecture": str(architecture),
        "native_constraints": str(constraints),
        "provider_manifest": str(provider),
        "backends": {
            "rapidwright": {
                "jar": str(jar),
                "java": str(java),
                "device_data": str(rapidwright_root),
                "openparf_install": str(INSTALL / "openparf"),
                "openparf_python": str(python),
            }
        },
        "environment": {},
        "preplacement_timing": {
            "path": str(preplacement),
            "sha256": sha256(preplacement),
        },
    }
    vivado = shutil.which("vivado")
    if vivado:
        profile["backends"]["vivado"] = {"executable": str(Path(vivado).resolve())}
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = output.with_name(output.name + ".new")
    staged.write_text(json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    command(
        [emuflow, "environment", "check", staged, "--backend", "rapidwright"],
        environment=environment,
    )
    if "vivado" in profile["backends"]:
        command(
            [emuflow, "environment", "check", staged, "--backend", "vivado"],
            environment=environment,
        )
    os.replace(staged, output)
    return output


def link_launchers() -> None:
    destination = Path.home() / ".local/bin"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("emuflow", "emuflow-native"):
        target = INSTALL / "bin" / name
        link = destination / name
        if link.is_symlink() or link.exists():
            if link.resolve() == target.resolve():
                continue
            print(f"Keeping existing launcher: {link}", flush=True)
            continue
        link.symlink_to(target)
    if str(destination) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Add {destination} to PATH, or invoke {INSTALL / 'bin/emuflow'} directly.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build EmuFlow and prepare its pinned XCVU19P runtime"
    )
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--java-heap", default="32g")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--rapidwright-jar",
        type=Path,
        help="use an existing pinned standalone JAR instead of downloading it",
    )
    args = parser.parse_args(argv)
    if args.jobs <= 0:
        raise InstallError("--jobs must be positive")
    if not sys.platform.startswith("linux") or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise InstallError(
            "the complete physical runtime currently requires Linux x86-64"
        )
    python = prepare_python(args.python.expanduser().resolve())
    program("cmake")
    java = program("java")
    javac = program("javac")

    rapidwright_root = INSTALL / "share/emuflow/rapidwright"
    jar = (
        args.rapidwright_jar.expanduser().resolve()
        if args.rapidwright_jar
        else rapidwright_root / RAPIDWRIGHT_ASSET
    )
    if args.rapidwright_jar:
        if not jar.is_file() or sha256(jar) != RAPIDWRIGHT_SHA256:
            raise InstallError("--rapidwright-jar is not the pinned 2026.1.0 asset")
    else:
        download_rapidwright(jar)
    timing_data = extract_rapidwright_runtime(jar, rapidwright_root)
    build_runtime(python, args.jobs)
    provider, architecture, constraints = generate_device_contract(
        python=python,
        java=java,
        javac=javac,
        jar=jar,
        rapidwright_root=rapidwright_root,
        java_heap=args.java_heap,
    )
    manifest = publish_environment(
        python=python,
        java=java,
        jar=jar,
        rapidwright_root=rapidwright_root,
        timing_data=timing_data,
        provider=provider,
        architecture=architecture,
        constraints=constraints,
    )
    link_launchers()
    print(f"EmuFlow installation is ready: {manifest}")
    backends = sorted(installed_environment()["backends"])
    print("Available physical backends: " + ", ".join(backends))
    print(
        "Run: emuflow run rtl/top.sv --top top --clock clk=10 "
        "--backend rapidwright"
    )
    if "vivado" in backends:
        print(
            "Or:  emuflow run rtl/top.sv --top top --clock clk=10 "
            "--backend vivado"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as error:
        raise SystemExit(f"install: {error}") from error
