"""Compact target-FPGA loading diagnostics; never a partition legality gate.

Counts and capacities must use the same resource units. Missing physical
measurements are unknown, not zero and not inferred from logical cell counts.
"""
from __future__ import annotations

import argparse
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Optional

from .errors import ValidationError
from .io import read_json, write_json
from .platform import Platform
from .resources import RESOURCE_FIELDS


def packed_logic_resources(architecture: Path, packed_netlist: Path) -> dict:
    """Count occupied built-in LUT/FF primitives, not pre-pack logical cells.

    Architecture-specific DSP/RAM macro units cannot be inferred from names or
    bit-slice atom counts. Leave those dimensions unmeasured.
    """
    models = {}
    arch_root = ET.parse(architecture).getroot()
    envelopes = set()
    layout = arch_root.find("layout")
    if layout is not None and len(layout) == 1 and layout[0].tag == "fixed_layout":
        from .fixed_device import _pb_resources

        def mark_lut_envelopes(node):
            if _pb_resources(node).get("lut") == 1:
                envelopes.add(node.get("name"))
            else:
                for child in list(node.findall("pb_type")) + list(node.findall("mode/pb_type")):
                    mark_lut_envelopes(child)

        for pb in arch_root.findall("complexblocklist/pb_type"):
            mark_lut_envelopes(pb)
    for element in arch_root.iter():
        if element.tag == "pb_type" and element.get("blif_model"):
            name, model = element.get("name"), element.get("blif_model")
            if name in models and models[name] != model:
                raise ValidationError("ambiguous architecture primitive model")
            models[name] = model
    counts = {field: 0 for model, field in ((".names", "lut"), (".latch", "ff"))
              if model in models.values()}
    stack = []
    for event, element in ET.iterparse(packed_netlist, events=("start", "end")):
        if element.tag != "block":
            continue
        if event == "start":
            if stack:
                stack[-1][1] = True
            stack.append([element.get("name") != "open" and (not stack or stack[-1][0]), False,
                          element.get("instance", "").split("[", 1)[0] in envelopes, False])
        else:
            active, has_children, envelope, lut_used = stack.pop()
            if active and not has_children:
                pb = element.get("instance", "").split("[", 1)[0]
                field = {".names": "lut", ".latch": "ff"}.get(models.get(pb))
                if field == "lut" and envelopes:
                    if envelope:
                        lut_used = True
                    else:
                        for parent in reversed(stack):
                            if parent[2]:
                                parent[3] = True
                                break
                        else:
                            raise ValidationError("packed LUT lacks a bound LUT6 physical envelope")
                elif field:
                    counts[field] += 1
            if active and envelope and lut_used:
                counts["lut"] += 1
            element.clear()
    return counts


