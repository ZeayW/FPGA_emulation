"""Shared helpers for projecting public contest graphs into BoardDB."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from emuflow.common.errors import ValidationError
from emuflow_tools.document_io import read_json
from emuflow.common.json_io import write_json
from emuflow.platform.platform import Platform


def materialize_homogeneous_boarddb(
    *,
    output_path: Path,
    name: str,
    description: str,
    fpga_ids: Sequence[str],
    links: Sequence[Mapping[str, Any]],
    device_template_path: Path,
    template_fpga_id: Optional[str] = None,
    provenance: Optional[Mapping[str, Any]] = None,
) -> Tuple[Platform, Platform, Dict[str, Any]]:
    """Populate an abstract graph with one selected homogeneous FPGA device."""
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("name: expected a non-empty string")
    if not fpga_ids or len(set(fpga_ids)) != len(fpga_ids):
        raise ValidationError("fpga_ids: expected distinct FPGA IDs")

    template_platform = Platform.load(device_template_path)
    template = read_json(device_template_path)
    raw_fpgas = template.get("fpgas")
    if not isinstance(raw_fpgas, list) or not raw_fpgas:
        raise ValidationError("device template has no FPGA records")
    by_id = {record.get("id"): record for record in raw_fpgas}
    if template_fpga_id is not None:
        if template_fpga_id not in by_id:
            raise ValidationError(
                f"device template has no FPGA {template_fpga_id!r}"
            )
        selected = by_id[template_fpga_id]
    else:
        selected = raw_fpgas[0]
        signature = (
            selected.get("part"),
            selected.get("utilization_limit"),
            selected.get("capacity"),
        )
        if any(
            (
                record.get("part"),
                record.get("utilization_limit"),
                record.get("capacity"),
            )
            != signature
            for record in raw_fpgas[1:]
        ):
            raise ValidationError(
                "device template is heterogeneous; select --template-fpga"
            )

    device = {
        "part": selected["part"],
        "utilization_limit": selected["utilization_limit"],
        "capacity": deepcopy(selected["capacity"]),
    }
    metadata: Dict[str, Any] = {
        "name": name,
        "kind": "virtual",
        "description": description,
    }
    provenance_record = deepcopy(dict(provenance or {}))
    provenance_record["device_template"] = {
        "path": str(device_template_path),
        "platform": template_platform.name,
        "fpga": selected["id"],
    }
    metadata["provenance"] = provenance_record
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": metadata,
        "fpgas": [{"id": fpga_id, **deepcopy(device)} for fpga_id in fpga_ids],
        "links": [deepcopy(dict(link)) for link in links],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, boarddb)
    validated = Platform.load(output_path)
    return validated, template_platform, selected
