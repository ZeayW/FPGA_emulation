"""Checked Phase-7 routed-boundary feedback for Phase 4/5 optimization."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional

from .boundary_timing import validate_boundary_timing_database
from .errors import ValidationError
from .platform import Platform
from .physical_summary_storage import canonical_physical_summary_sha256
from .runtime import validate_physical_summary
from .tdm import reconstruct_tdm_schedule_timing_paths, validate_tdm_schedule
from .tdm_feedback import canonical_mapping_sha256


PHYSICAL_ROUTE_FEEDBACK_SCHEMA = "emuflow.physical-route-feedback/v1"
PHYSICAL_ROUTE_FEEDBACK_PROVIDER = "phase7-boundary-domain-feedback-v1"


def _boundary_delays(
    physical_summary: Mapping[str, Any],
) -> Dict[str, float]:
    timing = physical_summary.get("boundary_timing")
    identities = physical_summary.get("boundary_identities")
    if not isinstance(timing, Mapping) or not isinstance(identities, Mapping):
        raise ValidationError(
            "physical route feedback requires exact boundary timing and identities"
        )
    if set(timing) != set(identities):
        raise ValidationError("physical boundary timing coverage disagrees")
    delays = {}
    for fpga in sorted(timing):
        validate_boundary_timing_database(timing[fpga], identities[fpga])
        for endpoint in timing[fpga]["endpoints"]:
            endpoint_id = endpoint["id"]
            delay = endpoint["delay_ns"]
            if (
                endpoint_id in delays
                or isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(float(delay))
                or float(delay) < 0.0
            ):
                raise ValidationError("physical boundary delay is invalid")
            delays[endpoint_id] = float(delay)
    return delays


def _reconstruct_physical_route_feedback(
    runtime: Mapping[str, Any],
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
) -> Dict[str, Any]:
    validate_physical_summary(physical_summary, runtime, platform)
    validate_tdm_schedule(routes, platform, schedule)
    delays = _boundary_delays(physical_summary)
    timing_paths = reconstruct_tdm_schedule_timing_paths(
        routes, platform, schedule
    )
    entry_by_id = {entry["id"]: entry for entry in schedule["entries"]}
    path_by_entry = defaultdict(list)
    paths = []
    for path in timing_paths:
        interface_delay = 0.0
        for hop in path["scheduled_hops"]:
            missing = [
                endpoint
                for endpoint in (hop["tx_endpoint"], hop["rx_endpoint"])
                if endpoint not in delays
            ]
            if missing:
                raise ValidationError(
                    f"physical route feedback lacks endpoints {missing}"
                )
            interface_delay += delays[hop["tx_endpoint"]]
            interface_delay += delays[hop["rx_endpoint"]]
            path_by_entry[hop["schedule_entry"]].append(path["path"])
        paths.append(
            {
                "path": path["path"],
                "clock_domain": path["clock_domain"],
                "normalized_slack": path["normalized_slack"],
                "scheduled_link_tdm_delay_ns": path["transport_delay_ns"],
                "routed_boundary_delay_ns": interface_delay,
                "combined_transport_boundary_delay_ns": (
                    path["transport_delay_ns"] + interface_delay
                ),
                "scheduled_entries": [
                    hop["schedule_entry"] for hop in path["scheduled_hops"]
                ],
            }
        )
    paths.sort(
        key=lambda item: (
            -item["combined_transport_boundary_delay_ns"], item["path"]
        )
    )
    entries_by_domain = defaultdict(list)
    for entry in schedule["entries"]:
        entries_by_domain[entry["capacity_key"]].append(entry)
    domains = []
    for domain in schedule["domain_schedules"]:
        entries = entries_by_domain[domain["key"]]
        routed_delay = 0.0
        affected = set()
        for entry in entries:
            tx = f"__emuflow_tx_{entry['id']}"
            rx = f"__emuflow_rx_{entry['id']}"
            if tx not in delays or rx not in delays:
                raise ValidationError(
                    f"physical route feedback lacks boundary for {entry['id']}"
                )
            routed_delay += delays[tx] + delays[rx]
            affected.update(path_by_entry.get(entry["id"], []))
        mean_delay = routed_delay / max(1, len(entries))
        # A dimensionless physical price.  It remains zero for unused domains
        # and scales routed boundary delay by the BoardDB TDM slot duration so
        # it can be added to the existing schedule-domain routing price.
        link = next(link for link in platform.links if link.id == domain["link"])
        slot_ns = 1000.0 / link.fabric_clock_mhz
        domains.append(
            {
                **dict(domain),
                "active_hops": len(entries),
                "routed_boundary_delay_ns": routed_delay,
                "mean_routed_boundary_delay_ns": mean_delay,
                "affected_timing_paths": sorted(affected),
                "physical_routing_price": (
                    mean_delay / slot_ns if entries else 0.0
                ),
            }
        )
    metrics = {
        "domains": len(domains),
        "active_domains": sum(domain["active_hops"] > 0 for domain in domains),
        "timing_paths": len(paths),
        "maximum_physical_routing_price": max(
            (domain["physical_routing_price"] for domain in domains),
            default=0.0,
        ),
        "maximum_path_boundary_delay_ns": max(
            (path["routed_boundary_delay_ns"] for path in paths),
            default=0.0,
        ),
    }
    return {
        "schema": PHYSICAL_ROUTE_FEEDBACK_SCHEMA,
        "provider": PHYSICAL_ROUTE_FEEDBACK_PROVIDER,
        "design": routes.get("design"),
        "platform": platform.name,
        "source_routes_sha256": canonical_mapping_sha256(routes),
        "source_schedule_sha256": canonical_mapping_sha256(schedule),
        "source_physical_summary_sha256": canonical_physical_summary_sha256(
            physical_summary
        ),
        "domains": domains,
        "paths": paths,
        "metrics": metrics,
    }


def build_physical_route_feedback(
    runtime: Mapping[str, Any],
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
) -> Dict[str, Any]:
    return _reconstruct_physical_route_feedback(
        runtime, routes, platform, schedule, physical_summary
    )


def validate_physical_route_feedback(
    runtime: Mapping[str, Any],
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    physical_summary: Mapping[str, Any],
    feedback: Mapping[str, Any],
) -> Dict[str, Any]:
    expected = _reconstruct_physical_route_feedback(
        runtime, routes, platform, schedule, physical_summary
    )
    if feedback != expected:
        raise ValidationError(
            "physical route feedback does not match independent reconstruction"
        )
    return {"status": "pass", **expected["metrics"]}


def combine_tdm_and_physical_feedback(
    tdm_feedback: Mapping[str, Any],
    physical_feedback: Mapping[str, Any],
    *,
    physical_weight: float = 1.0,
) -> Dict[str, Any]:
    """Add checked Phase-7 prices to a checked Phase-5 feedback artifact."""

    if (
        isinstance(physical_weight, bool)
        or not isinstance(physical_weight, (int, float))
        or not math.isfinite(float(physical_weight))
        or float(physical_weight) < 0.0
    ):
        raise ValidationError("physical feedback weight must be non-negative")
    if (
        tdm_feedback.get("schema") != "emuflow.tdm-feedback/v1"
        or physical_feedback.get("schema") != PHYSICAL_ROUTE_FEEDBACK_SCHEMA
        or tdm_feedback.get("design") != physical_feedback.get("design")
        or tdm_feedback.get("platform") != physical_feedback.get("platform")
        or tdm_feedback.get("source_routes_sha256")
        != physical_feedback.get("source_routes_sha256")
        or tdm_feedback.get("source_schedule_sha256")
        != physical_feedback.get("source_schedule_sha256")
    ):
        raise ValidationError("TDM and physical feedback sources disagree")
    physical_by_key = {
        domain["key"]: domain for domain in physical_feedback["domains"]
    }
    if set(physical_by_key) != {
        domain["key"] for domain in tdm_feedback["domains"]
    }:
        raise ValidationError("feedback capacity-domain coverage disagrees")
    result = dict(tdm_feedback)
    result["domains"] = []
    for domain in tdm_feedback["domains"]:
        physical = physical_by_key[domain["key"]]
        result["domains"].append(
            {
                **dict(domain),
                "schedule_routing_price": domain["routing_price"],
                "physical_routing_price": physical[
                    "physical_routing_price"
                ],
                "routing_price": domain["routing_price"]
                + float(physical_weight) * physical["physical_routing_price"],
            }
        )
    result["metrics"] = dict(tdm_feedback["metrics"])
    result["metrics"]["maximum_domain_price"] = max(
        (domain["routing_price"] for domain in result["domains"]),
        default=0.0,
    )
    return result
