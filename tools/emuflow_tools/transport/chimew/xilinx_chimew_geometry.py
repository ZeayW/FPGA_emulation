"""Bounded package-pin catalog retained by Chimew compilation admission."""
from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from types import MappingProxyType
from pathlib import Path

from emuflow.common.errors import ValidationError
from emuflow.common.native_tools import native_file_generation


def _native_package_geometry(path: Path, part: str):
    from emuflow_tools.transport.board_configuration import _record, _run
    with tempfile.TemporaryDirectory(prefix='chimew-package-') as directory:
        root=Path(directory).resolve();output=root/'catalog.json'
        source=_record(path,'physical-input-document-v1')
        _run(['phase6','prepare-chimew-package',source['path'],part,output],
             [source],output,'chimew-package-geometry-v1','phase6.prepare-chimew-package',root)
        value=json.loads(output.read_text(encoding='utf-8'))
        if value.get('path')!=source['path'] or value.get('part')!=part:
            raise ValidationError('native Chimew package geometry belongs to a different source')
        return value




@dataclass(frozen=True)
class XilinxPackageGeometry:
    """One validated device-sized catalog reused across the live compile."""
    path: Path
    part: str
    generation: tuple[int, ...]
    sha256: str
    pins: object

    @classmethod
    def open(cls, path: Path, part: str):
        value = _native_package_geometry(path.resolve(),part)
        value['path'] = Path(value['path'])
        value['generation'] = tuple(value['generation'])
        value['pins'] = {name:tuple(point) for name,point in value['pins'].items()}
        return cls.from_control_snapshot(value)

    def control_snapshot(self):
        """Carry only the bounded device catalog, never placement/endpoint data.

        The native controller owns this private, generation-bound control state.
        Retaining package pins avoids rescanning the full ArchitectureDB in each
        isolated phase. The architectural source remains the authority.
        """
        self.require_unchanged()
        if type(self.pins) is not MappingProxyType or not 0 < len(self.pins) <= 16384:
            raise ValidationError(
                "Chimew control package catalog exceeds device bounds"
            )
        value = {
            "schema": "emuflow.xilinx-package-geometry-control/v1",
            "path": self.path,
            "part": self.part,
            "generation": self.generation,
            "sha256": self.sha256,
            "pins": dict(self.pins),
        }
        self.require_unchanged()
        return value

    @classmethod
    def from_control_snapshot(cls, value):
        """Restore preflight's device facts without rereading a device graph."""
        if (
            type(value) is not dict
            or set(value) != {"schema", "path", "part", "generation", "sha256", "pins"}
            or value["schema"] != "emuflow.xilinx-package-geometry-control/v1"
        ):
            raise ValidationError("invalid Chimew package control identity")
        path, part = value["path"], value["part"]
        generation, digest, pins = value["generation"], value["sha256"], value["pins"]
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or type(part) is not str
            or not part
            or type(generation) is not tuple
            or len(generation) != 7
            or any(type(v) is not int or v < 0 for v in generation)
            or type(digest) is not str
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or type(pins) is not dict
            or not 0 < len(pins) <= 16384
        ):
            raise ValidationError("invalid Chimew package control envelope")
        for name, point in pins.items():
            if (
                type(name) is not str
                or not name
                or type(point) is not tuple
                or len(point) != 3
                or type(point[0]) is not str
                or not point[0]
                or any(type(v) is not int or v < 0 for v in point[1:])
            ):
                raise ValidationError("invalid Chimew package control pin geometry")
        result = cls(path, part, generation, digest, MappingProxyType(pins))
        try:
            if path.resolve() != path:
                raise ValidationError("Chimew package control source alias changed")
            result.require_unchanged()
        except OSError as error:
            raise ValidationError(
                "Chimew package control source is unavailable"
            ) from error
        return result

    def require_unchanged(self):
        if native_file_generation(self.path) != self.generation:
            raise ValidationError("Chimew package geometry generation changed after preflight")
