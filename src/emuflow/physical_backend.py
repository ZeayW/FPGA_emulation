"""Provider-neutral physical implementation contract.

The contract deliberately describes observable implementation results rather
than provider-native files.  OpenPARF/VPR and Vivado may expose different
internal stages, but both must account for the same partition cells, clocks,
routing closure, DRC closure, and timing domains before Phase 7C consumes the
result.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping

from .errors import ValidationError


PHYSICAL_BACKEND_SCHEMA = "emuflow.physical-backend/v1"
PHYSICAL_PARTITION_RESULT_SCHEMA = (
    "emuflow.physical-partition-result/v1"
)
PHYSICAL_BACKENDS = ("open", "vivado")


_DESCRIPTORS: Dict[str, Dict[str, Any]] = {
    "open": {
        "schema": PHYSICAL_BACKEND_SCHEMA,
        "id": "open",
        "implementation_engine": "vpr-openparf-vpr",
        "timing_engine": "vpr",
        "architecture_class": "public-academic",
        "source_model": "source-complete",
        "qualification": "academic-architecture-not-vendor-signoff",
        "capabilities": {
            "packing": True,
            "placement": True,
            "routing": True,
            "timing": True,
            "bitstream": False,
        },
    },
    "vivado": {
        "schema": PHYSICAL_BACKEND_SCHEMA,
        "id": "vivado",
        "implementation_engine": "vivado",
        "timing_engine": "vivado",
        "architecture_class": "xilinx-commercial-device",
        "source_model": "external-proprietary-provider",
        "qualification": "vendor-device-implementation-not-board-signoff",
        "capabilities": {
            "packing": True,
            "placement": True,
            "routing": True,
            "timing": True,
            "bitstream": False,
        },
    },
}


def physical_backend_descriptor(name: str) -> Dict[str, Any]:
    try:
        descriptor = _DESCRIPTORS[name]
    except KeyError as error:
        raise ValidationError(
            f"physical backend must be one of {list(PHYSICAL_BACKENDS)}"
        ) from error
    return {
        **descriptor,
        "capabilities": dict(descriptor["capabilities"]),
    }


def validate_physical_backend_descriptor(
    descriptor: Mapping[str, Any],
) -> Dict[str, Any]:
    backend_id = descriptor.get("id")
    expected = physical_backend_descriptor(str(backend_id))
    if dict(descriptor) != expected:
        raise ValidationError(
            f"physical backend descriptor for {backend_id!r} is invalid"
        )
    return {
        "status": "pass",
        "backend": backend_id,
        "implementation_engine": expected["implementation_engine"],
        "timing_engine": expected["timing_engine"],
    }


def validate_physical_partition_result(
    result: Mapping[str, Any],
    *,
    backend: str,
    fpga: str,
    part: str,
    original_cells: int,
    transport_cells: int,
) -> Dict[str, Any]:
    if result.get("schema") != PHYSICAL_PARTITION_RESULT_SCHEMA:
        raise ValidationError(
            f"physical result for {fpga} has an invalid schema"
        )
    if result.get("status") != "pass":
        raise ValidationError(f"physical result for {fpga} did not pass")
    identity = result.get("identity")
    expected_identity = {
        "backend": backend,
        "fpga": fpga,
        "part": part,
    }
    if identity != expected_identity:
        raise ValidationError(
            f"physical result identity for {fpga} is inconsistent"
        )
    accounting = result.get("cell_accounting")
    if not isinstance(accounting, dict):
        raise ValidationError(
            f"physical result for {fpga} has no cell accounting"
        )
    expected_routed = original_cells + transport_cells
    if accounting.get("original_cells") != original_cells:
        raise ValidationError(
            f"physical result original cells for {fpga} disagree"
        )
    if accounting.get("transport_cells") != transport_cells:
        raise ValidationError(
            f"physical result transport cells for {fpga} disagree"
        )
    if accounting.get("routed_cells") != expected_routed:
        raise ValidationError(
            f"physical result routed cells for {fpga} disagree"
        )
    infrastructure = accounting.get("infrastructure_cells")
    optimization = accounting.get("optimization_cells")
    physical_cells = accounting.get("physical_cells")
    if (
        isinstance(infrastructure, bool)
        or not isinstance(infrastructure, int)
        or infrastructure < 0
        or isinstance(optimization, bool)
        or not isinstance(optimization, int)
        or optimization < 0
        or physical_cells
        != expected_routed + infrastructure + optimization
    ):
        raise ValidationError(
            f"physical result implementation cells for {fpga} disagree"
        )
    closure = result.get("closure")
    if not isinstance(closure, dict):
        raise ValidationError(f"physical result for {fpga} has no closure")
    if closure.get("unrouted_nets") != 0 or closure.get("drc_violations") != 0:
        raise ValidationError(f"physical result for {fpga} did not close")
    hard_resources = result.get("hard_resources")
    if hard_resources is not None:
        if not isinstance(hard_resources, dict) or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in hard_resources.values()
        ):
            raise ValidationError(
                f"physical result hard resources for {fpga} are invalid"
            )
    timing = result.get("timing")
    if not isinstance(timing, dict):
        raise ValidationError(f"physical result for {fpga} has no timing")
    for field in (
        "wns_ns",
        "dut_wns_ns",
        "fabric_wns_ns",
        "fabric_to_dut_wns_ns",
    ):
        value = timing.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                f"physical result {fpga}.{field} must be numeric"
            )
        if not math.isfinite(float(value)):
            raise ValidationError(
                f"physical result {fpga}.{field} must be finite"
            )
    presence = timing.get("clock_domain_presence")
    if presence is not None and (
        not isinstance(presence, dict)
        or set(presence) != {"fabric", "dut", "cross"}
        or any(not isinstance(value, bool) for value in presence.values())
        or not presence["fabric"]
        or presence["cross"] != presence["dut"]
    ):
        raise ValidationError(
            f"physical result {fpga}.clock_domain_presence is invalid"
        )
    domain_delays = timing.get("clock_domain_delays_ns")
    if presence is not None:
        if not isinstance(domain_delays, dict):
            raise ValidationError(
                f"physical result {fpga} lacks clock-domain delays"
            )
        for domain in ("overall", "fabric", "dut", "cross"):
            value = domain_delays.get(domain)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
            ):
                raise ValidationError(
                    f"physical result {fpga}.{domain} clock delay is invalid"
                )
        if not presence["dut"] and (
            float(domain_delays["dut"]) != 0.0
            or float(domain_delays["cross"]) != 0.0
        ):
            raise ValidationError(
                f"physical result {fpga} assigns delay to an absent DUT clock"
            )
    endpoint_timing = {
        "tns_ns": timing.get("tns_ns"),
        "failing_endpoints": timing.get("failing_endpoints"),
        "failing_endpoint_constraints": timing.get(
            "failing_endpoint_constraints"
        ),
    }
    if backend == "open" or any(value is not None for value in endpoint_timing.values()):
        tns = endpoint_timing["tns_ns"]
        failing_endpoints = endpoint_timing["failing_endpoints"]
        failing_constraints = endpoint_timing["failing_endpoint_constraints"]
        if (
            isinstance(tns, bool)
            or not isinstance(tns, (int, float))
            or not math.isfinite(float(tns))
            or float(tns) > 0
        ):
            raise ValidationError(f"physical result {fpga}.tns_ns is invalid")
        for name, value in (
            ("failing_endpoints", failing_endpoints),
            ("failing_endpoint_constraints", failing_constraints),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"physical result {fpga}.{name} is invalid")
        if failing_endpoints > failing_constraints:
            raise ValidationError(
                f"physical result {fpga} failing endpoint counts disagree"
            )
        if (float(tns) < 0) != (failing_constraints > 0):
            raise ValidationError(
                f"physical result {fpga} TNS and failing endpoint count disagree"
            )
    timing_met = timing.get("timing_met")
    expected_timing_met = float(timing["wns_ns"]) >= 0
    if timing_met is not expected_timing_met:
        raise ValidationError(f"physical result {fpga}.timing_met disagrees")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValidationError(
            f"physical result for {fpga} has no bound artifacts"
        )
    return {
        "status": "pass",
        "backend": backend,
        "fpga": fpga,
        "part": part,
        "routed_cells": expected_routed,
        "physical_cells": physical_cells,
        "wns_ns": float(timing["wns_ns"]),
        **(
            {
                "tns_ns": float(timing["tns_ns"]),
                "failing_endpoints": timing["failing_endpoints"],
            }
            if "tns_ns" in timing
            else {}
        ),
    }


def physical_summary_item(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Project a provider result onto the existing Phase-7B interface."""
    identity = result["identity"]
    accounting = result["cell_accounting"]
    timing = result["timing"]
    clocks = result["clocks"]
    return {
        "fpga": identity["fpga"],
        **({"device_grid": result["device_grid"]} if "device_grid" in result else {}),
        **({"resources": dict(result["resources"]),
            "resource_measurement": result.get("resource_measurement")}
           if "resources" in result else {}),
        "backend": identity["backend"],
        **accounting,
        "unrouted_nets": result["closure"]["unrouted_nets"],
        "drc_violations": result["closure"]["drc_violations"],
        **(
            {"drc_warnings": result["closure"]["drc_warnings"]}
            if "drc_warnings" in result["closure"]
            else {}
        ),
        "wns_ns": timing["wns_ns"],
        "timing": {
            "dut_wns_ns": timing["dut_wns_ns"],
            "fabric_wns_ns": timing["fabric_wns_ns"],
            "fabric_to_dut_wns_ns": timing[
                "fabric_to_dut_wns_ns"
            ],
            "timing_met": timing["timing_met"],
            **(
                {
                    "tns_ns": timing["tns_ns"],
                    "failing_endpoints": timing["failing_endpoints"],
                    "failing_endpoint_constraints": timing[
                        "failing_endpoint_constraints"
                    ],
                }
                if "tns_ns" in timing
                else {}
            ),
        },
        "clocks": clocks,
        **(
            {"critical_path_ns": timing["critical_path_ns"]}
            if "critical_path_ns" in timing
            else {}
        ),
        **(
            {"clock_domain_delays_ns": timing["clock_domain_delays_ns"]}
            if "clock_domain_delays_ns" in timing
            else {}
        ),
        **(
            {"clock_domain_presence": timing["clock_domain_presence"]}
            if "clock_domain_presence" in timing
            else {}
        ),
        **(
            {"hard_resources": dict(result["hard_resources"])}
            if "hard_resources" in result
            else {}
        ),
    }
