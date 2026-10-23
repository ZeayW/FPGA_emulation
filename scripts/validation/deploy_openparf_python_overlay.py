#!/usr/bin/env python3
"""Install a checked pure-Python OpenPARF fix over unchanged native binaries.

All replacement files point into the exact clean Git checkout. Neither the
native base nor that checkout is modified by installation.
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
    "openparf/placement/placer.py", "openparf/placement/sll_weight.py",
)


def git(source, *args):
    return subprocess.check_output(["git", *args], cwd=source)


def deploy(source, base, output, commit, baseline):
    source, base, output = (Path(x).resolve() for x in (source, base, output))
    if git(source, "rev-parse", "HEAD").decode().strip() != commit:
        raise RuntimeError("OpenPARF overlay checkout is not the expected commit")
    if git(source, "status", "--porcelain"):
        raise RuntimeError("OpenPARF overlay requires a clean versioned checkout")
    changed = set(git(source, "diff", "--name-only", baseline, commit, "--", "engines/openparf").decode().splitlines())
    allowed = {"engines/openparf/" + x for x in REPLACEMENTS}
    allowed.add("engines/openparf/EMUFLOW_PROVENANCE.md")
    if not changed or not changed.issubset(allowed):
        raise RuntimeError("OpenPARF changes are not the supported Python-only patch")
    original = git(source, "show", baseline + ":engines/openparf/openparf/placement/placer.py")
    original_digest = hashlib.sha256(original).hexdigest()
    if hashlib.sha256((base / REPLACEMENTS[0]).read_bytes()).hexdigest() != original_digest:
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
                if name in REPLACEMENTS:
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
    args = parser.parse_args()
    result = deploy(args.source, args.native_base, args.output, args.commit, args.baseline)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
