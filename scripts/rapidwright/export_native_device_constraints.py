#!/usr/bin/env python3

"""Export and validate compact native Xilinx physical constraints."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from emuflow.rapidwright_provider import (
    load_rapidwright_provider_manifest,
    validate_rapidwright_provider_manifest,
)
from emuflow.xilinx_native_device_constraints import (
    load_xilinx_native_device_constraints,
)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stdout.splitlines()[-40:])
        raise SystemExit(
            f"command failed with exit code {completed.returncode}\n{tail}"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rapidwright-jar", type=Path, required=True)
    parser.add_argument("--provider-manifest", type=Path, required=True)
    parser.add_argument("--architecture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--scratch-dir",
        type=Path,
        help="optional external scratch root for Java compilation",
    )
    parser.add_argument("--java", default="java")
    parser.add_argument("--javac", default="javac")
    parser.add_argument("--java-heap", default="24g")
    args = parser.parse_args()

    java_source = (
        Path(__file__).resolve().parent / "EmuFlowNativeDeviceConstraints.java"
    )
    manifest = load_rapidwright_provider_manifest(args.provider_manifest)
    checked = validate_rapidwright_provider_manifest(manifest)
    if not args.rapidwright_jar.is_file():
        raise SystemExit(f"RapidWright jar is missing: {args.rapidwright_jar}")
    if not args.architecture.is_file():
        raise SystemExit(f"ArchitectureDB is missing: {args.architecture}")
    if args.scratch_dir is not None:
        args.scratch_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="emuflow-rw-native-constraints-",
        dir=args.scratch_dir,
    ) as temporary:
        classes = Path(temporary) / "classes"
        classes.mkdir()
        _run(
            [
                args.javac,
                "-cp",
                str(args.rapidwright_jar),
                "-d",
                str(classes),
                str(java_source),
            ]
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                args.java,
                f"-Xmx{args.java_heap}",
                "-cp",
                os.pathsep.join((str(classes), str(args.rapidwright_jar))),
                "EmuFlowNativeDeviceConstraints",
                checked["device_identity"]["device"],
                checked["part"],
                checked["version"],
                checked["revision"],
                _sha256(args.provider_manifest),
                checked["device_database_md5"],
                _sha256(args.architecture),
                str(args.output),
            ]
        )

    _, report = load_xilinx_native_device_constraints(
        args.output,
        architecture_path=args.architecture,
        provider_manifest_path=args.provider_manifest,
    )
    print(
        "RapidWright native device constraints: "
        f"{report['status']} {report['dedicated_edges']} dedicated edges "
        f"{report['dedicated_edges_by_kind']} "
        f"{report['clock_regions']} clock regions {report['slrs']} SLRs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
