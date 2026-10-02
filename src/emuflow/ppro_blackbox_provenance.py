"""Deterministic source identity for the PPro black-box runtime boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from .errors import ValidationError


RUNNER_SOURCE_BUNDLE_SCHEMA = "emuflow.ppro-runner-source-bundle/v1"
RUNNER_SOURCE_MEMBERS = (
    "io.py",
    "ppro_blackbox_calibration.py",
    "ppro_blackbox_campaign.py",
    "ppro_blackbox_constraints.py",
    "ppro_blackbox_ppro_adapter.py",
    "ppro_blackbox_provenance.py",
    "ppro_blackbox_runner.py",
    "ppro_blackbox_runtime.py",
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def runner_source_bundle(package_root: Path | None = None) -> Dict[str, Any]:
    """Return a path-redacted revision covering every runtime source module."""

    root = (
        package_root.resolve()
        if package_root is not None
        else Path(__file__).resolve().parent
    )
    if not root.is_dir():
        raise ValidationError("PPro runner source package root does not exist")
    members = []
    for relative in RUNNER_SOURCE_MEMBERS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValidationError(
                f"PPro runner source member is missing or unsafe: {relative}"
            )
        data = path.read_bytes()
        members.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    identity = {
        "schema": RUNNER_SOURCE_BUNDLE_SCHEMA,
        "members": members,
    }
    return {
        **identity,
        "runner_revision": hashlib.sha256(_canonical(identity)).hexdigest(),
    }
