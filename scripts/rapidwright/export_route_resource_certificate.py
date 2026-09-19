#!/usr/bin/env python3

"""Export and independently validate a compact RapidWright route certificate."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

from emuflow.rapidwright_provider import (
    load_rapidwright_provider_manifest,
    load_rapidwright_route_certificate,
    validate_rapidwright_provider_manifest,
)


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
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
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--java", default="java")
    parser.add_argument("--javac", default="javac")
    parser.add_argument("--java-heap", default="24g")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    java_source = script_dir / "EmuFlowRouteResourceCertificate.java"
    manifest = load_rapidwright_provider_manifest(args.provider_manifest)
    checked = validate_rapidwright_provider_manifest(manifest)
    if not args.rapidwright_jar.is_file():
        raise SystemExit(f"RapidWright jar is missing: {args.rapidwright_jar}")

    with tempfile.TemporaryDirectory(prefix="emuflow-rw-route-cert-") as temp:
        classes = Path(temp) / "classes"
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
                "EmuFlowRouteResourceCertificate",
                checked["device_identity"]["device"],
                checked["part"],
                checked["version"],
                checked["revision"],
                _sha256(args.provider_manifest),
                checked["device_database_md5"],
                str(args.output),
            ]
        )

    _, report = load_rapidwright_route_certificate(
        args.output,
        manifest,
        manifest_path=args.provider_manifest,
    )
    print(
        "RapidWright route certificate: "
        f"{report['status']} {report['payload_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
