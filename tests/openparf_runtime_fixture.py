"""Deterministic, non-degenerate fixture for native OpenPARF runtime smoke.

This fixture is deliberately larger than the minimal unit-test fixture so the
nonlinear global placer has meaningful density and wirelength objectives.  It
is test-only input generation, not a production benchmark or fallback path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple


def _cell(cell_type, connections, outputs):
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def write_openparf_runtime_fixture(root: Path) -> Tuple[Path, Path, Path]:
    """Write a 64-LUT/64-FF, 4x4-slice runtime smoke fixture."""

    root.mkdir(parents=True, exist_ok=True)
    mapped_path = root / "mapped.json"
    packed_path = root / "packed.json"
    architecture_path = root / "architecture.json"

    # Each LUT drives one FF and each FF drives the next LUT.  The shared clock
    # is a primary-input-style fanout net.  Therefore every emitted net has at
    # least two endpoints and every data net has exactly one driver.
    cells = {}
    clusters = []
    clock_bit = 1_000_000
    for index in range(64):
        lut_name = f"lut_{index:02d}"
        ff_name = f"ff_{index:02d}"
        previous_ff_bit = 20_000 + ((index - 1) % 64)
        lut_bit = 10_000 + index
        ff_bit = 20_000 + index
        cells[lut_name] = _cell(
            "LUT6",
            {
                "I0": [previous_ff_bit],
                "I1": ["0"], "I2": ["0"], "I3": ["0"],
                "I4": ["0"], "I5": ["0"], "O": [lut_bit],
            },
            {"O"},
        )
        cells[ff_name] = _cell(
            "FDRE",
            {
                "C": [clock_bit], "CE": ["1"], "D": [lut_bit],
                "Q": [ff_bit], "R": ["0"],
            },
            {"Q"},
        )
        clusters.append({
            "id": f"pair_{index:02d}",
            "kind": "slice",
            "site_templates": ["SLICEL"],
            "control_set": "shared-clock",
            "assignments": [
                {"instance": lut_name, "cell_type": "LUT6", "bel": "A6LUT"},
                {"instance": ff_name, "cell_type": "FDRE", "bel": "AFF"},
            ],
        })

    mapped_path.write_text(json.dumps({
        "modules": {"top": {"attributes": {"top": "1"}, "cells": cells}},
    }), encoding="utf-8")
    packed_path.write_text(json.dumps({
        "schema": "emuflow.packed-site-netlist/v1",
        "top": "top",
        "clusters": clusters,
        "cascade_chains": [],
    }), encoding="utf-8")

    lut_bels = [
        {
            "name": f"{letter}6LUT", "type": "LUT6", "z": index,
            "compatible_cells": [f"LUT{width}" for width in range(1, 7)],
        }
        for index, letter in enumerate("ABCDEFGH")
    ]
    ff_bels = [
        {
            "name": name, "type": "FF", "z": index,
            "compatible_cells": ["FDCE", "FDPE", "FDRE", "FDSE"],
        }
        for index, name in enumerate(
            name for letter in "ABCDEFGH"
            for name in (f"{letter}FF", f"{letter}FF2")
        )
    ]
    sites = [
        {
            "name": f"SLICE_X{x}Y{y}", "type": "SLICEL",
            "template": "SLICEL", "x": x, "y": y,
            "tile": {"grid_col": x, "grid_row": y},
        }
        for x in range(4)
        for y in range(4)
    ]
    architecture_path.write_text(json.dumps({
        "schema": "emuflow.archdb/v1",
        "part": "openparf-runtime-fixture",
        "source": {"format": "test/v1"},
        "policy": {"name": "test-runtime-smoke"},
        "site_templates": {"SLICEL": {
            "bels": [*lut_bels, *ff_bels], "alternative_templates": [],
        }},
        "sites": sites,
    }), encoding="utf-8")
    return mapped_path, packed_path, architecture_path

