"""Source-qualified Chimew Section 3.4 bank/channel assignment.

The native kernel computes Algorithm 2 costs and both min-cost-flow stages.
Python validates the physical model and checks linear-size primal/residual-dual
certificates.  It intentionally does not run a second package-pin optimizer on
large inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_executable


CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1 = "emuflow.chimew-bank-channel-input/v1"
CHIMEW_BANK_CHANNEL_INPUT_SCHEMA = "emuflow.chimew-bank-channel-input/v2"
CHIMEW_BANK_CHANNEL_INPUT_PROVIDER = "source-qualified-physical-bank-channel-v1"
CHIMEW_BANK_CHANNEL_REPORT_SCHEMA = "emuflow.chimew-bank-channel-report/v1"
CHIMEW_BANK_CHANNEL_PROVIDER = "chimew-section3.4-two-stage-assignment-v1"


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}: expected a non-empty string")
    return value


def _integer(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{label}: expected an integer >= {minimum}")
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}: expected a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0.0):
        qualifier = "positive " if positive else ""
        raise ValidationError(f"{label}: expected a finite {qualifier}number")
    return result


def _digest(value: Any, label: str) -> str:
    digest = _string(value, label).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValidationError(f"{label}: expected a SHA-256 digest")
    return digest


def _point(value: Any, label: str) -> Tuple[float, float]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label}: expected a physical point")
    return _number(value.get("x"), f"{label}.x"), _number(
        value.get("y"), f"{label}.y"
    )


def validate_chimew_bank_channel_input(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate source-qualified physical groups, banks, channels, and sites."""

    schema = document.get("schema")
    if schema not in {
        CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1,
        CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
    }:
        raise ValidationError("Chimew bank/channel input schema is invalid")
    if document.get("provider") != CHIMEW_BANK_CHANNEL_INPUT_PROVIDER:
        raise ValidationError("Chimew bank/channel input is not source-qualified")
    if document.get("coordinate_system") != "physical-site-xy":
        raise ValidationError("Chimew bank/channel assignment rejects normalized coordinates")
    design = _string(document.get("design"), "chimew.assignment.design")
    platform = _string(document.get("platform"), "chimew.assignment.platform")
    cost_scale = _integer(
        document.get("cost_quantization_per_site"),
        "chimew.assignment.cost_quantization_per_site",
        1,
    )
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("Chimew bank/channel provenance is missing")
    normalized_provenance = {
        "producer": _string(provenance.get("producer"), "chimew.assignment.producer"),
        "producer_version": _string(
            provenance.get("producer_version"), "chimew.assignment.producer_version"
        ),
    }
    for field in ("grouping_sha256", "placement_sha256", "architecture_sha256"):
        normalized_provenance[field] = _digest(
            provenance.get(field), f"chimew.assignment.{field}"
        )

    raw_domains = document.get("domains")
    if not isinstance(raw_domains, list) or not raw_domains:
        raise ValidationError("Chimew bank/channel domains are missing")
    domains = []
    domain_by_id = {}
    for index, raw in enumerate(raw_domains):
        if not isinstance(raw, dict):
            raise ValidationError(f"chimew.assignment.domains[{index}]: expected an object")
        domain_id = _string(raw.get("id"), f"chimew.assignment.domains[{index}].id")
        fpga_a = _string(raw.get("fpga_a"), f"{domain_id}.fpga_a")
        fpga_b = _string(raw.get("fpga_b"), f"{domain_id}.fpga_b")
        if fpga_a == fpga_b or domain_id in domain_by_id:
            raise ValidationError(f"invalid or duplicate Chimew domain {domain_id!r}")
        domain_by_id[domain_id] = index
        domains.append({"id": domain_id, "fpga_a": fpga_a, "fpga_b": fpga_b})

    raw_banks = document.get("bank_pairs")
    if not isinstance(raw_banks, list) or not raw_banks:
        raise ValidationError("Chimew bank pairs are missing")
    banks = []
    seen_bank_ids = set()
    channel_count = 0
    seen_channel_ids = set()
    for index, raw in enumerate(raw_banks):
        if not isinstance(raw, dict):
            raise ValidationError(f"chimew.assignment.bank_pairs[{index}]: expected an object")
        bank_id = _string(raw.get("id"), f"chimew.assignment.bank_pairs[{index}].id")
        domain_id = _string(raw.get("domain"), f"{bank_id}.domain")
        if bank_id in seen_bank_ids or domain_id not in domain_by_id:
            raise ValidationError(f"invalid or duplicate Chimew bank pair {bank_id!r}")
        seen_bank_ids.add(bank_id)
        bank_a = raw.get("bank_a")
        bank_b = raw.get("bank_b")
        if not isinstance(bank_a, dict) or not isinstance(bank_b, dict):
            raise ValidationError(f"Chimew bank pair {bank_id!r} endpoints are missing")
        normalized_bank_a = {
            "id": _string(bank_a.get("id"), f"{bank_id}.bank_a.id"),
            "point": _point(bank_a, f"{bank_id}.bank_a"),
        }
        normalized_bank_b = {
            "id": _string(bank_b.get("id"), f"{bank_id}.bank_b.id"),
            "point": _point(bank_b, f"{bank_id}.bank_b"),
        }
        raw_channels = raw.get("channels")
        if not isinstance(raw_channels, list) or not raw_channels:
            raise ValidationError(f"Chimew bank pair {bank_id!r} has no channels")
        channels = []
        orders = set()
        for channel_index, raw_channel in enumerate(raw_channels):
            if not isinstance(raw_channel, dict):
                raise ValidationError(f"{bank_id}.channels[{channel_index}] is invalid")
            channel_id = _string(
                raw_channel.get("id"), f"{bank_id}.channels[{channel_index}].id"
            )
            order = _integer(raw_channel.get("order"), f"{channel_id}.order")
            if channel_id in seen_channel_ids or order in orders:
                raise ValidationError(f"duplicate Chimew channel/order in {bank_id!r}")
            seen_channel_ids.add(channel_id)
            orders.add(order)
            channels.append(
                {
                    "id": channel_id,
                    "index": channel_count,
                    "order": order,
                    "pin_a": _point(raw_channel.get("pin_a"), f"{channel_id}.pin_a"),
                    "pin_b": _point(raw_channel.get("pin_b"), f"{channel_id}.pin_b"),
                }
            )
            channel_count += 1
        if orders != set(range(len(channels))):
            raise ValidationError(f"Chimew channel order for {bank_id!r} is not contiguous")
        channels.sort(key=lambda channel: channel["order"])
        banks.append(
            {
                "id": bank_id,
                "domain": domain_by_id[domain_id],
                "bank_a": normalized_bank_a,
                "bank_b": normalized_bank_b,
                "channels": channels,
            }
        )

    raw_groups = document.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValidationError("Chimew signal groups are missing")
    groups = []
    seen_group_ids = set()
    signal_count = fanin_count = 0
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, dict):
            raise ValidationError(f"chimew.assignment.groups[{index}]: expected an object")
        group_id = _string(raw.get("id"), f"chimew.assignment.groups[{index}].id")
        domain_id = _string(raw.get("domain"), f"{group_id}.domain")
        kind = raw.get("kind")
        direction = raw.get("direction")
        if group_id in seen_group_ids or domain_id not in domain_by_id:
            raise ValidationError(f"invalid or duplicate Chimew group {group_id!r}")
        if kind not in ("tdm_group", "common_signal"):
            raise ValidationError(f"invalid Chimew group kind for {group_id!r}")
        allowed_directions = (
            {"a_to_b", "b_to_a"}
            if schema == CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1
            else {"a_to_b", "b_to_a", "bidirectional"}
        )
        if direction not in allowed_directions:
            raise ValidationError(f"invalid Chimew direction for {group_id!r}")
        if kind == "common_signal" and direction == "bidirectional":
            raise ValidationError("a Chimew common signal cannot be bidirectional")
        raw_members = raw.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise ValidationError(f"Chimew group {group_id!r} has no signals")
        if kind == "common_signal" and len(raw_members) != 1:
            raise ValidationError("a Chimew common signal must be a singleton group")
        seen_group_ids.add(group_id)
        members = []
        seen_member_ids = set()
        for member_index, raw_member in enumerate(raw_members):
            if not isinstance(raw_member, dict):
                raise ValidationError(f"{group_id}.members[{member_index}] is invalid")
            member_id = _string(raw_member.get("id"), f"{group_id}.member.id")
            raw_fanins = raw_member.get("fanins")
            if member_id in seen_member_ids or not isinstance(raw_fanins, list) or not raw_fanins:
                raise ValidationError(f"invalid Chimew signal member {member_id!r}")
            seen_member_ids.add(member_id)
            timing_weight = raw_member.get("timing_weight", 1.0)
            timing_weight = _number(
                timing_weight,
                f"{group_id}.{member_id}.timing_weight",
                positive=True,
            )
            fanins = [
                _point(point, f"{group_id}.{member_id}.fanins[{fanin_index}]")
                for fanin_index, point in enumerate(raw_fanins)
            ]
            member_direction = raw_member.get("direction", direction)
            if member_direction not in ("a_to_b", "b_to_a") or (
                direction != "bidirectional" and member_direction != direction
            ):
                raise ValidationError(
                    f"invalid Chimew member direction for {member_id!r}"
                )
            members.append(
                {
                    "id": member_id,
                    "direction": 0 if member_direction == "a_to_b" else 1,
                    "timing_weight": timing_weight,
                    "fanout": _point(raw_member.get("fanout"), f"{group_id}.{member_id}.fanout"),
                    "fanins": fanins,
                }
            )
            signal_count += 1
            fanin_count += len(fanins)
        if direction == "bidirectional" and {
            member["direction"] for member in members
        } != {0, 1}:
            raise ValidationError(
                "a bidirectional Chimew bundle must contain both directions"
            )
        groups.append(
            {
                "id": group_id,
                "domain": domain_by_id[domain_id],
                "kind": 0 if kind == "tdm_group" else 1,
                "direction": (
                    0
                    if direction == "a_to_b"
                    else 1 if direction == "b_to_a" else 2
                ),
                "members": members,
            }
        )

    for domain_index, domain in enumerate(domains):
        capacity = sum(
            len(bank["channels"]) for bank in banks if bank["domain"] == domain_index
        )
        demand = sum(group["domain"] == domain_index for group in groups)
        if capacity < demand:
            raise ValidationError(
                f"Chimew domain {domain['id']!r} has insufficient channel capacity"
            )
    metrics = document.get("metrics")
    expected_metrics = {
        "groups": len(groups),
        "signals": signal_count,
        "fanins": fanin_count,
        "bank_pairs": len(banks),
        "channels": channel_count,
    }
    if not isinstance(metrics, dict) or any(
        metrics.get(field) != value for field, value in expected_metrics.items()
    ):
        raise ValidationError("Chimew bank/channel input metrics do not agree")
    expected_metrics["bidirectional_bundles"] = sum(
        group["direction"] == 2 for group in groups
    )
    declared_bidirectional = metrics.get("bidirectional_bundles")
    if declared_bidirectional is not None and (
        isinstance(declared_bidirectional, bool)
        or declared_bidirectional != expected_metrics["bidirectional_bundles"]
    ):
        raise ValidationError("Chimew bidirectional-bundle metrics do not agree")
    return {
        "design": design,
        "platform": platform,
        "cost_scale": cost_scale,
        "provenance": normalized_provenance,
        "domains": domains,
        "banks": banks,
        "groups": groups,
        "metrics": expected_metrics,
    }


