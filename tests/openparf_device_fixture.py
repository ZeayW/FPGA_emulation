"""Source-sealed tiny device facts for native input qualification."""

from collections import Counter
import hashlib
import json
from pathlib import Path
from tests.test_xilinx_native_device_constraints import _artifact, _reseal, PINNED


def seal_native_input(architecture_path, native_path, provider_path, supplied):
    architecture = json.loads(architecture_path.read_text())
    provider_path.write_bytes(PINNED.read_bytes())
    architecture["part"] = json.loads(PINNED.read_text())["device"]["part"]
    for site in architecture["sites"]:
        site.setdefault("physical_region", {"slr": "SLR0", "clock_region": "X0Y0"})
    for template in architecture.get("site_templates", {}).values():
        for bel in template["bels"]:
            if bel["name"] == "DSP48E2":
                bel["name"] = "DSP_ALU"
            if bel["name"] == "URAM288":
                bel["name"] = "URAM_288K_INST"
    for template in architecture.get("site_templates", {}).values():
        if not any(
            t.get("alternative_templates")
            for t in architecture.get("site_templates", {}).values()
        ) and any("RAMB36E2" in b["compatible_cells"] for b in template["bels"]):
            existing = {b["name"] for b in template["bels"]}
            for role in ("RAMB18E2_L", "RAMB18E2_U"):
                if role not in existing:
                    template["bels"].append(
                        {
                            "name": role,
                            "type": "RAMB18E2",
                            "z": len(template["bels"]),
                            "compatible_cells": ["RAMB18E2"],
                        }
                    )
    architecture_path.write_text(json.dumps(architecture))
    native = _artifact(architecture_path, provider_path)
    payload = native["payload"]
    groups = supplied["payload"].get("bram_tile_groups", [])
    for group in groups:
        group["upper"]["site"] = group["anchor"]
        for index, role in enumerate(("lower", "upper", "whole")):
            view = group[role]
            view["site_index"] = index
            view["native_site_type"] = {
                "lower": "RAMBFIFO18",
                "upper": "RAMB181",
                "whole": "RAMBFIFO36",
            }[role]
            view["native_bel"] = {
                "lower": "RAMBFIFO18",
                "upper": "RAMB18E2_U",
                "whole": "RAMBFIFO36E2",
            }[role]
    families = supplied["payload"].get("dedicated_adjacency", [])
    contracts = {
        "CARRY_NEXT": "carry8-co7-ci-all-v1",
        "DSP_CASCADE": "dsp48e2-all-cascade-sitepins-v1",
        "BRAM_CASCADE": "ramb36e2-72-data-parity-2-ecc-cascade-sitepins-v2",
        "URAM_CASCADE": "uram288-all-cascade-sitepins-v1",
    }
    for family in families:
        family.update(
            edge_count=sum(len(c) - 1 for c in family["chains"]),
            endpoint_contract=contracts[family["kind"]],
            native_proof_sha256="b" * 64,
            proof_method="rapidwright-dedicated-sitepin-vector-directed-path-v2",
        )
    counts = Counter(
        (s["physical_region"]["slr"], s["physical_region"]["clock_region"], s["type"])
        for s in architecture["sites"]
    )
    payload["site_capacity"] = [
        {"slr": slr, "clock_region": clock, "site_type": kind, "sites": count}
        for (slr, clock, kind), count in sorted(counts.items())
    ]
    payload["bram_tile_groups"] = groups
    payload["dedicated_adjacency"] = families
    for kind in contracts:
        payload["capabilities"]["dedicated_adjacency." + kind] = (
            "native_supported"
            if any(f["kind"] == kind for f in families)
            else "core_missing"
        )
    edges = {
        kind: sum(f["edge_count"] for f in families if f["kind"] == kind)
        for kind in sorted(contracts)
    }
    payload["summary"] = {
        "bram_tile_groups": len(groups),
        "capacity_buckets": len(counts),
        "clock_regions": len(
            {s["physical_region"]["clock_region"] for s in architecture["sites"]}
        ),
        "dedicated_edges": sum(edges.values()),
        "dedicated_edges_by_kind": edges,
        "sites": len(architecture["sites"]),
        "slrs": len({s["physical_region"]["slr"] for s in architecture["sites"]}),
    }
    _reseal(native)
    native_path.write_text(json.dumps(native))
    return native
