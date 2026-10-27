"""Bounded fixture snapshots of the sole typed OpenPARF bridge producer.

No legacy bridge algorithm or production JSON writer is retained here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import tempfile
import shutil
from tests.physical_handoff_native_fixture import encode_typed_bridge_facts

from emuflow.common.errors import ValidationError
from emuflow.common.io import file_sha256, read_json, write_json
from emuflow.physical.openparf.xilinx_openparf_atomic import (
    write_xilinx_openparf_atomic_placement_certificate,
)
from emuflow.physical.handoff import (
    materialize_xilinx_openparf_atomic_contract as materialize_typed,
)


def _snapshot(value):
    if isinstance(value, Mapping):
        return {key: _snapshot(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_snapshot(item) for item in value]
    return value


def materialize_bridge_fixture(
    mapped_path,
    architecture_path,
    certificate_path,
    packed_output,
    placement_output,
    *,
    physical_handoff_path=None,
    **kwargs,
):
    certificate = read_json(certificate_path)
    packed_output, placement_output = Path(packed_output), Path(placement_output)
    packed_output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".bridge-fixture-", dir=packed_output.parent
    ) as temporary:
        root = Path(temporary)
        if "clusters" in certificate:
            if (
                len(certificate["clusters"]) > 4096
                or sum(len(c.get("assignments", [])) for c in certificate["clusters"])
                > 4096
            ):
                raise ValidationError("bridge JSON fixture exceeds bounded population")
            certificate_path = root / "certificate.json"
            write_xilinx_openparf_atomic_placement_certificate(
                certificate_path, certificate
            )
        packed, placement = {}, {}
        packed_sink = kwargs.pop("packed_value_sink", None)
        placement_sink = kwargs.pop("placement_value_sink", None)
        kwargs.pop("source_packed_path", None)
        kwargs.pop("source_packed_value", None)
        inputs = encode_typed_bridge_facts(
            mapped_path, architecture_path, certificate_path, root / "inputs"
        )
        result = materialize_typed(
            inputs.mapped,
            architecture_path,
            inputs.certificate,
            root / "physical.sqlite",
            source_packed_path=inputs.source,
            packed_value_sink=packed,
            placement_value_sink=placement,
            **kwargs,
        )
        if physical_handoff_path is not None:
            # A bounded downstream fixture snapshot has a caller-selected path.
            # Production always consumes the native generation path in its report.
            from emuflow.physical.xilinx_physical_handoff import (
                open_xilinx_physical_handoff,
            )

            target = Path(physical_handoff_path)
            shutil.copyfile(result["physical_handoff"], target)
            packed, placement = open_xilinx_physical_handoff(
                target, validated_sha256=result["physical_handoff_sha256"]
            )
            for sink, value in ((packed_sink, packed), (placement_sink, placement)):
                if sink is not None:
                    sink.clear()
                    sink.update(value)
            return dict(
                result, physical_handoff=str(target), fixture_typed_snapshot=True
            )
        if result["placed_cells"] > 4096:
            raise ValidationError("bridge JSON fixture exceeds bounded population")
        packed, placement = _snapshot(packed), _snapshot(placement)
        packed["source"]["mapped_json_sha256"] = file_sha256(mapped_path)
        for value in (packed, placement):
            value["source"].pop("physical_handoff_sha256", None)
        write_json(packed_output, packed, compact=True)
        packed_digest = file_sha256(packed_output)
        placement["source"]["packed_sha256"] = packed_digest
        write_json(placement_output, placement, compact=True)
        for sink, value in ((packed_sink, packed), (placement_sink, placement)):
            if sink is not None:
                sink.clear()
                sink.update(value)
        result = dict(result)
        result.update(
            packed_sha256=packed_digest, placement_sha256=file_sha256(placement_output)
        )
        result["fixture_json_snapshot"] = True
        return result