def _distance(lhs: Tuple[float, float], rhs: Tuple[float, float]) -> float:
    return abs(lhs[0] - rhs[0]) + abs(lhs[1] - rhs[1])


def _raw_cost(group: Mapping[str, Any], endpoint_a: Tuple[float, float], endpoint_b: Tuple[float, float]) -> float:
    result = 0.0
    for member in group["members"]:
        output, input_point = (
            (endpoint_a, endpoint_b)
            if member.get("direction", group["direction"]) == 0
            else (endpoint_b, endpoint_a)
        )
        result += member.get("timing_weight", 1.0) * (
            _distance(member["fanout"], output)
            + sum(
                _distance(fanin, input_point) for fanin in member["fanins"]
            )
            / len(member["fanins"])
        )
    return result


def _ranked_cost(raw: float, scale: int) -> int:
    return math.floor(raw * scale + 0.5)


def _candidate_cost(group: Mapping[str, Any], endpoint_a: Tuple[float, float], endpoint_b: Tuple[float, float], scale: int) -> int:
    return _ranked_cost(_raw_cost(group, endpoint_a, endpoint_b), scale)


def _serialize(problem: Mapping[str, Any], path: Path) -> None:
    lines = [
        "EMUFLOW_CHIMEW_BANK_CHANNEL_INPUT_V2",
        f"PARAM {problem['cost_scale']}",
    ]
    for bank_index, bank in enumerate(problem["banks"]):
        point_a = bank["bank_a"]["point"]
        point_b = bank["bank_b"]["point"]
        lines.append(
            f"BANK {bank_index} {bank['domain']} {point_a[0]:.17g} {point_a[1]:.17g} {point_b[0]:.17g} {point_b[1]:.17g}"
        )
        for channel in bank["channels"]:
            pin_a, pin_b = channel["pin_a"], channel["pin_b"]
            lines.append(
                f"CHANNEL {channel['index']} {bank_index} {channel['order']} {pin_a[0]:.17g} {pin_a[1]:.17g} {pin_b[0]:.17g} {pin_b[1]:.17g}"
            )
    for group_index, group in enumerate(problem["groups"]):
        lines.append(
            f"GROUP {group_index} {group['domain']} {group['kind']} {group['direction']} {len(group['members'])}"
        )
        for member_index, member in enumerate(group["members"]):
            fanout = member["fanout"]
            fanins = " ".join(f"{point[0]:.17g} {point[1]:.17g}" for point in member["fanins"])
            lines.append(
                f"MEMBER {group_index} {member_index} {member['direction']} {member['timing_weight']:.17g} {fanout[0]:.17g} {fanout[1]:.17g} {len(member['fanins'])} {fanins}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_native(problem: Mapping[str, Any], executable: Optional[str]) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="emuflow-chimew-assignment-") as temporary:
        root = Path(temporary)
        input_path, output_path = root / "input.txt", root / "output.txt"
        _serialize(problem, input_path)
        command = resolve_native_executable("emuflow_chimew_bank_channel_assigner", executable)
        completed = subprocess.run([command, str(input_path), str(output_path)], text=True, capture_output=True)
        if completed.returncode != 0:
            raise EmuFlowError("Chimew bank/channel kernel failed: " + (completed.stderr.strip() or completed.stdout.strip()))
        lines = output_path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "EMUFLOW_CHIMEW_BANK_CHANNEL_OUTPUT_V1":
        raise EmuFlowError("Chimew bank/channel output header is invalid")
    result: Dict[str, Any] = {
        "bank_assignments": {},
        "chosen": {},
        "alternatives": {},
        "potentials": {},
        "certificate_sizes": {},
    }
    for line in lines[1:]:
        fields = line.split()
        if fields[:1] == ["METRIC"] and len(fields) == 4:
            result["metrics"] = tuple(map(int, fields[1:]))
        elif fields[:1] == ["BANK_ASSIGN"] and len(fields) == 4:
            result["bank_assignments"][int(fields[1])] = (int(fields[2]), int(fields[3]))
        elif fields[:1] == ["CERT"] and len(fields) == 3:
            result["certificate_sizes"][fields[1]] = int(fields[2])
            result["potentials"][fields[1]] = {}
        elif fields[:1] == ["POT"] and len(fields) == 4:
            label = fields[1]
            if label not in result["potentials"]:
                raise EmuFlowError("Chimew certificate appears before its header")
            result["potentials"][label][int(fields[2])] = int(fields[3])
        elif fields[:1] == ["CHOSEN"] and len(fields) == 3:
            result["chosen"][int(fields[1])] = int(fields[2])
        elif fields[:1] == ["ALTERNATIVE"] and len(fields) == 4:
            result["alternatives"][(int(fields[1]), int(fields[2]))] = {
                "cost": int(fields[3]), "assignments": {}
            }
        elif fields[:1] == ["CHANNEL_ASSIGN"] and len(fields) == 6:
            key = (int(fields[1]), int(fields[2]))
            if key not in result["alternatives"]:
                raise EmuFlowError("Chimew channel assignment appears before its alternative")
            result["alternatives"][key]["assignments"][int(fields[3])] = (int(fields[4]), int(fields[5]))
        else:
            raise EmuFlowError("Chimew bank/channel output is malformed")
    for label, size in result["certificate_sizes"].items():
        values = result["potentials"].get(label, {})
        if set(values) != set(range(size)):
            raise EmuFlowError(f"Chimew certificate {label!r} is incomplete")
        result["potentials"][label] = [values[index] for index in range(size)]
    return result


def _verify_certificate(
    right_count: int,
    capacities: Sequence[int],
    left_count: int,
    candidates: Sequence[Tuple[int, int, int]],
    assignments: Mapping[int, Tuple[int, int]],
    potentials: Sequence[int],
    expected_total: int,
) -> None:
    source = 0
    first_right = 1
    first_left = first_right + right_count
    sink = first_left + left_count
    if len(potentials) != sink + 1 or set(assignments) != set(range(left_count)):
        raise EmuFlowError("Chimew assignment certificate dimensions are invalid")
    candidate_costs = {(right, left): cost for right, left, cost in candidates}
    if len(candidate_costs) != len(candidates):
        raise EmuFlowError("Chimew assignment candidate graph contains duplicates")
    used = [0] * right_count
    total = 0
    selected = set()
    for left, (right, cost) in assignments.items():
        if candidate_costs.get((right, left)) != cost:
            raise EmuFlowError("Chimew assignment selects a missing or wrong-cost edge")
        used[right] += 1
        total += cost
        selected.add((right, left))
    if total != expected_total or any(use > capacity for use, capacity in zip(used, capacities)):
        raise EmuFlowError("Chimew assignment primal certificate is invalid")

    residual_edges = []
    for right, capacity in enumerate(capacities):
        if capacity - used[right] > 0:
            residual_edges.append((source, first_right + right, 0))
        if used[right] > 0:
            residual_edges.append((first_right + right, source, 0))
    for right, left, cost in candidates:
        if (right, left) in selected:
            residual_edges.append((first_left + left, first_right + right, -cost))
        else:
            residual_edges.append((first_right + right, first_left + left, cost))
    for left in range(left_count):
        residual_edges.append((sink, first_left + left, 0))
    for start, end, cost in residual_edges:
        if cost + potentials[start] - potentials[end] < 0:
            raise EmuFlowError("Chimew residual-dual certificate has a negative reduced cost")


def _group_cost_signature(group: Mapping[str, Any]) -> Tuple[Any, ...]:
    """Return an exact, identifier-free signature for one Algorithm 2 row."""

    members = []
    for member in group["members"]:
        members.append(
            (
                member.get("timing_weight", 1.0),
                member.get("direction", group["direction"]),
                member["fanout"],
                tuple(sorted(member["fanins"])),
            )
        )
    return group["kind"], group["direction"], tuple(sorted(members))


def _verify_stage2_certificate(
    bank: Mapping[str, Any],
    all_groups: Sequence[Mapping[str, Any]],
    groups: Sequence[int],
    priority: int,
    assignments: Mapping[int, Tuple[int, int]],
    potentials: Sequence[int],
    expected_total: int,
    cost_scale: int,
) -> None:
    """Check the expanded channel matching using exact repeated-row classes.

    Groups with identical physical coordinates, direction, kind, and dual
    potential induce exactly the same residual-cost row.  Checking one
    representative row plus every selected reverse edge proves the original
    expanded certificate without materializing the repeated dense graph.
    """

    right_count = len(bank["channels"])
    left_count = len(groups)
    source = 0
    first_right = 1
    first_left = first_right + right_count
    sink = first_left + left_count
    if len(potentials) != sink + 1 or set(assignments) != set(range(left_count)):
        raise EmuFlowError("Chimew assignment certificate dimensions are invalid")

    direction_counts = [0, 0, 0]
    common_count = 0
    for group_index in groups:
        group = all_groups[group_index]
        if group["kind"] == 0:
            direction_counts[group["direction"]] += 1
        else:
            common_count += 1

    dedicated_direction = None
    if common_count == 0:
        active_directions = [
            direction
            for direction, count in enumerate(direction_counts)
            if count > 0
        ]
        if len(active_directions) == 1:
            dedicated_direction = active_directions[0]

    def eligible(group: Mapping[str, Any], right: int) -> bool:
        # A per-direction BoardDB lane inventory is represented by a bank
        # whose groups are all TDM signals in one direction.  Every channel
        # in that bank is physically available to that direction; applying
        # the paper's shared-bank direction partition here would incorrectly
        # reserve all unused channels for a direction/common-signal class
        # that is absent from the domain.
        if dedicated_direction is not None:
            return (
                group["kind"] == 0
                and group["direction"] == dedicated_direction
            )
        first, second = priority, 1 - priority
        if right < direction_counts[first]:
            return group["kind"] == 0 and group["direction"] == first
        if right < direction_counts[first] + direction_counts[second]:
            return group["kind"] == 0 and group["direction"] == second
        if right < sum(direction_counts):
            return group["kind"] == 0 and group["direction"] == 2
        return group["kind"] == 1

    used = [0] * right_count
    total = 0
    row_classes: Dict[Tuple[Any, ...], list[int]] = {}
    for left, group_index in enumerate(groups):
        right, cost = assignments[left]
        if right < 0 or right >= right_count:
            raise EmuFlowError("Chimew assignment uses a channel from another bank")
        group = all_groups[group_index]
        if not eligible(group, right):
            raise EmuFlowError("Chimew assignment selects an ineligible channel")
        channel = bank["channels"][right]
        expected_cost = _candidate_cost(
            group, channel["pin_a"], channel["pin_b"], cost_scale
        )
        if cost != expected_cost:
            raise EmuFlowError("Chimew assignment selects a wrong-cost edge")
        used[right] += 1
        total += cost
        row_classes.setdefault(
            (_group_cost_signature(group), potentials[first_left + left]), []
        ).append(left)
        reverse_reduced = (
            -cost
            + potentials[first_left + left]
            - potentials[first_right + right]
        )
        if reverse_reduced < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )
    if total != expected_total or any(use > 1 for use in used):
        raise EmuFlowError("Chimew assignment primal certificate is invalid")

    for right, use in enumerate(used):
        start, end = (
            (first_right + right, source)
            if use
            else (source, first_right + right)
        )
        if potentials[start] - potentials[end] < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )
    for left in range(left_count):
        if potentials[sink] - potentials[first_left + left] < 0:
            raise EmuFlowError(
                "Chimew residual-dual certificate has a negative reduced cost"
            )

    for (_, left_potential), members in row_classes.items():
        representative = all_groups[groups[members[0]]]
        selected_right = assignments[members[0]][0] if len(members) == 1 else None
        for right, channel in enumerate(bank["channels"]):
            if not eligible(representative, right) or right == selected_right:
                continue
            cost = _candidate_cost(
                representative,
                channel["pin_a"],
                channel["pin_b"],
                cost_scale,
            )
            if cost + potentials[first_right + right] - left_potential < 0:
                raise EmuFlowError(
                    "Chimew residual-dual certificate has a negative reduced cost"
                )


