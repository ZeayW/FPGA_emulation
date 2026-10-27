"""Bounded fixture snapshots of the sole typed OpenPARF bridge producer.

No legacy bridge algorithm or production JSON writer is retained here.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import tempfile

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.physical.openparf.xilinx_openparf_atomic import write_xilinx_openparf_atomic_placement_certificate
from emuflow.physical.openparf.xilinx_openparf_bridge import materialize_xilinx_openparf_atomic_contract as materialize_typed


def _snapshot(value):
    if isinstance(value, Mapping):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_snapshot(item) for item in value]
    return value


def materialize_bridge_fixture(
    mapped_path, architecture_path, certificate_path, packed_output, placement_output,
    *, physical_handoff_path=None, **kwargs,
):
    if physical_handoff_path is not None:
        return materialize_typed(mapped_path, architecture_path, certificate_path, physical_handoff_path, **kwargs)
    certificate = read_json(certificate_path)
    packed_output, placement_output = Path(packed_output), Path(placement_output)
    packed_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bridge-fixture-', dir=packed_output.parent) as temporary:
        root = Path(temporary)
        if 'clusters' in certificate:
            if len(certificate['clusters']) > 4096 or sum(len(c.get('assignments', [])) for c in certificate['clusters']) > 4096:
                raise ValidationError('bridge JSON fixture exceeds bounded population')
            certificate_path = root / 'certificate.json'
            write_xilinx_openparf_atomic_placement_certificate(certificate_path, certificate)
        packed, placement = {}, {}
        packed_sink = kwargs.pop('packed_value_sink', None)
        placement_sink = kwargs.pop('placement_value_sink', None)
        result = materialize_typed(
            mapped_path, architecture_path, certificate_path, root/'physical.sqlite',
            packed_value_sink=packed, placement_value_sink=placement, **kwargs,
        )
        if result['placed_cells'] > 4096:
            raise ValidationError('bridge JSON fixture exceeds bounded population')
        packed, placement = _snapshot(packed), _snapshot(placement)
        for value in (packed, placement):
            value['source'].pop('physical_handoff_sha256', None)
        write_json(packed_output, packed, compact=True)
        packed_digest = file_sha256(packed_output)
        placement['source']['packed_sha256'] = packed_digest
        write_json(placement_output, placement, compact=True)
        for sink, value in ((packed_sink, packed), (placement_sink, placement)):
            if sink is not None:
                sink.clear(); sink.update(value)
        result = dict(result)
        result.update(packed_sha256=packed_digest, placement_sha256=file_sha256(placement_output))
        result['fixture_json_snapshot'] = True
        return result
