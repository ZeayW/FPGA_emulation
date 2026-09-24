"""Deterministic comparison of fail-closed Xilinx placer candidates.

Capability is only the first promotion gate.  This module deliberately does
not choose a default provider: a capable provider still needs the same routed
Phase 7 and global OpenSTA QoR experiment as every other candidate.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, Mapping, Sequence

from .errors import ValidationError
from .xilinx_placer_capability import (
    qualify_xilinx_placer_capabilities,
    validate_xilinx_placer_capability_report,
)


XILINX_PLACER_COMPARISON_SCHEMA = "emuflow.xilinx-placer-comparison/v1"


def compare_xilinx_placer_candidates(
    reports: Sequence[Mapping[str, Any]],
    *,
    required_primitives: Iterable[str],
    required_constraints: Iterable[str],
) -> Dict[str, Any]:
    """Compare candidate readiness for one explicit workload.

    The result is intentionally descriptive.  It exposes which candidates can
    enter the physical QoR gate, but refuses to infer a production default from
    source capabilities alone.
    """

    primitives = sorted(set(required_primitives))
    constraints = sorted(set(required_constraints))
    candidates = []
    providers = set()
    for report in reports:
        checked = validate_xilinx_placer_capability_report(report)
        provider = checked["provider"]
        if provider in providers:
            raise ValidationError(f"duplicate Xilinx placer provider {provider!r}")
        providers.add(provider)
        decision = qualify_xilinx_placer_capabilities(
            checked,
            required_primitives=primitives,
            required_constraints=constraints,
        )
        statuses = Counter(
            entry["status"]
            for population in ("stages", "primitives", "constraints")
            for entry in checked[population].values()
        )
        candidates.append({
            "provider": provider,
            "revision": checked["revision"],
            "capability_gate": decision,
            "status_counts": dict(sorted(statuses.items())),
            "physical_qor_gate": "required" if decision["status"] == "pass" else "blocked",
        })

    candidates.sort(key=lambda item: item["provider"])
    ready = [
        item["provider"]
        for item in candidates
        if item["capability_gate"]["status"] == "pass"
    ]
    return {
        "schema": XILINX_PLACER_COMPARISON_SCHEMA,
        "workload": {
            "required_primitives": primitives,
            "required_constraints": constraints,
        },
        "candidates": candidates,
        "qor_eligible_providers": ready,
        "default_selection": {
            "status": "deferred",
            "reason": (
                "capability evidence cannot select a default; compare routed "
                "Phase 7 legality, runtime, and global OpenSTA WNS/TNS"
            ),
        },
    }
