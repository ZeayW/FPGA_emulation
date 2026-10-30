"""Primitive-library facts and validation; circuit normalization is native."""
from __future__ import annotations
from emuflow.common.paths import DATA_ROOT
from pathlib import Path
from typing import Any, Dict, Mapping
from emuflow.common.errors import ValidationError
from emuflow.common.io import read_json
from emuflow.ir.resources import ResourceVector, classify_primitive_resources

XILINX_PRIMITIVE_LIBRARY_SCHEMA = (
    "emuflow.xilinx-primitive-library-db/v1"
)

XILINX_ULTRASCALEPLUS_OPEN_PROFILE = "xilinx-ultrascaleplus-open-v1"

DEFAULT_XILINX_PRIMITIVE_LIBRARY = (
    DATA_ROOT
    / "rapidwright"
    / "xilinx-ultrascaleplus-open-v1.primitives.json"
)

def validate_xilinx_primitive_library(
    value: Mapping[str, Any],
) -> Dict[str, Any]:
    if value.get("schema") != XILINX_PRIMITIVE_LIBRARY_SCHEMA:
        raise ValidationError("Xilinx primitive library schema is invalid")
    if value.get("mapping_profile") != XILINX_ULTRASCALEPLUS_OPEN_PROFILE:
        raise ValidationError("Xilinx primitive mapping profile is invalid")
    if value.get("family") != "UltraScale+":
        raise ValidationError("Xilinx primitive library family is invalid")
    if value.get("unknown_cell_policy") != "fail":
        raise ValidationError("Xilinx primitive library must fail unknown cells")
    cells = value.get("cells")
    if not isinstance(cells, dict) or not cells:
        raise ValidationError("Xilinx primitive library cells are missing")
    normalized: Dict[str, Any] = {}
    for name, contract in sorted(cells.items()):
        if not isinstance(name, str) or not name or not isinstance(contract, dict):
            raise ValidationError("Xilinx primitive cell contract is invalid")
        category = contract.get("category")
        if not isinstance(category, str) or not category:
            raise ValidationError(f"primitive {name!r} category is invalid")
        for field in ("hard_block", "placement_required"):
            if not isinstance(contract.get(field), bool):
                raise ValidationError(
                    f"primitive {name!r} field {field!r} is invalid"
                )
        resources = contract.get("resources")
        if not isinstance(resources, dict):
            raise ValidationError(f"primitive {name!r} resources are invalid")
        resource_vector = ResourceVector.from_mapping(
            resources, f"primitive {name!r} resources"
        )
        classified = classify_primitive_resources(name)
        if resource_vector != classified:
            raise ValidationError(
                f"primitive {name!r} resource contract disagrees with EmuIR"
            )
        normalized[name] = {
            "category": category,
            "hard_block": contract["hard_block"],
            "placement_required": contract["placement_required"],
            "resources": resource_vector.to_dict(include_zeros=False),
        }
    return {
        "status": "pass",
        "schema": XILINX_PRIMITIVE_LIBRARY_SCHEMA,
        "library_id": value.get("library_id"),
        "mapping_profile": XILINX_ULTRASCALEPLUS_OPEN_PROFILE,
        "cells": normalized,
    }

def load_xilinx_primitive_library(
    path: Path = DEFAULT_XILINX_PRIMITIVE_LIBRARY,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValidationError("Xilinx primitive library must be an object")
    return value, validate_xilinx_primitive_library(value)
