"""Characterize EmuFlow's production transport RTL with the open Route-A mapper.

PPro's ordinary pre-partition reports do not expose the transport shell that
PPro inserts after partitioning.  This module deliberately does not infer a
zero cost from that missing observation.  It maps the exact RTL emitted by
``transport_to_systemverilog`` together with the production virtual runtime
controller and fits a compact, independently held-out resource model.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Sequence

from .errors import ValidationError
from .io import read_json, write_json
from .netlist import transport_to_systemverilog
from .platform import Platform
from .runtime import virtual_runtime_controller_to_systemverilog
from .synthesis import run_xilinx_ultrascaleplus_yosys


OPEN_TRANSPORT_MATRIX_SCHEMA = "emuflow.open-transport-matrix/v2"
OPEN_TRANSPORT_OBSERVATION_SCHEMA = "emuflow.open-transport-observation/v2"
OPEN_TRANSPORT_FIT_SCHEMA = "emuflow.open-transport-cost-fit/v2"
OPEN_TRANSPORT_MODEL = "production-transport-rtl-structural-v2"
OPEN_TRANSPORT_PROVENANCE = "open_source_rtl_characterization"

RESOURCE_NAMES = ("lut", "ff", "bram18k", "dsp48", "uram288")
BASE_FEATURE_NAMES = (
    "fixed_shell",
    "tx_output_lanes",
    "rx_shadow_bits",
    "rx_arrival_slot_groups",
    "tx_deep_mux_lanes",
)
FRAME_SLOTS = (2, 4, 8, 16)
MAX_HOLDOUT_RELATIVE_ERROR = 0.15
ZERO_ABSOLUTE_TOLERANCE = 1.0


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"open transport {name} must be a non-negative integer")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise ValidationError(f"open transport {name} must be positive")
    return result


def _case(
    identifier: str,
    role: str,
    *,
    frame_slots: int,
    tx_bits: int = 0,
    rx_bits: int = 0,
    tx_active_slots: int = 0,
    rx_active_slots: int = 0,
    peer_count: int = 1,
    multicast_replicas: int = 0,
) -> Dict[str, Any]:
    return {
        "id": identifier,
        "role": role,
        "frame_slots": frame_slots,
        "tx_bits": tx_bits,
        "rx_bits": rx_bits,
        "tx_active_slots": tx_active_slots,
        "rx_active_slots": rx_active_slots,
        "peer_count": peer_count,
        "multicast_replicas": multicast_replicas,
    }


def generate_open_transport_matrix() -> Dict[str, Any]:
    """Return the fixed fit/holdout matrix used for release characterization."""

    fit = [
        _case("fit-shell-f2", "fit", frame_slots=2),
        _case("fit-shell-f4", "fit", frame_slots=4),
        _case("fit-shell-f8", "fit", frame_slots=8),
        _case("fit-shell-f16", "fit", frame_slots=16),
        _case("fit-tx1", "fit", frame_slots=2, tx_bits=1, tx_active_slots=1),
        _case("fit-tx8", "fit", frame_slots=2, tx_bits=8, tx_active_slots=1),
        _case("fit-tx32", "fit", frame_slots=2, tx_bits=32, tx_active_slots=1),
        _case("fit-rx1", "fit", frame_slots=2, rx_bits=1, rx_active_slots=1),
        _case("fit-rx8", "fit", frame_slots=2, rx_bits=8, rx_active_slots=1),
        _case("fit-rx32", "fit", frame_slots=2, rx_bits=32, rx_active_slots=1),
        _case(
            "fit-mixed8", "fit", frame_slots=2, tx_bits=8, rx_bits=8,
            tx_active_slots=1, rx_active_slots=1,
        ),
        _case(
            "fit-tx-slots", "fit", frame_slots=4, tx_bits=16,
            tx_active_slots=2,
        ),
        _case(
            "fit-rx-slots", "fit", frame_slots=4, rx_bits=16,
            rx_active_slots=2,
        ),
        _case(
            "fit-mixed-slots", "fit", frame_slots=8, tx_bits=24, rx_bits=12,
            tx_active_slots=4, rx_active_slots=2,
        ),
        _case(
            "fit-two-peer-tx", "fit", frame_slots=4, tx_bits=16,
            tx_active_slots=2, peer_count=2,
        ),
        _case(
            "fit-two-peer-rx", "fit", frame_slots=4, rx_bits=16,
            rx_active_slots=2, peer_count=2,
        ),
        _case(
            "fit-three-peer-mixed", "fit", frame_slots=8, tx_bits=18,
            rx_bits=9, tx_active_slots=3, rx_active_slots=3, peer_count=3,
        ),
        _case(
            "fit-multicast4", "fit", frame_slots=4, tx_bits=8,
            tx_active_slots=2, peer_count=2, multicast_replicas=4,
        ),
        _case(
            "fit-multicast8", "fit", frame_slots=8, tx_bits=8,
            tx_active_slots=2, peer_count=3, multicast_replicas=8,
        ),
    ]
    holdout = [
        _case(
            "holdout-small-mixed", "holdout", frame_slots=2, tx_bits=3,
            rx_bits=5, tx_active_slots=1, rx_active_slots=1,
        ),
        _case(
            "holdout-f4-mixed", "holdout", frame_slots=4, tx_bits=12,
            rx_bits=7, tx_active_slots=3, rx_active_slots=2,
        ),
        _case(
            "holdout-f8-two-peer", "holdout", frame_slots=8, tx_bits=48,
            rx_bits=24, tx_active_slots=4, rx_active_slots=4, peer_count=2,
        ),
        _case(
            "holdout-f4-multicast", "holdout", frame_slots=4, tx_bits=12,
            tx_active_slots=3, peer_count=3, multicast_replicas=6,
        ),
        _case(
            "holdout-f16-shell", "holdout", frame_slots=16,
        ),
        _case(
            "holdout-f16-large", "holdout", frame_slots=16, tx_bits=64,
            rx_bits=32, tx_active_slots=8, rx_active_slots=4, peer_count=3,
            multicast_replicas=12,
        ),
    ]
    value = {
        "schema": OPEN_TRANSPORT_MATRIX_SCHEMA,
        "model": OPEN_TRANSPORT_MODEL,
        "cases": fit + holdout,
    }
    validate_open_transport_matrix(value)
    value["matrix_sha256"] = _sha256_json(value)
    return value


def validate_open_transport_matrix(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != OPEN_TRANSPORT_MATRIX_SCHEMA:
        raise ValidationError("open transport matrix schema is invalid")
    if value.get("model") != OPEN_TRANSPORT_MODEL:
        raise ValidationError("open transport matrix model is invalid")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValidationError("open transport matrix requires cases")
    identifiers = set()
    roles = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValidationError("open transport matrix case is invalid")
        identifier = case.get("id")
        role = case.get("role")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValidationError("open transport matrix case id is invalid or duplicated")
        identifiers.add(identifier)
        if role not in {"fit", "holdout"}:
            raise ValidationError("open transport matrix role is invalid")
        roles.add(role)
        frame_slots = _positive_int(case.get("frame_slots"), "frame_slots")
        if frame_slots not in FRAME_SLOTS:
            raise ValidationError("open transport frame_slots is outside the characterized set")
        tx_bits = _nonnegative_int(case.get("tx_bits"), "tx_bits")
        rx_bits = _nonnegative_int(case.get("rx_bits"), "rx_bits")
        tx_slots = _nonnegative_int(case.get("tx_active_slots"), "tx_active_slots")
        rx_slots = _nonnegative_int(case.get("rx_active_slots"), "rx_active_slots")
        peers = _positive_int(case.get("peer_count"), "peer_count")
        replicas = _nonnegative_int(case.get("multicast_replicas"), "multicast_replicas")
        if tx_slots > frame_slots or rx_slots > frame_slots:
            raise ValidationError("open transport active slots exceed frame_slots")
        if (tx_bits == 0) != (tx_slots == 0) or (rx_bits == 0) != (rx_slots == 0):
            raise ValidationError("open transport active slots disagree with transported bits")
        if replicas and (tx_bits == 0 or peers < 2):
            raise ValidationError("open transport multicast requires TX bits and multiple peers")
    if roles != {"fit", "holdout"}:
        raise ValidationError("open transport matrix requires fit and holdout cases")
    return {"status": "pass", "case_count": len(cases)}


def _build_case(case: Mapping[str, Any]) -> tuple[Platform, Dict[str, Any], Dict[str, float]]:
    frame_slots = int(case["frame_slots"])
    tx_bits = int(case["tx_bits"])
    rx_bits = int(case["rx_bits"])
    tx_slots = int(case["tx_active_slots"])
    rx_slots = int(case["rx_active_slots"])
    peer_count = int(case["peer_count"])
    replicas = int(case["multicast_replicas"])
    required_lane_uses = max(1, tx_bits + replicas, rx_bits)
    link_width = max(64, int(math.ceil(required_lane_uses / peer_count)))
    boarddb = {
        "schema": "emuflow.boarddb/v1",
        "platform": {
            "name": "open-transport-characterization",
            "kind": "virtual",
        },
        "fpgas": [
            {
                "id": f"F{index}",
                "part": "academic-xcvu19p-behavioral",
                "utilization_limit": 1.0,
                "capacity": {"lut": 1_000_000, "ff": 2_000_000},
            }
            for index in range(peer_count + 1)
        ],
        "links": [
            {
                "id": f"link-f0-f{index}",
                "endpoints": ["F0", f"F{index}"],
                "direction": "full_duplex",
                "mode": "abstract",
                "data_lanes_per_direction": link_width,
                "fabric_clock_mhz": 250.0,
                "latency_cycles": 1,
                "capacity_sharing": "per_direction",
            }
            for index in range(1, peer_count + 1)
        ],
    }
    platform = Platform.from_dict(boarddb)
    source_signals = [
        {"signal": f"net:tx:{index}", "index": index}
        for index in range(tx_bits)
    ]
    shadow_signals = [
        {"signal": f"net:rx:{index}", "index": index}
        for index in range(rx_bits)
    ]
    endpoints = []
    used: dict[tuple[str, int], set[int]] = {}

    def reserve_lane(link: str, slot: int) -> int:
        lanes = used.setdefault((link, slot), set())
        lane = 0
        while lane in lanes:
            lane += 1
        if lane >= link_width:
            raise ValidationError("open transport case exceeds characterization link width")
        lanes.add(lane)
        return lane

    primary_peers = []
    for index in range(tx_bits):
        peer_index = index % peer_count + 1
        primary_peers.append(peer_index)
        slot = index % tx_slots
        link = f"link-f0-f{peer_index}"
        endpoints.append(
            {
                "kind": "tx",
                "link": link,
                "peer": f"F{peer_index}",
                "signal": f"net:tx:{index}",
                "slot": slot,
                "lane": reserve_lane(link, slot),
            }
        )
    for replica in range(replicas):
        source_index = replica % tx_bits
        peer_index = (primary_peers[source_index] + replica) % peer_count + 1
        if peer_index == primary_peers[source_index]:
            peer_index = peer_index % peer_count + 1
        slot = replica % tx_slots
        link = f"link-f0-f{peer_index}"
        endpoints.append(
            {
                "kind": "tx",
                "link": link,
                "peer": f"F{peer_index}",
                "signal": f"net:tx:{source_index}",
                "slot": slot,
                "lane": reserve_lane(link, slot),
            }
        )
    for index in range(rx_bits):
        peer_index = index % peer_count + 1
        slot = index % rx_slots
        link = f"link-f0-f{peer_index}"
        endpoints.append(
            {
                "kind": "rx",
                "link": link,
                "peer": f"F{peer_index}",
                "signal": f"net:rx:{index}",
                "slot": slot,
                "arrival_slot": slot,
                "lane": reserve_lane(link, slot),
            }
        )
    transport = {
        "fpga": "F0",
        "frame_slots": frame_slots,
        "cut_mode": "register-only",
        "source_signals": source_signals,
        "shadow_signals": shadow_signals,
        "endpoints": endpoints,
    }
    features = extract_transport_features(transport)
    return platform, transport, features


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
    if not isinstance(endpoints, list) or not isinstance(shadow_signals, list):
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


def _tool_version(executable: str) -> str:
    completed = subprocess.run(
        [executable, "-V"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValidationError("open transport characterization cannot identify Yosys")
    return completed.stdout.strip().splitlines()[0]


def characterize_open_transport_case(
    case: Mapping[str, Any],
    *,
    yosys_executable: str,
    work_root: Path,
    mapper: Callable[..., Mapping[str, Any]] = run_xilinx_ultrascaleplus_yosys,
) -> Dict[str, Any]:
    """Map one exact production transport shell and return compact evidence."""

    validate_open_transport_matrix(
        {"schema": OPEN_TRANSPORT_MATRIX_SCHEMA, "model": OPEN_TRANSPORT_MODEL,
         "cases": [dict(case, role="fit"), dict(case, id=f"{case['id']}-holdout", role="holdout")]}
    )
    work_root.mkdir(parents=True, exist_ok=True)
    platform, transport, features = _build_case(case)
    transport_rtl = transport_to_systemverilog(transport, platform)
    runtime_rtl = virtual_runtime_controller_to_systemverilog()
    started = time.monotonic()
    temporary = Path(tempfile.mkdtemp(prefix=f"{case['id']}.", dir=work_root))
    try:
        transport_path = temporary / "transport.sv"
        runtime_path = temporary / "runtime.sv"
        mapped_path = temporary / "mapped.json"
        log_path = temporary / "yosys.log"
        transport_path.write_text(transport_rtl, encoding="utf-8")
        runtime_path.write_text(runtime_rtl, encoding="utf-8")
        report = dict(
            mapper(
                [transport_path, runtime_path],
                "emuflow_transport_F0",
                mapped_path,
                executable=yosys_executable,
                log_path=log_path,
            )
        )
        audit = report.get("primitive_audit")
        if not isinstance(audit, dict) or audit.get("status") != "pass":
            raise ValidationError("open transport mapping lacks a passing primitive audit")
        raw_resources = audit.get("resource_totals", {})
        if not isinstance(raw_resources, dict):
            raise ValidationError("open transport primitive resources are invalid")
        resources = {name: int(raw_resources.get(name, 0)) for name in RESOURCE_NAMES}
        if any(value < 0 for value in resources.values()):
            raise ValidationError("open transport primitive resources are negative")
        observation = {
            "schema": OPEN_TRANSPORT_OBSERVATION_SCHEMA,
            "identity": {"id": case["id"], "role": case["role"]},
            "model": OPEN_TRANSPORT_MODEL,
            "case": dict(case),
            "features": features,
            "resources": resources,
            "mapping": {
                "provider": report.get("provider"),
                "family": report.get("family"),
                "mapping_profile": report.get("mapping_profile"),
                "yosys_version": _tool_version(yosys_executable),
                "primitive_library_sha256": audit.get("primitive_library_sha256"),
                "mapped_json_sha256": audit.get("mapped_json_sha256"),
                "cell_types": audit.get("cell_types", {}),
                "unknown_cell_types": audit.get("unknown_cell_types", {}),
                "external_macros": audit.get("external_macros", {}),
            },
            "source": {
                "transport_sv_sha256": _sha256_bytes(transport_rtl.encode("utf-8")),
                "runtime_sv_sha256": _sha256_bytes(runtime_rtl.encode("utf-8")),
                "generator": "emuflow.netlist.transport_to_systemverilog",
                "runtime_generator": "emuflow.runtime.virtual_runtime_controller_to_systemverilog",
            },
            "runtime_seconds": time.monotonic() - started,
            "provenance": {"class": OPEN_TRANSPORT_PROVENANCE},
        }
        validate_open_transport_observation(observation)
        observation["observation_sha256"] = _sha256_json(observation)
        return observation
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def validate_open_transport_observation(value: Mapping[str, Any]) -> Dict[str, Any]:
    if value.get("schema") != OPEN_TRANSPORT_OBSERVATION_SCHEMA:
        raise ValidationError("open transport observation schema is invalid")
    identity = value.get("identity")
    if not isinstance(identity, dict) or identity.get("role") not in {"fit", "holdout"}:
        raise ValidationError("open transport observation identity is invalid")
    if value.get("model") != OPEN_TRANSPORT_MODEL:
        raise ValidationError("open transport observation model is invalid")
    features = value.get("features")
    expected = set(BASE_FEATURE_NAMES) | {f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]}
    if not isinstance(features, dict) or set(features) != expected:
        raise ValidationError("open transport observation features are invalid")
    if any(
        isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount < 0
        for amount in features.values()
    ):
        raise ValidationError("open transport observation feature is invalid")
    resources = value.get("resources")
    if not isinstance(resources, dict) or set(resources) != set(RESOURCE_NAMES):
        raise ValidationError("open transport observation resources are invalid")
    if any(isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 for amount in resources.values()):
        raise ValidationError("open transport observation resource total is invalid")
    mapping = value.get("mapping")
    if (
        not isinstance(mapping, dict)
        or mapping.get("mapping_profile") != "xilinx-ultrascaleplus-open-v1"
        or mapping.get("unknown_cell_types") != {}
        or mapping.get("external_macros") != {}
    ):
        raise ValidationError("open transport observation mapping audit is invalid")
    provenance = value.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("class") != OPEN_TRANSPORT_PROVENANCE:
        raise ValidationError("open transport observation provenance is invalid")
    return {"status": "pass", "id": identity.get("id")}


def run_open_transport_matrix(
    matrix: Mapping[str, Any],
    *,
    output_root: Path,
    work_root: Path,
    yosys_executable: str,
) -> Dict[str, Any]:
    validate_open_transport_matrix(matrix)
    output_root.mkdir(parents=True, exist_ok=True)
    observations = []
    for case in matrix["cases"]:
        observation = characterize_open_transport_case(
            case,
            yosys_executable=yosys_executable,
            work_root=work_root,
        )
        path = output_root / f"{case['id']}.json"
        write_json(path, observation, compact=True)
        observations.append(path.name)
    summary = {
        "schema": "emuflow.open-transport-campaign-summary/v1",
        "status": "pass",
        "matrix_sha256": matrix.get("matrix_sha256", _sha256_json(matrix)),
        "case_count": len(observations),
        "observations": observations,
    }
    # Keep the observation directory homogeneous so a shell glob cannot feed
    # campaign metadata into the observation fitter.
    write_json(
        output_root.with_name(f"{output_root.name}-summary.json"),
        summary,
        compact=True,
    )
    return summary


def _nnls(features: Sequence[Sequence[float]], targets: Sequence[float]) -> list[float]:
    if not features or len(features) != len(targets):
        raise ValidationError("open transport fit has no aligned samples")
    width = len(features[0])
    coefficients = [0.0] * width
    predictions = [0.0] * len(targets)
    for _ in range(4000):
        largest_change = 0.0
        for column in range(width):
            denominator = sum(row[column] ** 2 for row in features)
            if denominator == 0:
                continue
            old = coefficients[column]
            numerator = sum(
                row[column] * (target - (prediction - row[column] * old))
                for row, target, prediction in zip(features, targets, predictions)
            )
            new = max(0.0, numerator / denominator)
            delta = new - old
            if delta:
                for index, row in enumerate(features):
                    predictions[index] += row[column] * delta
            coefficients[column] = new
            largest_change = max(largest_change, abs(delta))
        if largest_change < 1e-10:
            break
    return coefficients


def _matrix_rank(rows: Sequence[Sequence[float]], tolerance: float = 1e-10) -> int:
    matrix = [list(map(float, row)) for row in rows]
    if not matrix:
        return 0
    columns = len(matrix[0])
    rank = 0
    for column in range(columns):
        pivot = max(range(rank, len(matrix)), key=lambda index: abs(matrix[index][column]))
        if abs(matrix[pivot][column]) <= tolerance:
            continue
        matrix[rank], matrix[pivot] = matrix[pivot], matrix[rank]
        scale = matrix[rank][column]
        matrix[rank] = [value / scale for value in matrix[rank]]
        for row_index in range(len(matrix)):
            if row_index == rank:
                continue
            factor = matrix[row_index][column]
            if abs(factor) <= tolerance:
                continue
            matrix[row_index] = [
                value - factor * pivot_value
                for value, pivot_value in zip(matrix[row_index], matrix[rank])
            ]
        rank += 1
        if rank == len(matrix) or rank == columns:
            break
    return rank


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def fit_open_transport_cost_model(
    observations: Sequence[Mapping[str, Any]], *, bootstrap_samples: int = 128
) -> Dict[str, Any]:
    """Fit the production RTL model and enforce disjoint holdout accuracy."""

    if bootstrap_samples < 16:
        raise ValidationError("open transport bootstrap_samples must be >= 16")
    normalized = []
    for observation in observations:
        validate_open_transport_observation(observation)
        normalized.append(dict(observation))
    if not normalized:
        raise ValidationError("open transport fitting requires observations")
    feature_names = list(BASE_FEATURE_NAMES) + [
        f"frame_slots_{slots}" for slots in FRAME_SLOTS[1:]
    ]
    fit = [item for item in normalized if item["identity"]["role"] == "fit"]
    holdout = [item for item in normalized if item["identity"]["role"] == "holdout"]
    if len(fit) <= len(feature_names) or not holdout:
        raise ValidationError("open transport fitting lacks fit or holdout coverage")
    rows = [[float(item["features"][name]) for name in feature_names] for item in fit]
    if _matrix_rank(rows) != len(feature_names):
        raise ValidationError("open transport fit matrix does not identify all coefficients")
    versions = {item["mapping"]["yosys_version"] for item in normalized}
    library_hashes = {item["mapping"]["primitive_library_sha256"] for item in normalized}
    profiles = {item["mapping"]["mapping_profile"] for item in normalized}
    if len(versions) != 1 or len(library_hashes) != 1 or len(profiles) != 1:
        raise ValidationError("open transport observations mix mapper identities")

    generator = random.Random(0)
    resources = {}
    predictions_by_resource: dict[str, list[float]] = {}
    for resource in RESOURCE_NAMES:
        targets = [float(item["resources"][resource]) for item in fit]
        structurally_zero = not any(
            item["resources"][resource] for item in normalized
        )
        if structurally_zero:
            coefficients = [0.0] * len(feature_names)
            boot = [[0.0] * bootstrap_samples for _ in feature_names]
            residuals = [0.0] * len(targets)
        else:
            coefficients = _nnls(rows, targets)
            predictions = [
                sum(value * coefficient for value, coefficient in zip(row, coefficients))
                for row in rows
            ]
            residuals = [target - prediction for target, prediction in zip(targets, predictions)]
            boot = [[] for _ in feature_names]
            for _ in range(bootstrap_samples):
                sampled_targets = [
                    max(0.0, prediction + residuals[generator.randrange(len(residuals))])
                    for prediction in predictions
                ]
                sampled = _nnls(rows, sampled_targets)
                for index, value in enumerate(sampled):
                    boot[index].append(value)
        fit_predictions = [
            sum(value * coefficient for value, coefficient in zip(row, coefficients))
            for row in rows
        ]
        rmse = math.sqrt(
            sum((target - prediction) ** 2 for target, prediction in zip(targets, fit_predictions))
            / len(targets)
        )
        resources[resource] = {
            "status": "structural_zero" if structurally_zero else "mapped_fit",
            "parameters": {
                name: {
                    "nominal": coefficients[index],
                    "aggressive": _percentile(boot[index], 0.05),
                    "conservative": _percentile(boot[index], 0.95),
                    "identifiable": True,
                }
                for index, name in enumerate(feature_names)
            },
            "fit_rmse": rmse,
        }
        predictions_by_resource[resource] = coefficients

    holdout_checks = []
    all_identifiable = True
    for item in holdout:
        row = [float(item["features"][name]) for name in feature_names]
        check = {"id": item["identity"]["id"], "resources": {}}
        for resource in RESOURCE_NAMES:
            coefficients = predictions_by_resource[resource]
            predicted = sum(value * coefficient for value, coefficient in zip(row, coefficients))
            actual = float(item["resources"][resource])
            absolute = abs(predicted - actual)
            relative = absolute / actual if actual else None
            passed = absolute <= ZERO_ABSOLUTE_TOLERANCE if actual == 0 else relative <= MAX_HOLDOUT_RELATIVE_ERROR
            check["resources"][resource] = {
                "actual": actual,
                "predicted": predicted,
                "absolute_error": absolute,
                "relative_error": relative,
                "passed": passed,
            }
            all_identifiable = all_identifiable and passed
        holdout_checks.append(check)
    for resource_name, resource in resources.items():
        identifiable = all(
            check["resources"][resource_name]["passed"] for check in holdout_checks
        )
        for parameter in resource["parameters"].values():
            parameter["identifiable"] = identifiable
    return {
        "schema": OPEN_TRANSPORT_FIT_SCHEMA,
        "model": OPEN_TRANSPORT_MODEL,
        "feature_names": feature_names,
        "resources": resources,
        "fit_samples": len(fit),
        "holdout_checks": holdout_checks,
        "all_resources_identifiable": all_identifiable,
        "excluded_observations": 0,
        "provenance": {
            "class": OPEN_TRANSPORT_PROVENANCE,
            "mapping_profile": next(iter(profiles)),
            "yosys_version": next(iter(versions)),
            "primitive_library_sha256": next(iter(library_hashes)),
            "observation_sha256s": sorted(
                item.get("observation_sha256", _sha256_json(item)) for item in normalized
            ),
        },
    }


def read_open_transport_observations(paths: Sequence[Path]) -> list[Dict[str, Any]]:
    return [read_json(path.resolve()) for path in paths]