def loading_policy(value: Optional[Mapping[str, Any]] = None) -> dict:
    policy = {"minimum": 0.4, "target_min": 0.6, "target_max": 0.8,
              "principal_resources": ["lut", "ff", "dsp", "bram"]}
    if value is not None:
        if set(value) - set(policy):
            raise ValidationError("unknown utilization policy field")
        policy.update(value)
    for key in ("minimum", "target_min", "target_max"):
        v = policy[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValidationError("utilization thresholds must be finite numbers")
    if not 0 <= policy["minimum"] <= policy["target_min"] <= policy["target_max"] <= 1:
        raise ValidationError("expected 0 <= minimum <= target_min <= target_max <= 1")
    fields = policy["principal_resources"]
    if (not isinstance(fields, list) or not fields
            or any(f not in RESOURCE_FIELDS for f in fields)
            or len(set(fields)) != len(fields)):
        raise ValidationError("principal_resources must be distinct resource names")
    return policy


def resource_loading(platform: Platform, records: list, *, measured: bool) -> dict:
    by_id = {}
    for row in records:
        fpga = row.get("fpga")
        if fpga in by_id or fpga not in {f.id for f in platform.fpgas}:
            raise ValidationError("duplicate or unknown FPGA in resource counts")
        by_id[fpga] = row
    per_fpga = []
    for fpga in platform.fpgas:
        raw = by_id.get(fpga.id, {}).get("resources")
        resources = {}
        for field in RESOURCE_FIELDS:
            # Sparse DUT vectors explicitly mean zero; physical measurements
            # must explicitly report each measured dimension, including zeros.
            used = None if raw is None else raw.get(field, None if measured else 0)
            if used is not None and (isinstance(used, bool) or not isinstance(used, int) or used < 0):
                raise ValidationError("resource counts must be nonnegative integers")
            capacity = fpga.capacity.get(field, 0)
            effective = fpga.effective_capacity.get(field, 0)
            if capacity or used:
                resources[field] = {
                    "used": used, "capacity": capacity, "effective_capacity": effective,
                    "utilization": used / capacity if used is not None and capacity else None,
                    "effective_utilization": used / effective if used is not None and effective else None,
                }
        per_fpga.append({"fpga": fpga.id, "resources": resources})
    totals = {}
    for field in RESOURCE_FIELDS:
        rows = [r["resources"].get(field, {"used": 0, "capacity": 0, "effective_capacity": 0}) for r in per_fpga]
        cap = sum(r["capacity"] for r in rows)
        eff = sum(r["effective_capacity"] for r in rows)
        used = None if any(r["used"] is None for r in rows) else sum(r["used"] for r in rows)
        if cap or used:
            totals[field] = {"used": used, "capacity": cap, "effective_capacity": eff,
                             "utilization": used / cap if used is not None and cap else None,
                             "effective_utilization": used / eff if used is not None and eff else None}
    return {"per_fpga": per_fpga, "total": totals}


def build_utilization_report(platform: Platform, phase3: Mapping[str, Any],
                             physical: Optional[Mapping[str, Any]] = None,
                             policy: Optional[Mapping[str, Any]] = None) -> dict:
    policy = loading_policy(policy)
    for report in (phase3, physical or {}):
        if report.get("platform", platform.name) != platform.name:
            raise ValidationError("resource report platform does not match BoardDB")
    dut = resource_loading(platform, phase3.get("partitions", []), measured=False)
    final = resource_loading(platform, (physical or {}).get("fpgas", []), measured=True)
    principal = {f: dut["total"].get(f, {}).get("utilization") for f in policy["principal_resources"]
                 if dut["total"].get(f, {}).get("capacity", 0) > 0}
    complete = bool(principal) and all(v is not None for v in principal.values())
    peak = max(principal.values()) if complete else None
    label = ("unknown" if peak is None else "low-load" if peak < policy["minimum"]
             else "below-target" if peak < policy["target_min"]
             else "target" if peak <= policy["target_max"] else "high-load")
    balance = {k: v for k, v in phase3.get("validation", {}).items()
               if k.startswith(("requested_balance", "effective_balance", "balance_auto", "balance_dimensions"))}
    return {"schema": "emuflow.resource-loading/v1", "platform": platform.name,
            "capacity_scope": ("fixed-physical-grid scalar bounds; exact packing checked by VPR"
                               if platform.physical_device else
                               "BoardDB resource units; physical-device equivalence not certified"),
            "policy": policy, "phase3_dut": dut, "phase7_final": final,
            "balance": balance,
            "comparison_contract": {"fpgas": [f.to_dict() for f in platform.fpgas],
                                    "physical_device": platform.physical_device,
                                    "requested_balance": {k: v for k, v in balance.items() if k.startswith("requested_")},
                                    "policy": policy},
            "qualification": {"class": label, "principal_utilization": peak,
                              "principal_resource": max(principal, key=principal.get) if complete else None,
                              "load_floor_met": complete and peak >= policy["minimum"],
                              "functional_legality_unchanged": True,
                              "scope": "DUT loading only; not timing or physical closure"}}


def compare_loading(reports: list) -> dict:
    if len(reports) < 2:
        raise ValidationError("loading comparison requires at least two arms")
    contract = reports[0]["comparison_contract"]
    if any(r["comparison_contract"] != contract for r in reports[1:]):
        raise ValidationError("comparison FPGA capacity, requested balance, or loading policy differs")
    if not contract["requested_balance"]:
        raise ValidationError("comparison is missing requested balance evidence")
    return {"status": "pass", "load_floor_met": all(r["qualification"]["load_floor_met"] for r in reports),
            "effective_balance_equal": all(r["balance"] == reports[0]["balance"] for r in reports)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", type=Path, required=True)
    parser.add_argument("--phase3-report", type=Path, required=True)
    parser.add_argument("--physical-summary", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--compare", type=Path, help="Prior resource-loading report")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_utilization_report(Platform.load(args.platform), read_json(args.phase3_report),
                                      read_json(args.physical_summary) if args.physical_summary else None,
                                      read_json(args.policy) if args.policy else None)
    if args.compare:
        result["comparison"] = compare_loading([read_json(args.compare), result])
    write_json(args.output, result, compact=True)


if __name__ == "__main__":
    main()
