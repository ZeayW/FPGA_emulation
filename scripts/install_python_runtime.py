#!/usr/bin/env python3
"""Install Python sources with their package hierarchy, excluding local caches."""

import argparse
from pathlib import Path
import shutil


def install(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination or source in destination.parents:
        raise ValueError("the runtime destination must be outside its source package")
    for path in sorted(source.rglob("*.py")):
        if "__pycache__" in path.relative_to(source).parts:
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    install(arguments.source, arguments.destination)
