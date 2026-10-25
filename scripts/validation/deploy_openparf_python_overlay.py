#!/usr/bin/env python3
"""Install checked OpenPARF Python files and an optional rebuilt ISM operator.

Python replacements point into the exact clean Git checkout. A native ISM
replacement requires its exact-commit build receipt. Neither the native base
nor the checkout is modified by installation.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


REPLACEMENTS = (
    "openparf/params.json",
    "openparf/placement/data_collections.py",
    "openparf/placement/filler.py",
    "openparf/placement/placer.py",
    "openparf/placement/sll_weight.py",
    "openparf/ops/ism_dp/ism_dp.py",
)
PLACER = "openparf/placement/placer.py"
ISM_SOURCE = "engines/openparf/openparf/ops/ism_dp/src/ism_detailed_placer.hpp"


def git(source, *args):
    return subprocess.check_output(["git", *args], cwd=source)


def deploy(source, base, output, commit, baseline, native_ism_manifest=None):
    source, base, output = (Path(x).resolve() for x in (source, base, output))
    if git(source, "rev-parse", "HEAD").decode().strip() != commit:
        raise RuntimeError("OpenPARF overlay checkout is not the expected commit")
    if git(source, "status", "--porcelain"):
        raise RuntimeError("OpenPARF overlay requires a clean versioned checkout")
    changed = set(git(source, "diff", "--name-only", baseline, commit, "--", "engines/openparf").decode().splitlines())
    allowed = {"engines/openparf/" + x for x in REPLACEMENTS}
    allowed.add("engines/openparf/EMUFLOW_PROVENANCE.md")
    native_ism = None
    if native_ism_manifest is not None:
        receipt = json.loads(Path(native_ism_manifest).read_text())
        if (receipt.get("source_commit") != commit
                or receipt.get("source_root") != str(source)
                or receipt.get("status") != "built"
                or receipt.get("provider") != "ism-utilization-capacity-and-fresh-demand-v1"):
            raise RuntimeError("ISM build does not match the exact source checkout")
        native_ism = Path(receipt["binary"]["path"]).resolve()
        if (not native_ism.name.startswith("ism_dp_cpp.") or native_ism.suffix != ".so"
                or hashlib.sha256(native_ism.read_bytes()).hexdigest() != receipt["binary"]["sha256"]):
            raise RuntimeError("ISM native binary does not match its build receipt")
        allowed.add(ISM_SOURCE)
    if not changed or not changed.issubset(allowed):
        raise RuntimeError("OpenPARF changes are not the supported Python-only patch")
    original = git(source, "show", baseline + ":engines/openparf/" + PLACER)
    original_digest = hashlib.sha256(original).hexdigest()
    if hashlib.sha256((base / PLACER).read_bytes()).hexdigest() != original_digest:
        raise RuntimeError("OpenPARF native base has a different placement implementation")
    if output.exists() or output.is_symlink():
        raise RuntimeError("OpenPARF overlay output already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("." + output.name + "." + uuid.uuid4().hex)
    temporary.mkdir()
    engine = source / "engines/openparf"
    try:
        def link_directory(relative):
            origin, target = base / relative, temporary / relative
            for member in origin.iterdir():
                if member.name in {"__pycache__", "PYTHON_OVERLAY_MANIFEST.json"}:
                    continue
                name = (relative / member.name).as_posix()
                if native_ism is not None and name.startswith("openparf/ops/ism_dp/ism_dp_cpp.") and member.suffix == ".so":
                    if member.name != native_ism.name:
                        raise RuntimeError("ISM native binary has a different Python ABI suffix")
                    (target / member.name).symlink_to(native_ism)
                elif name in REPLACEMENTS:
                    (target / member.name).symlink_to(engine / name)
                elif any(path.startswith(name + "/") for path in REPLACEMENTS):
                    (target / member.name).mkdir()
                    link_directory(relative / member.name)
                else:
                    (target / member.name).symlink_to(member.resolve(), target_is_directory=member.is_dir())
        link_directory(Path("."))
        for name in REPLACEMENTS:
            target = temporary / name
            if not target.exists():
                target.symlink_to(engine / name)
        manifest = {
            "schema": "emuflow.openparf-python-overlay/v1", "source_commit": commit,
            "baseline_commit": baseline, "native_base": str(base),
            "native_base_placer_sha256": original_digest,
            "replacements": {name: hashlib.sha256((engine / name).read_bytes()).hexdigest()
                             for name in REPLACEMENTS},
        }
        if native_ism is not None:
            manifest["native_ism"] = receipt
        (temporary / "PYTHON_OVERLAY_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(temporary, output)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "native-base", "output", "commit", "baseline"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--native-ism-manifest")
    args = parser.parse_args()
    result = deploy(args.source, args.native_base, args.output, args.commit, args.baseline,
                    args.native_ism_manifest)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
