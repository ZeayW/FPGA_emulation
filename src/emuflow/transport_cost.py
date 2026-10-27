"""Validated transport resource model consumed by the production split stage."""
from __future__ import annotations
import math
from typing import Any, Dict, Mapping, Sequence
from .errors import ValidationError

OPEN_TRANSPORT_MODEL = "production-transport-rtl-structural-v2"
OPEN_TRANSPORT_PROVENANCE = "open_source_rtl_characterization"
BASE_FEATURE_NAMES = ("fixed_shell", "tx_output_lanes", "rx_shadow_bits", "rx_arrival_slot_groups", "tx_deep_mux_lanes")
FRAME_SLOTS = (2, 4, 8, 16)
TRANSPORT_COST_SCHEMA = "emuflow.transport-cost/v2"
_PROFILES = ("aggressive", "nominal", "conservative")

def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"open transport {name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise ValidationError(f"open transport {name} must be positive")
    return result


def extract_transport_features(transport: Mapping[str, Any]) -> Dict[str, float]:
    """Extract the exact structural features used by the calibrated model.

    Production Phase 6 calls this same function, so characterization and
    application prediction cannot silently drift to different feature
    definitions.
    """

    frame_slots = _positive_int(transport.get("frame_slots"), "frame_slots")
    if frame_slots not in FRAME_SLOTS:
        raise ValidationError(
            "open transport production frame_slots is outside the "
            "characterized set"
        )
    endpoints = transport.get("endpoints")
    shadow_signals = transport.get("shadow_signals")
    if (
        not isinstance(endpoints, Sequence)
        or isinstance(endpoints, (str, bytes))
        or not isinstance(shadow_signals, Sequence)
        or isinstance(shadow_signals, (str, bytes))
    ):
        raise ValidationError("open transport production artifact is invalid")
    tx_lane_assignments: dict[tuple[str, str, int], int] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, Mapping):
            raise ValidationError("open transport endpoint is invalid")
        if endpoint["kind"] != "tx":
            continue
        key = (endpoint["link"], endpoint["peer"], endpoint["lane"])
        tx_lane_assignments[key] = tx_lane_assignments.get(key, 0) + 1
    rx_arrival_groups = set()
    for endpoint in endpoints:
        if endpoint["kind"] == "rx":
            rx_arrival_groups.add(
                (endpoint["link"], endpoint["peer"], endpoint["arrival_slot"])
            )
        elif endpoint["kind"] != "tx":
            raise ValidationError("open transport endpoint kind is invalid")
    features = {
        "fixed_shell": 1.0,
        # TX logic is synthesized per physical output lane, not per logical
        # signal.  Different slots can reuse one lane and one LUT.
        "tx_output_lanes": float(len(tx_lane_assignments)),
        "rx_shadow_bits": float(len(shadow_signals)),
        # RX case decoding is shared by all shadow bits arriving from the same
        # peer in one slot.
        "rx_arrival_slot_groups": float(len(rx_arrival_groups)),
        # A 3-bit-or-wider slot mux plus four data choices exceeds one LUT6.
        # This threshold feature represents the extra mapped mux level while
        # remaining independent of a particular characterization case size.
        "tx_deep_mux_lanes": float(
            sum(count >= 4 for count in tx_lane_assignments.values())
        ),
    }
    for slots in FRAME_SLOTS[1:]:
        features[f"frame_slots_{slots}"] = 1.0 if frame_slots == slots else 0.0
    return features


def validate_transport_cost_database(
    value: Mapping[str, Any], *, expected_platform: str | None = None
) -> Dict[str, Any]:
    if value.get("schema") != TRANSPORT_COST_SCHEMA:
        raise ValidationError("transport cost schema is invalid")
    platform = value.get("platform")
    profile = value.get("profile")
    resources = value.get("resources")
    feature_names = value.get("feature_names")
    if not isinstance(platform, str) or not platform:
        raise ValidationError("transport cost platform is invalid")
    if expected_platform is not None and platform != expected_platform:
        raise ValidationError("transport cost platform disagrees")
    if (
        profile not in _PROFILES
        or value.get("model") != OPEN_TRANSPORT_MODEL
        or not isinstance(feature_names, list)
        or not feature_names
        or any(not isinstance(name, str) or not name for name in feature_names)
        or len(set(feature_names)) != len(feature_names)
        or not isinstance(resources, dict)
        or not resources
    ):
        raise ValidationError("transport cost profile or resources are invalid")
    count = 0
    for resource, parameters in resources.items():
        if not isinstance(resource, str) or not resource or not isinstance(parameters, dict):
            raise ValidationError("transport cost resource entry is invalid")
        if set(parameters) != set(feature_names):
            raise ValidationError("transport cost resource features disagree")
        for name in feature_names:
            amount = parameters.get(name)
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)) or amount < 0:
                raise ValidationError("transport cost parameter is invalid")
            count += 1
    provenance = value.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("class") != OPEN_TRANSPORT_PROVENANCE
        or not isinstance(provenance.get("fit_sha256"), str)
        or len(provenance["fit_sha256"]) != 64
    ):
        raise ValidationError("transport cost provenance is invalid")
    return {"status": "pass", "platform": platform, "profile": profile, "parameters": count}


def predict_transport_resources(
    database: Mapping[str, Any], transports: Mapping[str, Mapping[str, Any]]
) -> Dict[str, Any]:
    """Apply one validated TransportCostDB to production Phase 6 artifacts."""

    validation = validate_transport_cost_database(database)
    feature_names = list(database["feature_names"])
    records = []
    totals = {resource: 0.0 for resource in database["resources"]}
    for fpga_id, transport in sorted(transports.items()):
        if transport.get("fpga") != fpga_id:
            raise ValidationError("transport cost prediction FPGA identity disagrees")
        features = extract_transport_features(transport)
        if set(features) != set(feature_names):
            raise ValidationError(
                "transport cost database disagrees with production feature contract"
            )
        resources = {}
        for resource, coefficients in sorted(database["resources"].items()):
            amount = sum(
                float(features[name]) * float(coefficients[name])
                for name in feature_names
            )
            resources[resource] = amount
            totals[resource] += amount
        records.append(
            {"fpga": fpga_id, "features": features, "resources": resources}
        )
    return {
        "schema": "emuflow.transport-cost-prediction/v1",
        "status": "pass",
        "platform": validation["platform"],
        "profile": validation["profile"],
        "model": database["model"],
        "fpgas": records,
        "total_resources": totals,
    }