def evaluate_chimew_bank_channel_assignment(
    document: Mapping[str, Any], *, executable: Optional[str] = None
) -> Dict[str, Any]:
    """Run both paper stages and check exact optimality without replaying them."""

    problem = validate_chimew_bank_channel_input(document)
    native = _run_native(problem, executable)
    group_count, stage1_cost, stage2_cost = native.get("metrics", (-1, -1, -1))
    if group_count != len(problem["groups"]):
        raise EmuFlowError("Chimew bank/channel metrics do not agree")

    bank_candidates = []
    stage1_assignments = {}
    for bank_index, bank in enumerate(problem["banks"]):
        for group_index, group in enumerate(problem["groups"]):
            if bank["domain"] == group["domain"]:
                bank_candidates.append(
                    (
                        bank_index,
                        group_index,
                        _candidate_cost(group, bank["bank_a"]["point"], bank["bank_b"]["point"], problem["cost_scale"]),
                    )
                )
    if set(native["bank_assignments"]) != set(range(len(problem["groups"]))):
        raise EmuFlowError("Chimew bank assignment is incomplete")
    for group_index, (bank_index, cost) in native["bank_assignments"].items():
        stage1_assignments[group_index] = (bank_index, cost)
    _verify_certificate(
        len(problem["banks"]),
        [len(bank["channels"]) for bank in problem["banks"]],
        len(problem["groups"]),
        bank_candidates,
        stage1_assignments,
        native["potentials"].get("STAGE1", []),
        stage1_cost,
    )

    groups_by_bank = [[] for _ in problem["banks"]]
    for group_index in range(len(problem["groups"])):
        groups_by_bank[stage1_assignments[group_index][0]].append(group_index)
    selected_channels = {}
    checked_alternatives = 0
    computed_stage2_cost = 0
    priorities = {}
    for bank_index, groups in enumerate(groups_by_bank):
        if not groups:
            continue
        bank = problem["banks"][bank_index]
        alternatives = []
        channel_position = {channel["index"]: position for position, channel in enumerate(bank["channels"])}
        for priority in (0, 1):
            key = (bank_index, priority)
            alternative = native["alternatives"].get(key)
            if alternative is None:
                raise EmuFlowError("Chimew package-pin alternative is missing")
            local_assignments = {}
            if set(alternative["assignments"]) != set(groups):
                raise EmuFlowError("Chimew package-pin assignment is incomplete")
            for left, group_index in enumerate(groups):
                channel_index, cost = alternative["assignments"][group_index]
                if channel_index not in channel_position:
                    raise EmuFlowError("Chimew assignment uses a channel from another bank")
                local_assignments[left] = (channel_position[channel_index], cost)
            label = f"BANK{bank_index}P{priority}"
            _verify_stage2_certificate(
                bank,
                problem["groups"],
                groups,
                priority,
                local_assignments,
                native["potentials"].get(label, []),
                alternative["cost"],
                problem["cost_scale"],
            )
            alternatives.append(alternative["cost"])
            checked_alternatives += 1
        expected_priority = 1 if alternatives[1] < alternatives[0] else 0
        if native["chosen"].get(bank_index) != expected_priority:
            raise EmuFlowError("Chimew package-pin priority choice is not minimum-cost")
        priorities[bank["id"]] = "a_to_b" if expected_priority == 0 else "b_to_a"
        computed_stage2_cost += alternatives[expected_priority]
        chosen = native["alternatives"][(bank_index, expected_priority)]["assignments"]
        for group_index, (channel_index, cost) in chosen.items():
            selected_channels[group_index] = (channel_index, cost)
    if computed_stage2_cost != stage2_cost:
        raise EmuFlowError("Chimew selected package-pin cost does not agree")

    assignments = []
    stage1_physical_cost = 0.0
    stage2_physical_cost = 0.0
    flat_channels = {
        channel["index"]: (bank_index, channel)
        for bank_index, bank in enumerate(problem["banks"])
        for channel in bank["channels"]
    }
    for group_index, group in enumerate(problem["groups"]):
        bank_index, bank_cost = stage1_assignments[group_index]
        channel_index, channel_cost = selected_channels[group_index]
        channel_bank, channel = flat_channels[channel_index]
        if channel_bank != bank_index:
            raise EmuFlowError("Chimew bank/channel stages disagree")
        bank = problem["banks"][bank_index]
        bank_raw_cost = _raw_cost(
            group, bank["bank_a"]["point"], bank["bank_b"]["point"]
        )
        channel_raw_cost = _raw_cost(group, channel["pin_a"], channel["pin_b"])
        stage1_physical_cost += bank_raw_cost
        stage2_physical_cost += channel_raw_cost
        assignments.append(
            {
                "group": group["id"],
                "bank_pair": problem["banks"][bank_index]["id"],
                "channel": channel["id"],
                "bank_cost_rank": bank_cost,
                "channel_cost_rank": channel_cost,
                "bank_cost": bank_raw_cost,
                "channel_cost": channel_raw_cost,
            }
        )
    canonical_input = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema": CHIMEW_BANK_CHANNEL_REPORT_SCHEMA,
        "status": "standalone_paper_kernel",
        "integration_status": "not-a-phase6-pin-plan",
        "design": problem["design"],
        "platform": problem["platform"],
        "provider": CHIMEW_BANK_CHANNEL_PROVIDER,
        "paper_scope": "FPGA-2026-Sections-3.4.1-through-3.4.3",
        "algorithm2_interpretation": (
            "per-signal-fanout-distance-plus-mean-fanin-distance; optional "
            "source-bound timing weights are an EmuFlow integration extension"
        ),
        "numeric_policy": "nearest-integer-cost-rank-with-explicit-scale",
        "provenance": problem["provenance"],
        "input_sha256": hashlib.sha256(canonical_input).hexdigest(),
        "metrics": {
            **problem["metrics"],
            "stage1_cost_rank": stage1_cost,
            "stage2_cost_rank": stage2_cost,
            "stage1_physical_site_cost": stage1_physical_cost,
            "stage2_physical_site_cost": stage2_physical_cost,
            "certified_matchings": 1 + checked_alternatives,
            "certificate_disagreements": 0,
        },
        "direction_priority": priorities,
        "assignments": assignments,
    }
