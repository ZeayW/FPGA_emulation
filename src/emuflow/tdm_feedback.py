"""Concrete Phase-5 schedule feedback for system-level routing."""

from __future__ import annotations

import math
import hashlib
import json
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional

from .errors import ValidationError
from .platform import Platform
from .tdm import (
    reconstruct_tdm_schedule_timing_paths,
    reconstruct_tdm_schedule_timing_paths_from_routes,
    validate_tdm_schedule,
)


TDM_FEEDBACK_SCHEMA = "emuflow.tdm-feedback/v1"
TDM_FEEDBACK_PROVIDER = "concrete-schedule-domain-feedback-v1"


def canonical_mapping_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _reconstruct_tdm_feedback(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
) -> Dict[str, Any]:
    validation = validate_tdm_schedule(
        routes,
        platform,
        schedule,
    )
    timing_paths = []
    if isinstance(routes.get("timing"), dict):
        timing_paths = (
            reconstruct_tdm_schedule_timing_paths_from_routes(
                routes, platform, schedule
            )
        )
    path_by_entry = defaultdict(list)
    normalized_by_path = {}
    domains_by_entry = {
        entry["id"]: entry["capacity_key"]
        for entry in schedule["entries"]
    }
    for path in timing_paths:
        normalized_by_path[path["path"]] = path["normalized_slack"]
        for hop in path["scheduled_hops"]:
            path_by_entry[hop["schedule_entry"]].append(path["path"])

    entries_by_domain = defaultdict(list)
    for entry in schedule["entries"]:
        entries_by_domain[entry["capacity_key"]].append(entry)
    domains = []
    for domain in schedule["domain_schedules"]:
        entries = entries_by_domain[domain["key"]]
        occupied = {(entry["slot"], entry["lane"]) for entry in entries}
        total_wait = sum(entry.get("ratio_wait_slots", 0) for entry in entries)
        maximum_wait = max(
            (entry.get("ratio_wait_slots", 0) for entry in entries),
            default=0,
        )
        affected_paths = sorted(
            {
                path
                for entry in entries
                for path in path_by_entry.get(entry["id"], [])
            }
        )
        worst_path_slack = min(
            (normalized_by_path[path] for path in affected_paths),
            default=None,
        )
        capacity = domain["capacity_bit_hops"]
        scheduled = domain["scheduled_bit_hops"]
        # This is a deterministic pricing signal, not a dual optimum.  It
        # combines exact occupancy and realized wait while remaining finite at
        # saturation; consumers retain the underlying terms separately.
        pressure = (
            scheduled / max(1, capacity - scheduled + 1)
            + total_wait / max(1, capacity)
        )
        domains.append(
            {
                **dict(domain),
                "unused_bit_hops": capacity - scheduled,
                "occupied_slot_lanes": len(occupied),
                "total_wait_slots": total_wait,
                "maximum_wait_slots": maximum_wait,
                "waiting_hops": sum(
                    entry.get("ratio_wait_slots", 0) > 0 for entry in entries
                ),
                "affected_timing_paths": affected_paths,
                "worst_affected_normalized_slack": worst_path_slack,
                "routing_price": pressure,
            }
        )
    paths = [
        {
            "path": path["path"],
            "clock_domain": path["clock_domain"],
            "slack_ns": path["slack_ns"],
            "normalized_slack": path["normalized_slack"],
            "transport_delay_ns": path["transport_delay_ns"],
            "scheduled_entries": [
                hop["schedule_entry"] for hop in path["scheduled_hops"]
            ],
            "capacity_domains": sorted(
                {
                    domains_by_entry[hop["schedule_entry"]]
                    for hop in path["scheduled_hops"]
                }
            ),
        }
        for path in timing_paths
    ]
    paths.sort(key=lambda item: (item["normalized_slack"], item["path"]))
    metrics = {
        "domains": len(domains),
        "saturated_domains": sum(
            domain["unused_bit_hops"] == 0 for domain in domains
        ),
        "scheduled_bit_hops": validation["scheduled_bit_hops"],
        "total_wait_slots": sum(
            domain["total_wait_slots"] for domain in domains
        ),
        "maximum_domain_price": max(
            (domain["routing_price"] for domain in domains), default=0.0
        ),
        "timing_paths": len(paths),
        "worst_normalized_slack": (
            paths[0]["normalized_slack"] if paths else None
        ),
    }
    return {
        "schema": TDM_FEEDBACK_SCHEMA,
        "provider": TDM_FEEDBACK_PROVIDER,
        "design": routes.get("design"),
        "platform": platform.name,
        "route_provider": routes.get("provider"),
        "schedule_provider": schedule.get("provider"),
        "frame_slots": schedule["metrics"]["frame_slots"],
        "source_routes_sha256": canonical_mapping_sha256(routes),
        "source_schedule_sha256": canonical_mapping_sha256(schedule),
        "domains": domains,
        "paths": paths,
        "metrics": metrics,
    }


def build_tdm_feedback(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
) -> Dict[str, Any]:
    return _reconstruct_tdm_feedback(
        routes,
        platform,
        schedule,
    )


def validate_tdm_feedback(
    routes: Mapping[str, Any],
    platform: Platform,
    schedule: Mapping[str, Any],
    feedback: Mapping[str, Any],
) -> Dict[str, Any]:
    expected = _reconstruct_tdm_feedback(
        routes,
        platform,
        schedule,
    )
    if feedback != expected:
        raise ValidationError(
            "TDM feedback does not match independent schedule reconstruction"
        )
    for domain in feedback["domains"]:
        if not math.isfinite(float(domain["routing_price"])):
            raise ValidationError("TDM feedback contains a non-finite price")
    return {"status": "pass", **expected["metrics"]}
