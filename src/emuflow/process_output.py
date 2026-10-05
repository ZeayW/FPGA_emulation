"""Bounded subprocess output capture for long-running provider tools."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Optional


DEFAULT_OUTPUT_TAIL_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def run_with_bounded_output(
    command: Iterable[str],
    *,
    cwd: Optional[Path] = None,
    environment: Optional[Mapping[str, str]] = None,
    maximum_bytes: int = DEFAULT_OUTPUT_TAIL_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` while retaining only its final combined-output bytes.

    ``subprocess.run(..., stdout=PIPE)`` retains the complete provider log in
    memory.  Synthesis logs for large RTL can be hundreds of MiB or more, even
    though callers only need a bounded diagnostic tail on failure.  Drain the
    pipe continuously so the child cannot block, and discard old bytes as the
    fixed-size tail advances.
    """

    argv = list(command)
    if not argv:
        raise ValueError("bounded subprocess command must not be empty")
    if maximum_bytes <= 0:
        raise ValueError("bounded subprocess maximum_bytes must be positive")
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=None if environment is None else dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
        raise AssertionError("bounded subprocess pipe was not created")
    tail = bytearray()
    while True:
        chunk = process.stdout.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        tail.extend(chunk)
        excess = len(tail) - maximum_bytes
        if excess > 0:
            del tail[:excess]
    return_code = process.wait()
    return subprocess.CompletedProcess(
        argv,
        return_code,
        stdout=tail.decode("utf-8", errors="replace"),
    )
