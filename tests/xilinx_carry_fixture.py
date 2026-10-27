"""Small CARRY8/LUT6_2 device fixture for the maintained mixed atomic backend."""
import hashlib
import json
from pathlib import Path

from emuflow.common.io import read_json
from emuflow.physical.rapidwright.rapidwright_provider import validate_rapidwright_provider_manifest
from tests.xilinx_site_reference import pack_xilinx_sites

ROOT = Path(__file__).resolve().parents[1]
PINNED = ROOT / "resources/rapidwright/xcvu19p-fsva3824-2-e.provider.json"


def _cell(cell_type, connections):
    outputs = {"O", "O5", "O6", "Q", "CO"}
    return {
        "type": cell_type,
        "port_directions": {
            port: ("output" if port in outputs else "input")
            for port in connections
        },
        "connections": connections,
    }


def _add_carry(cells, name, *, ci, co_base, net_base):
    di = []
    select = []
    for index in range(8):
        di_net = net_base + 2 * index
        s_net = di_net + 1
        adapter = f"{name}$lut{index}"
        cells[adapter] = _cell("LUT6_2", {
            "I0": [net_base + 100 + 2 * index],
            "I1": [net_base + 101 + 2 * index],
            "I2": ["0"], "I3": ["0"], "I4": ["0"], "I5": ["1"],
            "O5": [di_net], "O6": [s_net],
        })
        di.append(di_net)
        select.append(s_net)
    cells[name] = _cell("CARRY8", {
        "CI": [ci], "CI_TOP": ["0"], "DI": di, "S": select,
        "CO": list(range(co_base, co_base + 8)),
        "O": list(range(co_base + 8, co_base + 16)),
    })


def _write_fixture(root):
    mapped = root / "mapped.json"
    packed = root / "packed.json"
    architecture = root / "architecture.json"
    provider = root / "provider.json"
    native = root / "native.json"
    cells = {}
    _add_carry(cells, "carry0", ci=1, co_base=10, net_base=1000)
    _add_carry(cells, "carry1", ci=17, co_base=40, net_base=2000)
    data_bits = [
        net_base + offset
        for net_base in (1100, 2100)
        for offset in range(16)
        if net_base + offset != 1100
    ]
    cells["src_ff"] = _cell("FDRE", {
        "C": [5000], "CE": ["1"], "D": ["0"], "Q": [1100], "R": ["0"],
    })
    cells["ff"] = _cell("FDRE", {
        "C": [5000], "CE": ["1"], "D": [48], "Q": [5001], "R": ["0"],
    })
    mapped.write_text(json.dumps({
        "modules": {"top": {
            "attributes": {"top": "1"},
            "ports": {
                "clk": {"direction": "input", "bits": [5000]},
                "data": {"direction": "input", "bits": data_bits},
            },
            "netnames": {
                "clk": {"hide_name": 0, "bits": [5000]},
                "data": {"hide_name": 0, "bits": data_bits},
            },
            "cells": cells,
        }},
    }), encoding="utf-8")
    pack_xilinx_sites(mapped, packed, top="top")

    lut_bels = [{
        "name": f"{letter}6LUT", "type": "LUT6", "z": 2 * index + 1,
        "compatible_cells": [*[f"LUT{width}" for width in range(1, 7)], "LUT6_2"],
    } for index, letter in enumerate("ABCDEFGH")]
    ff_bels = [{
        "name": name, "type": "FF", "z": index,
        "compatible_cells": ["FDCE", "FDPE", "FDRE", "FDSE"],
    } for index, name in enumerate(
        name for letter in "ABCDEFGH" for name in (f"{letter}FF", f"{letter}FF2")
    )]
    carry_bel = {
        "name": "CARRY8", "type": "CARRY8", "z": 0,
        "compatible_cells": ["CARRY8"],
    }
    sites = []
    for x in range(4):
        for y in range(4):
            sites.append({
                "name": f"SLICE_X{x}Y{y}", "type": "SLICEL", "template": "SLICEL",
                # Match real UltraScale+: tile rows increase top-to-bottom,
                # opposite the directed bottom-to-top CARRY_NEXT site order.
                "x": x, "y": y,
                "tile": {"grid_col": x, "grid_row": 3 - y},
                "physical_region": {"slr": "SLR0", "clock_region": "X0Y0"},
            })
    architecture.write_text(json.dumps({
        "schema": "emuflow.archdb/v1",
        "part": "xcvu19p-fsva3824-2-e",
        "source": {"format": "unit-test/v1"},
        "policy": {"name": "carry8-native-test"},
        "site_templates": {
            "SLICEL": {
                "bels": [*lut_bels, *ff_bels, carry_bel],
                "alternative_templates": [],
            },
        },
        "sites": sites,
    }, sort_keys=True), encoding="utf-8")
    provider.write_text(PINNED.read_text(encoding="utf-8"), encoding="utf-8")
    checked = validate_rapidwright_provider_manifest(read_json(provider))
    chains = [[f"SLICE_X{x}Y{y}" for y in range(4)] for x in range(4)]
    payload = {
        "bram_tile_groups": [],
        "capabilities": {
            "bram_tile_groups": "native_supported",
            "clock_region_site_capacity": "native_supported",
            "dedicated_adjacency.BRAM_CASCADE": "core_missing",
            "dedicated_adjacency.CARRY_NEXT": "native_supported",
            "dedicated_adjacency.DSP_CASCADE": "core_missing",
            "dedicated_adjacency.URAM_CASCADE": "core_missing",
            "half_column_clock_capacity": "unverified",
            "slr_site_capacity": "native_supported",
        },
        "dedicated_adjacency": [{
            "chains": chains,
            "edge_count": 12,
            "endpoint_contract": "carry8-co7-ci-all-v1",
            "kind": "CARRY_NEXT",
            "native_proof_sha256": "b" * 64,
            "proof_method": (
                "rapidwright-dedicated-sitepin-vector-directed-path-v2"
            ),
        }],
        "site_capacity": [{
            "clock_region": "X0Y0", "site_type": "SLICEL", "sites": 16,
            "slr": "SLR0",
        }],
        "source": {
            "architecture_sha256": hashlib.sha256(architecture.read_bytes()).hexdigest(),
            "device": checked["device_identity"]["device"],
            "device_database_md5": checked["device_database_md5"],
            "full_part": checked["part"],
            "generator": {"revision": checked["revision"], "version": checked["version"]},
            "provider_manifest_sha256": hashlib.sha256(provider.read_bytes()).hexdigest(),
            "route_backend": "rapidwright-native-device-database-v1",
        },
        "summary": {
            "bram_tile_groups": 0, "capacity_buckets": 1,
            "clock_regions": 1, "dedicated_edges": 12,
            "dedicated_edges_by_kind": {
                "BRAM_CASCADE": 0, "CARRY_NEXT": 12,
                "DSP_CASCADE": 0, "URAM_CASCADE": 0,
            },
            "sites": 16, "slrs": 1,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    native.write_text(json.dumps({
        "schema": "emuflow.xilinx-native-device-constraints/v3",
        "payload": payload,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
    }, sort_keys=True), encoding="utf-8")
    return mapped, packed, architecture, native, provider
