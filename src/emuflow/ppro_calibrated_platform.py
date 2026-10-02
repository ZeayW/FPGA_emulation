"""Generate validated EmuFlow platform profiles from black-box fit artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence

from .board_link_timing import validate_board_link_timing
from .errors import ValidationError
from .io import read_json, write_json
from .open_transport_characterization import (
    BASE_FEATURE_NAMES,
    FRAME_SLOTS,
    OPEN_TRANSPORT_FIT_SCHEMA,
    OPEN_TRANSPORT_MODEL,
    OPEN_TRANSPORT_PROVENANCE,
)
from .platform import Platform
from .ppro_blackbox_calibration import validate_public_platform_prior
from .ppro_blackbox_stage3 import CALIBRATED_CAPACITY_SCHEMA, TOPOLOGY_FIT_SCHEMA
from .ppro_blackbox_stage4 import LATENCY_FIT_SCHEMA, PAYLOAD_FIT_SCHEMA


TRANSPORT_COST_SCHEMA = "emuflow.transport-cost/v2"
CALIBRATED_MANIFEST_SCHEMA = "emuflow.ppro-calibrated-platform-manifest/v1"
_PROFILES = ("aggressive", "nominal", "conservative")
_MAX_HOLDOUT_RELATIVE_ERROR = 0.15
_ZERO_ACTUAL_ABSOLUTE_TOLERANCE = 1.0
_AXIS_MAP = {
    # public-prior key, BoardDB resource, observation resource, BoardDB units
    # per observation unit.  PPro's ordinary BRAM report is normalized as a
    # 36-Kib-class block, while BoardDB intentionally uses 18-Kib blocks.
    "lut": ("clb_lut", "lut", "lut", 1.0),
    "ff": ("clb_ff", "ff", "ff", 1.0),
    "bram": ("bram_kib", "bram18k", "bram36k", 2.0),
    "uram": ("uram_kib", "uram288", "uram288", 1.0),
    "dsp": ("dsp", "dsp48", "dsp48", 1.0),
}


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _public_capacities(prior: Mapping[str, Any]) -> Dict[str, int]:
    resources = {item["name"]: float(item["value"]) for item in prior["device"]["resources"]}
    result = {
        "lut": int(math.floor(resources["clb_lut"])),
        "ff": int(math.floor(resources["clb_ff"])),
        "bram18k": int(math.floor(resources["bram_kib"] / 18.0)),
        "uram288": int(math.floor(resources["uram_kib"] / 288.0)),
        "dsp48": int(math.floor(resources["dsp"])),
        "io": int(math.floor(resources["user_io"])),
    }
    if any(value <= 0 for value in result.values()):
        raise ValidationError("public prior contains a non-positive device capacity")
    return result


def _utilization_limit(
    capacity_fit: Mapping[str, Any], public_capacity: Mapping[str, int]
) -> tuple[float, Dict[str, Any]]:
    if capacity_fit.get("schema") != CALIBRATED_CAPACITY_SCHEMA:
        raise ValidationError("calibrated platform capacity fit schema is invalid")
    axes = capacity_fit.get("axes")
    if not isinstance(axes, dict):
        raise ValidationError("calibrated platform capacity axes are invalid")
    ratios = {}
    conversions = {}
    for axis, (_, board_resource, demand_resource, demand_to_board_scale) in _AXIS_MAP.items():
        record = axes.get(axis)
        if not isinstance(record, dict):
            raise ValidationError(f"calibrated platform lacks the {axis} capacity axis")
        demand = record.get("effective_resource_capacity")
        observed_resource = record.get("observation_resource")
        if (
            isinstance(demand, bool)
            or not isinstance(demand, (int, float))
            or demand <= 0
            or observed_resource != demand_resource
        ):
            raise ValidationError(f"calibrated platform {axis} fit lacks effective capacity")
        mapped_demand = float(demand) * demand_to_board_scale
        ratio = mapped_demand / public_capacity[board_resource]
        if not 0.0 < ratio <= 1.0:
            raise ValidationError(f"calibrated platform {axis} effective capacity exceeds public bounds")
        ratios[axis] = ratio
        conversions[axis] = {
            "observation_resource": demand_resource,
            "boarddb_resource": board_resource,
            "boarddb_units_per_observation_unit": demand_to_board_scale,
        }
    # BoardDB v1 has one utilization limit.  The minimum fitted ratio is the
    # only conservative lossless projection across resource dimensions.
    value = min(ratios.values())
    return value, {
        "projection": "minimum-per-resource-effective-to-public-ratio",
        "per_resource_ratios": ratios,
        "demand_unit_conversions": conversions,
        "selected_limit": value,
    }


def _configuration_count(prior: Mapping[str, Any], configuration_id: str) -> int:
    matches = [item for item in prior["configurations"] if item["id"] == configuration_id]
    if len(matches) != 1:
        raise ValidationError("calibrated platform configuration id is not in the public prior")
    return int(matches[0]["fpga_count"])


def _require_calibration_gates(
    *,
    capacity_fit: Mapping[str, Any],
    topology_fit: Mapping[str, Any],
    payload_fit: Mapping[str, Any],
    latency_fit: Mapping[str, Any],
    transport_fit: Mapping[str, Any],
) -> None:
    for name, fit, flag in (
        ("capacity", capacity_fit, "all_resolved_holdouts_match"),
        ("topology", topology_fit, "all_holdouts_match"),
        ("payload", payload_fit, "all_resolved_holdouts_match"),
    ):
        if fit.get("excluded_observations") != 0:
            raise ValidationError(f"calibrated platform {name} fit has excluded observations")
        checks = fit.get("holdout_checks")
        if not isinstance(checks, list) or not checks or fit.get(flag) is not True:
            raise ValidationError(f"calibrated platform {name} holdout gate failed")

    if latency_fit.get("excluded_observations") != 0:
        raise ValidationError("calibrated platform latency fit has excluded observations")
    latency_checks = latency_fit.get("holdout_checks")
    latency_error = latency_fit.get("holdout_max_relative_error")
    if (
        not isinstance(latency_checks, list)
        or not latency_checks
        or isinstance(latency_error, bool)
        or not isinstance(latency_error, (int, float))
        or latency_error > _MAX_HOLDOUT_RELATIVE_ERROR
    ):
        raise ValidationError("calibrated platform latency holdout gate failed")
    for record in latency_fit.get("parameters", {}).values():
        if not isinstance(record, dict) or record.get("identifiable") is not True:
            raise ValidationError("calibrated platform latency fit is not identifiable")
    ratio_delays = latency_fit.get("tdm_ratio_delay_ns")
    if not isinstance(ratio_delays, dict):
        raise ValidationError("calibrated platform latency TDM fit is invalid")
    for record in ratio_delays.values():
        if not isinstance(record, dict) or record.get("identifiable") is not True:
            raise ValidationError("calibrated platform latency TDM fit is not identifiable")

    if transport_fit.get("excluded_observations") != 0:
        raise ValidationError("calibrated platform transport fit has excluded observations")
    transport_checks = transport_fit.get("holdout_checks")
    if not isinstance(transport_checks, list) or not transport_checks:
        raise ValidationError("calibrated platform transport holdout gate failed")
    if (
        transport_fit.get("schema") != OPEN_TRANSPORT_FIT_SCHEMA
        or transport_fit.get("model") != OPEN_TRANSPORT_MODEL
        or transport_fit.get("all_resources_identifiable") is not True
    ):
        raise ValidationError("calibrated platform transport fit is not source-characterized")
    provenance = transport_fit.get("provenance")
    if (
        not isinstance(provenance, dict)
        or provenance.get("class") != OPEN_TRANSPORT_PROVENANCE
    ):
        raise ValidationError("calibrated platform transport provenance is invalid")
    expected_features = set(BASE_FEATURE_NAMES) | {
        f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]
    }
    feature_names = transport_fit.get("feature_names")
    if not isinstance(feature_names, list) or set(feature_names) != expected_features:
        raise ValidationError("calibrated platform transport feature contract is invalid")
    resources = transport_fit.get("resources")
    if not isinstance(resources, dict) or not resources:
        raise ValidationError("calibrated platform transport resources are invalid")
    for fit in resources.values():
        for record in fit.get("parameters", {}).values():
            if not isinstance(record, dict) or record.get("identifiable") is not True:
                raise ValidationError("calibrated platform transport fit is not identifiable")
    for check in transport_checks:
        for result in check.get("resources", {}).values():
            actual = result.get("actual")
            relative = result.get("relative_error")
            absolute = result.get("absolute_error")
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                raise ValidationError("calibrated platform transport holdout is invalid")
            if actual == 0:
                if (
                    isinstance(absolute, bool)
                    or not isinstance(absolute, (int, float))
                    or absolute > _ZERO_ACTUAL_ABSOLUTE_TOLERANCE
                ):
                    raise ValidationError("calibrated platform transport zero-cost holdout failed")
            elif (
                isinstance(relative, bool)
                or not isinstance(relative, (int, float))
                or relative > _MAX_HOLDOUT_RELATIVE_ERROR
            ):
                raise ValidationError("calibrated platform transport holdout gate failed")


def _fit_edges(topology_fit: Mapping[str, Any], fpga_count: int) -> Dict[tuple[int, int], int]:
    if topology_fit.get("schema") != TOPOLOGY_FIT_SCHEMA:
        raise ValidationError("calibrated platform topology fit schema is invalid")
    result = {}
    for record in topology_fit.get("directed_edges", []):
        source = record.get("source")
        sink = record.get("sink")
        if not isinstance(source, str) or not isinstance(sink, str):
            raise ValidationError("calibrated topology edge identity is invalid")
        try:
            source_index = int(source.removeprefix("F"))
            sink_index = int(sink.removeprefix("F"))
        except ValueError as error:
            raise ValidationError("calibrated topology FPGA alias is invalid") from error
        if not (0 <= source_index < fpga_count and 0 <= sink_index < fpga_count):
            raise ValidationError("calibrated topology edge exceeds the selected configuration")
        if record.get("state") == "reachable":
            hops = record.get("effective_hops")
            if isinstance(hops, bool) or not isinstance(hops, int) or hops < 1:
                raise ValidationError("calibrated topology reachable edge lacks hop evidence")
            result[(source_index, sink_index)] = hops
        elif record.get("state") != "unreachable":
            raise ValidationError("calibrated topology edge state is invalid")
    return result


def _shortest_hops(
    fpga_count: int, direct_edges: set[tuple[int, int]], source: int, sink: int
) -> int | None:
    queue = deque([(source, 0)])
    seen = {source}
    while queue:
        node, depth = queue.popleft()
        for left, right in sorted(direct_edges):
            if left != node or right in seen:
                continue
            if right == sink:
                return depth + 1
            seen.add(right)
            queue.append((right, depth + 1))
    return None


def _direct_topology(topology_fit: Mapping[str, Any], fpga_count: int) -> set[tuple[int, int]]:
    fitted = _fit_edges(topology_fit, fpga_count)
    direct = {pair for pair, hops in fitted.items() if hops == 1}
    if not direct:
        raise ValidationError("calibrated platform has no observed one-hop edge")
    for (source, sink), expected in fitted.items():
        actual = _shortest_hops(fpga_count, direct, source, sink)
        if actual != expected:
            raise ValidationError(
                f"one-hop topology cannot explain observed F{source}->F{sink} hop count"
            )
    return direct


def _payload_widths(payload_fit: Mapping[str, Any]) -> Dict[tuple[int, int], int]:
    if payload_fit.get("schema") != PAYLOAD_FIT_SCHEMA:
        raise ValidationError("calibrated platform payload fit schema is invalid")
    result = {}
    for record in payload_fit.get("link_signatures", []):
        if (
            record.get("bidirectional") is not False
            or record.get("flow_count") != 1
            or record.get("fanout") != 1
            or record.get("forced_tdm_ratio") != 0
        ):
            continue
        source = int(str(record["source"]).removeprefix("F"))
        sink = int(str(record["sink"]).removeprefix("F"))
        ratio_one = record.get("ratio_one_lower_width_bits")
        if isinstance(ratio_one, bool) or not isinstance(ratio_one, int) or ratio_one <= 0:
            raise ValidationError("calibrated payload fit lacks a ratio-one lower bound")
        result[(source, sink)] = ratio_one
    return result


def _latency_bound(latency_fit: Mapping[str, Any], profile: str) -> float:
    if latency_fit.get("schema") != LATENCY_FIT_SCHEMA:
        raise ValidationError("calibrated platform latency fit schema is invalid")
    parameters = latency_fit.get("parameters", {})
    values = {}
    for name in (
        "endpoint_ns",
        "per_hop_ns",
    ):
        record = parameters.get(name)
        if not isinstance(record, dict) or profile not in record:
            raise ValidationError(f"calibrated platform latency fit lacks {name}.{profile}")
        values[name] = float(record[profile])
    return values["endpoint_ns"] + values["per_hop_ns"]


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


def generate_calibrated_platform_profiles(
    *,
    prior: Mapping[str, Any],
    configuration_id: str,
    capacity_fit: Mapping[str, Any],
    topology_fit: Mapping[str, Any],
    payload_fit: Mapping[str, Any],
    latency_fit: Mapping[str, Any],
    transport_fit: Mapping[str, Any],
    fabric_clock_mhz: Mapping[str, float],
) -> Dict[str, Any]:
    """Generate three immutable, validated profiles; perform no fitting here."""
    normalized_prior = validate_public_platform_prior(prior)
    if transport_fit.get("schema") != OPEN_TRANSPORT_FIT_SCHEMA:
        raise ValidationError("calibrated platform transport fit schema is invalid")
    if set(fabric_clock_mhz) != set(_PROFILES):
        raise ValidationError("calibrated platform requires all fabric-clock sensitivity profiles")
    for profile, value in fabric_clock_mhz.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValidationError(f"calibrated platform fabric clock {profile} is invalid")
    _require_calibration_gates(
        capacity_fit=capacity_fit,
        topology_fit=topology_fit,
        payload_fit=payload_fit,
        latency_fit=latency_fit,
        transport_fit=transport_fit,
    )
    fpga_count = _configuration_count(normalized_prior, configuration_id)
    public_capacity = _public_capacities(normalized_prior)
    utilization_limit, capacity_projection = _utilization_limit(capacity_fit, public_capacity)
    direct_edges = _direct_topology(topology_fit, fpga_count)
    payload_widths = _payload_widths(payload_fit)
    if any(edge not in payload_widths for edge in direct_edges):
        raise ValidationError("calibrated platform lacks ratio-one payload evidence for a direct edge")

    generated = {}
    source_hashes = {
        "prior": _sha256(normalized_prior),
        "capacity_fit": _sha256(capacity_fit),
        "topology_fit": _sha256(topology_fit),
        "payload_fit": _sha256(payload_fit),
        "latency_fit": _sha256(latency_fit),
        "transport_fit": _sha256(transport_fit),
    }
    for profile in _PROFILES:
        frequency = float(fabric_clock_mhz[profile])
        platform_name = f"ppro-calibrated-{configuration_id}-{profile}"
        links = []
        consumed = set()
        timing_bounds = {}
        for source, sink in sorted(direct_edges):
            if (source, sink) in consumed:
                continue
            reverse = (sink, source)
            symmetric = reverse in direct_edges and payload_widths[reverse] == payload_widths[(source, sink)]
            link_id = f"link-f{source}-f{sink}"
            width = payload_widths[(source, sink)]
            delay = _latency_bound(latency_fit, profile)
            cycles = int(math.ceil(delay * frequency / 1000.0))
            links.append(
                {
                    "id": link_id,
                    "endpoints": [f"F{source}", f"F{sink}"],
                    "direction": "full_duplex" if symmetric else "unidirectional",
                    "mode": "abstract",
                    "data_lanes_per_direction": width,
                    "fabric_clock_mhz": frequency,
                    "latency_cycles": cycles,
                    "capacity_sharing": "per_direction",
                }
            )
            timing_bounds[(link_id, f"F{source}", f"F{sink}")] = delay
            consumed.add((source, sink))
            if symmetric:
                reverse_delay = _latency_bound(latency_fit, profile)
                timing_bounds[(link_id, f"F{sink}", f"F{source}")] = reverse_delay
                consumed.add(reverse)

        boarddb = {
            "schema": "emuflow.boarddb/v1",
            "platform": {
                "name": platform_name,
                "kind": "virtual",
                "description": "PPro-behavior-equivalent academic profile; not a physical S2C board specification",
            },
            "fpgas": [
                {
                    "id": f"F{index}",
                    "part": "academic-xcvu19p-behavioral",
                    "utilization_limit": utilization_limit,
                    "capacity": public_capacity,
                }
                for index in range(fpga_count)
            ],
            "links": links,
        }
        platform = Platform.from_dict(boarddb)
        timing_records = []
        for (link_id, source, sink), delay in sorted(timing_bounds.items()):
            link = next(item for item in platform.links if item.id == link_id)
            timing_records.append(
                {
                    "link": link_id,
                    "from": source,
                    "to": sink,
                    "fabric_clock_mhz": link.fabric_clock_mhz,
                    "latency_cycles": link.latency_cycles,
                    "delay_bound_ns": delay,
                    "qualification": "characterized-upper-bound",
                    "source": {
                        "kind": "vendor-characterization",
                        "reference": f"black-box-calibration:{source_hashes['latency_fit']}",
                    },
                }
            )
        timing = {
            "schema": "emuflow.board-link-timing/v1",
            "status": "pass",
            "platform": platform_name,
            "measurement_scope": "tx-transport-stage-to-rx-transport-stage",
            "final_link_timing_signoff": False,
            "links": timing_records,
        }
        validate_board_link_timing(timing, platform)

        transport_resources = {}
        transport_features = transport_fit.get("feature_names")
        if not isinstance(transport_features, list) or not transport_features:
            raise ValidationError("calibrated platform transport fit lacks feature names")
        for resource, fit in sorted(transport_fit.get("resources", {}).items()):
            parameters = fit.get("parameters", {})
            transport_resources[resource] = {
                name: float(parameters[name][profile])
                for name in transport_features
            }
        transport = {
            "schema": TRANSPORT_COST_SCHEMA,
            "platform": platform_name,
            "profile": profile,
            "model": OPEN_TRANSPORT_MODEL,
            "feature_names": transport_features,
            "resources": transport_resources,
            "provenance": {
                "class": OPEN_TRANSPORT_PROVENANCE,
                "fit_sha256": source_hashes["transport_fit"],
            },
        }
        validate_transport_cost_database(transport, expected_platform=platform_name)
        generated[profile] = {
            "boarddb": boarddb,
            "board_link_timing": timing,
            "transport_cost": transport,
        }

    manifest = {
        "schema": CALIBRATED_MANIFEST_SCHEMA,
        "configuration_id": configuration_id,
        "claim_scope": "PPro-behavior-equivalent-academic-model",
        "not_claimed": [
            "physical-board-equivalence",
            "package-pin-equivalence",
            "reference-clock-reset-equivalence",
            "hardware-measurement-signoff",
        ],
        "fabric_clock_provenance": "research_assumption",
        "fabric_clock_mhz": {name: float(fabric_clock_mhz[name]) for name in _PROFILES},
        "capacity_projection": capacity_projection,
        "source_hashes": source_hashes,
        "profiles": {
            name: {artifact: _sha256(value) for artifact, value in generated[name].items()}
            for name in _PROFILES
        },
    }
    return {"manifest": manifest, "profiles": generated}


def write_calibrated_platform_profiles(output_dir: Path, bundle: Mapping[str, Any]) -> None:
    """Write only final immutable model artifacts, never campaign scratch."""
    root = output_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = bundle.get("manifest")
    profiles = bundle.get("profiles")
    if not isinstance(manifest, dict) or not isinstance(profiles, dict):
        raise ValidationError("calibrated platform bundle structure is invalid")
    write_json(root / "manifest.json", manifest, compact=True)
    for profile in _PROFILES:
        artifacts = profiles.get(profile)
        if not isinstance(artifacts, dict):
            raise ValidationError(f"calibrated platform bundle lacks {profile}")
        destination = root / profile
        destination.mkdir(parents=True, exist_ok=True)
        write_json(destination / "boarddb.json", artifacts["boarddb"], compact=True)
        write_json(
            destination / "board-link-timing.json",
            artifacts["board_link_timing"],
            compact=True,
        )
        write_json(destination / "transport-cost.json", artifacts["transport_cost"], compact=True)
    validate_calibrated_platform_bundle(root)


def validate_calibrated_platform_bundle(root: Path) -> Dict[str, Any]:
    manifest = read_json(root / "manifest.json")
    if manifest.get("schema") != CALIBRATED_MANIFEST_SCHEMA:
        raise ValidationError("calibrated platform manifest schema is invalid")
    if manifest.get("claim_scope") != "PPro-behavior-equivalent-academic-model":
        raise ValidationError("calibrated platform claim scope is invalid")
    if manifest.get("fabric_clock_provenance") != "research_assumption":
        raise ValidationError("calibrated platform fabric-clock provenance is invalid")
    records = manifest.get("profiles")
    if not isinstance(records, dict) or set(records) != set(_PROFILES):
        raise ValidationError("calibrated platform manifest profile coverage is invalid")
    summaries = {}
    for profile in _PROFILES:
        destination = root / profile
        artifacts = {
            "boarddb": read_json(destination / "boarddb.json"),
            "board_link_timing": read_json(destination / "board-link-timing.json"),
            "transport_cost": read_json(destination / "transport-cost.json"),
        }
        expected_hashes = records[profile]
        if not isinstance(expected_hashes, dict) or any(
            expected_hashes.get(name) != _sha256(value) for name, value in artifacts.items()
        ):
            raise ValidationError(f"calibrated platform {profile} artifact hash mismatch")
        platform = Platform.from_dict(artifacts["boarddb"])
        timing = validate_board_link_timing(artifacts["board_link_timing"], platform)
        transport = validate_transport_cost_database(
            artifacts["transport_cost"], expected_platform=platform.name
        )
        summaries[profile] = {
            "platform": platform.name,
            "fpgas": len(platform.fpgas),
            "links": len(platform.links),
            "directed_timing_links": timing["directed_links"],
            "transport_parameters": transport["parameters"],
        }
    return {"status": "pass", "configuration_id": manifest.get("configuration_id"), "profiles": summaries}
