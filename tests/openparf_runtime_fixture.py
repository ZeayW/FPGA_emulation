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


def write_openparf_runtime_fixture(
    root: Path, *, include_hard: bool = False
) -> Tuple[Path, Path, Path]:
    """Write a 64-LUT/64-FF, 4x4-slice runtime smoke fixture.

    ``include_hard`` inserts independent DSP48E2, RAMB36E2, and URAM288
    instances into three existing FF-to-LUT ring edges.  No disconnected or
    synthetic load-only net is added.
    """

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
    hard_insertions = {
        1: ("dsp", "DSP48E2", "A", "P", 30_000),
        2: ("bram", "RAMB36E2", "ADDRARDADDR", "DOADO", 30_001),
        3: ("uram", "URAM288", "ADDR_A", "DOUT_A", 30_002),
    } if include_hard else {}
    for index in range(64):
        lut_name = f"lut_{index:02d}"
        ff_name = f"ff_{index:02d}"
        previous_ff_index = (index - 1) % 64
        previous_ff_bit = 20_000 + previous_ff_index
        if index in hard_insertions:
            previous_ff_bit = hard_insertions[index][4]
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

    if include_hard:
        for insertion_index, (
            instance, primitive, input_port, output_port, output_bit
        ) in hard_insertions.items():
            input_bit = 20_000 + ((insertion_index - 1) % 64)
            cells[instance] = _cell(
                primitive,
                {input_port: [input_bit], output_port: [output_bit]},
                {output_port},
            )
            clusters.append({
                "id": instance,
                "kind": "hard",
                "site_templates": [primitive],
                "control_set": None,
                "assignments": [{
                    "instance": instance,
                    "cell_type": primitive,
                    "bel": primitive,
                    "bel_candidates": [primitive],
                }],
            })

    mapped_path.write_text(json.dumps({
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {
                "clk": {"direction": "input", "bits": [clock_bit]},
            },
            "netnames": {
                "clk": {"hide_name": 0, "bits": [clock_bit]},
            },
            "cells": cells,
        }},
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
    site_templates = {"SLICEL": {
        "bels": [*lut_bels, *ff_bels], "alternative_templates": [],
    }}
    if include_hard:
        for resource_index, primitive in enumerate(
            ("DSP48E2", "RAMB36E2", "URAM288"), start=0
        ):
            site_templates[primitive] = {
                "bels": [{
                    "name": primitive,
                    "type": primitive,
                    "z": 0,
                    "compatible_cells": [primitive],
                    "placement_mode": primitive,
                }],
                "alternative_templates": [],
            }
            for site_index in range(2):
                x = 4 + 2 * resource_index + site_index
                sites.append({
                    "name": f"{primitive}_X{site_index}Y0",
                    "type": primitive,
                    "template": primitive,
                    "x": x,
                    "y": 0,
                    "tile": {"grid_col": x, "grid_row": 0},
                })
    architecture_path.write_text(json.dumps({
        "schema": "emuflow.archdb/v1",
        "part": "openparf-runtime-fixture",
        "source": {"format": "test/v1"},
        "policy": {"name": "test-runtime-smoke"},
        "site_templates": site_templates,
        "sites": sites,
    }), encoding="utf-8")
    return mapped_path, packed_path, architecture_path


def write_openparf_ramb18_fixture(root: Path) -> Tuple[Path, Path, Path]:
    """Write two connected RAMB18E2 cells packed by the production packer."""

    mapped_path, packed_path, architecture_path = write_openparf_runtime_fixture(
        root, include_hard=False
    )
    mapped = json.loads(mapped_path.read_text(encoding="utf-8"))
    cells = mapped["modules"]["top"]["cells"]
    for index, (name, sink) in enumerate(
        (("ramb18_lo", "lut_01"), ("ramb18_hi", "lut_02"))
    ):
        input_bit = cells[sink]["connections"]["I0"][0]
        output_bit = 40_000 + index
        cells[name] = _cell(
            "RAMB18E2",
            {
                # Keep the production primitive's real bus widths.  A
                # one-element connection would describe a scalar logical
                # port named ADDRARDADDR/DOADO, which does not exist on the
                # RapidWright RAMB18E2 Unisim and therefore is not a valid
                # physical-routing fixture.
                "ADDRARDADDR": [input_bit] + ["0"] * 13,
                "CLKARDCLK": [1_000_000],
                "DOADO": [output_bit] + ["0"] * 15,
            },
            {"DOADO"},
        )
        cells[sink]["connections"]["I0"] = [output_bit]
    mapped_path.write_text(json.dumps(mapped), encoding="utf-8")

    architecture = json.loads(architecture_path.read_text(encoding="utf-8"))
    architecture["site_templates"].update({
        "RAMB180": {
            "bels": [{
                "name": "RAMB18E2_L", "type": "RAMB18E2", "z": 0,
                "compatible_cells": ["RAMB18E2"],
                "placement_mode": "RAMB180",
            }],
            "alternative_templates": [],
        },
        "RAMB181": {
            "bels": [{
                "name": "RAMB18E2_U", "type": "RAMB18E2", "z": 0,
                "compatible_cells": ["RAMB18E2"],
                "placement_mode": "RAMB181",
            }],
            "alternative_templates": ["RAMB180", "RAMB36"],
        },
        "RAMB36": {
            "bels": [{
                "name": "RAMB36E2", "type": "RAMB36E2", "z": 0,
                "compatible_cells": ["RAMB36E2"],
                "placement_mode": "RAMB36",
            }],
            "alternative_templates": [],
        },
    })
    for index in range(2):
        architecture["sites"].append({
            "name": f"RAMB18_X{index}Y1", "type": "RAMB181",
            "template": "RAMB181", "x": 30 + 4 * index, "y": 1,
            "tile": {
                "grid_col": 30 + 4 * index, "grid_row": 4,
                "site_index": 0,
            },
        })
    architecture_path.write_text(json.dumps(architecture), encoding="utf-8")

    from emuflow.xilinx_packing import pack_xilinx_sites

    pack_xilinx_sites(mapped_path, packed_path, top="top")
    return mapped_path, packed_path, architecture_path


_HARDBLOCK_CASCADE_SPECS = {
    "DSP48E2": {
        "input": ("A", 30),
        "output": ("P", 48),
        "cascade": ("ACOUT", "ACIN", 30),
        "clock": None,
    },
    "RAMB36E2": {
        "input": ("ADDRARDADDR", 15),
        "output": ("DOUTADOUT", 32),
        "cascade": ("CASDOUTA", "CASDINA", 32),
        "clock": "CLKARDCLK",
    },
    "URAM288": {
        "input": ("ADDR_A", 23),
        "output": ("DOUT_A", 72),
        "cascade": ("CAS_OUT_DOUT_A", "CAS_IN_DOUT_A", 72),
        "clock": "CLK",
    },
}


def write_openparf_hardblock_cascade_fixture(
    root: Path, primitive: str
) -> Tuple[Path, Path, Path]:
    """Write one real-width two-instance hard-block cascade fixture.

    The hard blocks replace two FF-to-LUT ring edges so their ordinary data
    ports remain connected to real logic. Every dedicated-cascade lane is an
    integer net; constants never pad a cascade bus. The real Xilinx packer,
    rather than this fixture, produces the packed clusters and chain contract.
    """

    try:
        spec = _HARDBLOCK_CASCADE_SPECS[primitive]
    except KeyError as exc:
        raise ValueError(
            f"unsupported hard-block cascade primitive {primitive!r}"
        ) from exc

    mapped_path, packed_path, architecture_path = write_openparf_runtime_fixture(root)
    mapped = json.loads(mapped_path.read_text(encoding="utf-8"))
    module = mapped["modules"]["top"]
    cells = module["cells"]
    clock_bit = module["ports"]["clk"]["bits"][0]
    input_port, input_width = spec["input"]
    output_port, output_width = spec["output"]
    cascade_output, cascade_input, cascade_width = spec["cascade"]
    cascade_bits = list(range(40_000, 40_000 + cascade_width))

    for ordinal, (name, ring_index) in enumerate(
        (("hardblock_head", 1), ("hardblock_tail", 4))
    ):
        input_bits = [20_000 + ring_index - 1, *(["0"] * (input_width - 1))]
        output_base = 30_000 + ordinal * 1_000
        output_bits = list(range(output_base, output_base + output_width))
        connections = {input_port: input_bits, output_port: output_bits}
        directions = {input_port: "input", output_port: "output"}
        if ordinal == 0:
            connections[cascade_output] = cascade_bits
            directions[cascade_output] = "output"
        else:
            connections[cascade_input] = cascade_bits
            directions[cascade_input] = "input"
        if spec["clock"] is not None:
            connections[spec["clock"]] = [clock_bit]
            directions[spec["clock"]] = "input"
        cells[name] = {
            "type": primitive,
            "port_directions": directions,
            "connections": connections,
        }
        cells[f"lut_{ring_index:02d}"]["connections"]["I0"] = [output_bits[0]]

    mapped_path.write_text(json.dumps(mapped), encoding="utf-8")

    architecture = json.loads(architecture_path.read_text(encoding="utf-8"))
    architecture["site_templates"][primitive] = {
        "bels": [{
            "name": primitive,
            "type": primitive,
            "z": 0,
            "compatible_cells": [primitive],
            "placement_mode": primitive,
        }],
        "alternative_templates": [],
    }
    for site_index in range(2):
        x = 4 + site_index
        architecture["sites"].append({
            "name": f"{primitive}_X0Y{site_index}",
            "type": primitive,
            "template": primitive,
            "x": x,
            "y": site_index,
            "tile": {"grid_col": x, "grid_row": site_index},
        })
    architecture_path.write_text(json.dumps(architecture), encoding="utf-8")

    from emuflow.xilinx_packing import pack_xilinx_sites

    pack_xilinx_sites(mapped_path, packed_path, top="top")
    return mapped_path, packed_path, architecture_path
