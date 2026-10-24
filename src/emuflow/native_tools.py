"""Resolve native tools built from this repository without PATH fallbacks."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from .errors import EmuFlowError


REPO_ROOT = Path(__file__).resolve().parents[2]


def native_install_roots() -> tuple[Path, ...]:
    roots = []
    configured = os.environ.get("EMUFLOW_NATIVE_ROOT")
    if configured:
        # An explicit installation is authoritative, including missing tools.
        return (Path(configured).expanduser().resolve(),)

    roots.append(REPO_ROOT / "build" / "native" / "install")

    launcher = Path(sys.argv[0]).expanduser()
    if launcher.parent.name == "bin":
        roots.append(launcher.resolve().parent.parent)

    unique = []
    seen = set()
    for root in roots:
        normalized = root.resolve()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return tuple(unique)


def resolve_native_executable(
    name: str,
    explicit: Optional[str] = None,
) -> str:
    """Return an explicit or in-tree-built executable.

    An explicit path remains useful for controlled comparison experiments.
    There is deliberately no ``PATH`` lookup: the default flow must execute
    the source revision built by this repository.
    """

    if explicit is not None:
        # Backends such as VPR intentionally execute in an artifact directory.
        # Bind a caller-supplied relative path to the invocation directory now,
        # before any backend changes its subprocess working directory.
        return str(Path(explicit).expanduser().resolve())

    candidates = tuple(root / "bin" / name for root in native_install_roots())
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    searched = ", ".join(str(candidate) for candidate in candidates)
    raise EmuFlowError(
        f"in-tree {name} build product was not found; searched: {searched}. "
        "Build the monorepo with `cmake --preset release && "
        "cmake --build --preset release` or set EMUFLOW_NATIVE_ROOT."
    )


def is_unified_native_executable(path: str) -> bool:
    """Recognize the unified entry, including an explicit symlink to it."""
    return Path(path).expanduser().resolve().name == "emuflow-native"


def resolve_native_stage_command(
    phase: str,
    operation: str,
    explicit: Optional[str] = None,
) -> list[str]:
    """Resolve the unified production entry; never select an older install.

    Explicit external engines are intentional comparison inputs. They are not
    selected because a production installation is incomplete.
    """
    if explicit is not None and not is_unified_native_executable(explicit):
        return [resolve_native_executable("emuflow-native", explicit)]
    return [resolve_native_executable("emuflow-native", explicit), phase, operation]


def native_file_generation(path: Path) -> tuple[int, ...]:
    """Local ArtifactRef metadata; not a portable source/semantic hash."""
    status = Path(path).stat()
    modified_seconds, modified_ns = divmod(status.st_mtime_ns, 1_000_000_000)
    changed_seconds, changed_ns = divmod(status.st_ctime_ns, 1_000_000_000)
    return (
        status.st_dev, status.st_ino, status.st_size, modified_seconds,
        modified_ns, changed_seconds, changed_ns,
    )
