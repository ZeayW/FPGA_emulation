"""Seal the source-qualified Chimew kernels into one Phase 6 certificate.

The individual Chimew kernels deliberately remain independently testable.  This
module checks that a *single* schedule, placement, architecture, grouping, RUDY
gate, and bank/channel problem produced all of the artifacts passed to Phase 6.
It does not rerun an optimizer: validation is linear in the artifact sizes plus
the sparse RUDY bin intersections already represented by the report.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional

from .chimew_bank_channel import (
    CHIMEW_BANK_CHANNEL_PROVIDER,
    CHIMEW_BANK_CHANNEL_REPORT_SCHEMA,
    _candidate_cost,
    _raw_cost,
    validate_chimew_bank_channel_input,
)
from .chimew_grouping import (
    _domain,
    _popcount,
    _tdm_ratio,
    validate_chimew_crossings,
)
from .chimew_refinement import (
    CHIMEW_REFINED_GROUPING_SCHEMA,
    CHIMEW_REFINEMENT_PROVIDER,
    _pairwise_objective,
    _validate_initial_groups,
    validate_chimew_positions,
)
from .chimew_rudy import (
    CHIMEW_RUDY_PROVIDER,
    CHIMEW_RUDY_REPORT_SCHEMA,
    _oracle as _rudy_oracle,
    validate_chimew_rudy_input,
)
from .errors import ValidationError


CHIMEW_QUALIFICATION_SCHEMA = "emuflow.chimew-phase6-qualification/v1"
CHIMEW_QUALIFICATION_PROVIDER = (
    "chimew-paper-kernel-chain-plus-emuflow-provenance-v1"
)
CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA = "emuflow.chimew-phase6-qualification/v2"
CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER = (
    "chimew-paper-kernel-chain-plus-emuflow-byte-provenance-v2"
)
CHIMEW_BYTE_BOUND_SOURCE_SCOPE = "byte-bound-source-artifacts"
CHIMEW_BYTE_BOUND_SOURCE_LABELS = {
    "routing",
    "placement",
    "netlist",
    "architecture",
    "package_pins",
}
CHIMEW_BYTE_BOUND_OPTIONAL_SOURCE_LABELS = {"timing_paths"}


def canonical_sha256(document: Mapping[str, Any]) -> str:
    from .chimew_bank_channel_storage import (
        BankChannelInputView,
        canonical_bank_channel_input_sha256,
    )
    from .tdm_schedule_storage import (
        TDMScheduleView,
        canonical_tdm_schedule_sha256,
    )

    if isinstance(document, BankChannelInputView):
        return canonical_bank_channel_input_sha256(document)
    if isinstance(document, TDMScheduleView):
        return canonical_tdm_schedule_sha256(document)
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _close(lhs: Any, rhs: Any) -> bool:
    if isinstance(lhs, bool) or isinstance(rhs, bool):
        return lhs == rhs
    if isinstance(lhs, (int, float)) and isinstance(rhs, (int, float)):
        return math.isclose(float(lhs), float(rhs), rel_tol=1e-10, abs_tol=1e-9)
    return lhs == rhs


def _require_identity(
    schedule: Mapping[str, Any], label: str, document: Mapping[str, Any]
) -> None:
    if (
        document.get("design") != schedule.get("design")
        or document.get("platform") != schedule.get("platform")
    ):
        raise ValidationError(f"Chimew {label} identity does not match the schedule")


def _validate_source_binding(source_binding: Mapping[str, Any]) -> Dict[str, Any]:
    if (
        set(source_binding) != {"scope", "digests"}
        or source_binding.get("scope") != CHIMEW_BYTE_BOUND_SOURCE_SCOPE
    ):
        raise ValidationError("Chimew source binding scope is invalid")
    digests = source_binding.get("digests")
    if (
        not isinstance(digests, dict)
        or not CHIMEW_BYTE_BOUND_SOURCE_LABELS <= set(digests)
        or set(digests)
        - (
            CHIMEW_BYTE_BOUND_SOURCE_LABELS
            | CHIMEW_BYTE_BOUND_OPTIONAL_SOURCE_LABELS
        )
    ):
        raise ValidationError("Chimew source binding inventory is invalid")
    for label, digest in digests.items():
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(f"Chimew {label} source binding digest is invalid")
    return {"scope": CHIMEW_BYTE_BOUND_SOURCE_SCOPE, "digests": dict(digests)}


def _validate_refined_grouping(
    schedule: Mapping[str, Any],
    encodings: Mapping[str, int],
    positions: Mapping[str, float],
    initial: Mapping[str, int],
    refined: Mapping[str, Any],
) -> Dict[str, int]:
    if refined.get("schema") != CHIMEW_REFINED_GROUPING_SCHEMA:
        raise ValidationError("Chimew refined grouping schema is invalid")
    if refined.get("provider") != CHIMEW_REFINEMENT_PROVIDER:
        raise ValidationError("Chimew refined grouping provider is invalid")
    _require_identity(schedule, "refined grouping", refined)
    raw_entries = refined.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError("Chimew refined grouping entries are missing")
    assignment: Dict[str, int] = {}
    for index, record in enumerate(raw_entries):
        if not isinstance(record, dict):
            raise ValidationError(f"chimew.refined.entries[{index}] is invalid")
        entry_id = record.get("schedule_entry")
        group = record.get("group")
        if (
            not isinstance(entry_id, str)
            or not entry_id
            or isinstance(group, bool)
            or not isinstance(group, int)
            or group < 0
            or entry_id in assignment
        ):
            raise ValidationError("Chimew refined grouping entry is invalid")
        if record.get("encoding") != encodings.get(entry_id) or not _close(
            record.get("source_y"), positions.get(entry_id)
        ):
            raise ValidationError("Chimew refined grouping source data does not agree")
        assignment[entry_id] = group
    schedule_by_id = {entry["id"]: entry for entry in schedule.get("entries", [])}
    if set(assignment) != set(schedule_by_id):
        raise ValidationError("Chimew refined grouping must cover the schedule exactly")

    initial_masks: Dict[int, int] = defaultdict(int)
    refined_masks: Dict[int, int] = defaultdict(int)
    members: Dict[int, set[str]] = defaultdict(set)
    for entry_id, group in initial.items():
        initial_masks[group] |= encodings[entry_id]
    for entry_id, group in assignment.items():
        refined_masks[group] |= encodings[entry_id]
        members[group].add(entry_id)
    if dict(initial_masks) != dict(refined_masks):
        raise ValidationError("Chimew refinement changed a group SLL encoding")
    for group, entry_ids in members.items():
        domains = {_domain(schedule_by_id[entry_id]) for entry_id in entry_ids}
        ratios = {_tdm_ratio(schedule_by_id[entry_id]) for entry_id in entry_ids}
        if len(domains) != 1 or len(ratios) != 1:
            raise ValidationError(f"Chimew refined group {group} crosses a domain")
        ratio = next(iter(ratios))
        if isinstance(ratio, bool) or not isinstance(ratio, int) or len(entry_ids) > ratio:
            raise ValidationError(f"Chimew refined group {group} exceeds capacity")

    initial_members: Dict[int, set[str]] = defaultdict(set)
    for entry_id, group in initial.items():
        initial_members[group].add(entry_id)
    before = _pairwise_objective(initial_members, positions, set(initial_members))
    after = _pairwise_objective(members, positions, set(members))
    metrics = refined.get("metrics")
    expected = {
        "signals": len(assignment),
        "groups": len(members),
        "moved_signals": sum(initial[item] != group for item, group in assignment.items()),
        "pairwise_source_y_before": before,
        "pairwise_source_y_after": after,
        "group_physical_sll_crossings": sum(
            _popcount(value) for value in refined_masks.values()
        ),
        "oracle_disagreements": 0,
    }
    if not isinstance(metrics, dict) or any(
        not _close(metrics.get(field), value) for field, value in expected.items()
    ):
        raise ValidationError("Chimew refined grouping metrics do not agree")
    if after > before + 1e-9:
        raise ValidationError("Chimew refinement increased its position objective")
    return assignment


def _rudy_input_sha256(problem: Mapping[str, Any]) -> str:
    payload = {
        "origin": [problem["origin_x"], problem["origin_y"]],
        "bin": [problem["bin_width"], problem["bin_height"]],
        "shape": [problem["columns"], problem["rows"]],
        "wire_pitch": problem["wire_pitch"],
        "max_utilization": problem["max_utilization"],
        "provenance": problem["provenance"],
        "capacities": problem["capacities"],
        "nets": [{"id": net_id, "pins": pins} for net_id, pins in problem["nets"]],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_rudy_report(
    rudy_input: Mapping[str, Any], report: Mapping[str, Any]
) -> Dict[str, Any]:
    problem = validate_chimew_rudy_input(rudy_input)
    if (
        report.get("schema") != CHIMEW_RUDY_REPORT_SCHEMA
        or report.get("provider") != CHIMEW_RUDY_PROVIDER
        or report.get("design") != problem["design"]
        or report.get("platform") != problem["platform"]
        or report.get("provenance") != problem["provenance"]
        or report.get("input_sha256") != _rudy_input_sha256(problem)
    ):
        raise ValidationError("Chimew RUDY report provenance does not agree")
    loads, expected_metrics = _rudy_oracle(problem)
    bins = report.get("bins")
    if not isinstance(bins, list) or len(bins) != len(loads):
        raise ValidationError("Chimew RUDY report bins do not cover the grid")
    for index, (record, load, capacity) in enumerate(
        zip(bins, loads, problem["capacities"])
    ):
        if not isinstance(record, dict) or any(
            (
                not _close(record.get(field), value)
                if field in ("capacity", "load", "utilization")
                else record.get(field) != value
            )
            for field, value in {
                "column": index % problem["columns"],
                "row": index // problem["columns"],
                "capacity": capacity,
                "load": load,
                "utilization": load / capacity,
            }.items()
        ):
            raise ValidationError(f"Chimew RUDY bin {index} does not agree")
    metrics = report.get("metrics")
    if not isinstance(metrics, dict) or any(
        not _close(metrics.get(field), value)
        for field, value in {**expected_metrics, "oracle_disagreements": 0}.items()
    ):
        raise ValidationError("Chimew RUDY report metrics do not agree")
    expected_gate = "pass" if expected_metrics["overloaded_bins"] == 0 else "rejected"
    if report.get("gate_status") != expected_gate:
        raise ValidationError("Chimew RUDY gate status does not agree")
    return problem


def validate_chimew_bank_channel_report_artifact(
    bank_input: Mapping[str, Any], report: Mapping[str, Any]
) -> Dict[str, Any]:
    problem = validate_chimew_bank_channel_input(bank_input)
    if (
        report.get("schema") != CHIMEW_BANK_CHANNEL_REPORT_SCHEMA
        or report.get("provider") != CHIMEW_BANK_CHANNEL_PROVIDER
        or report.get("design") != problem["design"]
        or report.get("platform") != problem["platform"]
        or report.get("provenance") != problem["provenance"]
        or report.get("input_sha256") != canonical_sha256(bank_input)
    ):
        raise ValidationError("Chimew bank/channel report provenance does not agree")
    banks = {bank["id"]: bank for bank in problem["banks"]}
    channels = {
        channel["id"]: (bank["id"], bank["domain"], channel)
        for bank in banks.values()
        for channel in bank["channels"]
    }
    assignments = report.get("assignments")
    if not isinstance(assignments, list):
        raise ValidationError("Chimew bank/channel assignments are missing")
    assignment_by_group = {}
    for record in assignments:
        if not isinstance(record, dict):
            raise ValidationError("Chimew bank/channel assignment is invalid")
        group_id = record.get("group")
        if (
            not isinstance(group_id, str)
            or not group_id
            or group_id in assignment_by_group
        ):
            raise ValidationError("Chimew bank/channel assignment coverage is invalid")
        assignment_by_group[group_id] = record
    if len(assignment_by_group) != len(problem["groups"]):
        raise ValidationError("Chimew bank/channel assignment coverage is invalid")
    seen_channels = set()
    stage1_rank = stage2_rank = 0
    stage1_cost = stage2_cost = 0.0
    for group in problem["groups"]:
        group_id = group["id"]
        record = assignment_by_group.get(group_id)
        if record is None:
            raise ValidationError("Chimew bank/channel assignment coverage is invalid")
        bank_id = record.get("bank_pair")
        channel_id = record.get("channel")
        if (
            bank_id not in banks
            or channel_id not in channels
            or channel_id in seen_channels
        ):
            raise ValidationError("Chimew bank/channel assignment coverage is invalid")
        bank = banks[bank_id]
        channel_bank_id, channel_domain, channel = channels[channel_id]
        if (
            bank["id"] != channel_bank_id
            or bank["domain"] != channel_domain
            or bank["domain"] != group["domain"]
        ):
            raise ValidationError("Chimew bank/channel assignment crosses a domain")
        raw_bank = _raw_cost(group, bank["bank_a"]["point"], bank["bank_b"]["point"])
        raw_channel = _raw_cost(group, channel["pin_a"], channel["pin_b"])
        ranked_bank = _candidate_cost(
            group, bank["bank_a"]["point"], bank["bank_b"]["point"], problem["cost_scale"]
        )
        ranked_channel = _candidate_cost(
            group, channel["pin_a"], channel["pin_b"], problem["cost_scale"]
        )
        expected = {
            "bank_cost_rank": ranked_bank,
            "channel_cost_rank": ranked_channel,
            "bank_cost": raw_bank,
            "channel_cost": raw_channel,
        }
        if any(not _close(record.get(field), value) for field, value in expected.items()):
            raise ValidationError("Chimew bank/channel assignment cost does not agree")
        seen_channels.add(channel_id)
        stage1_rank += ranked_bank
        stage2_rank += ranked_channel
        stage1_cost += raw_bank
        stage2_cost += raw_channel
    metrics = report.get("metrics")
    expected_metrics = {
        **problem["metrics"],
        "stage1_cost_rank": stage1_rank,
        "stage2_cost_rank": stage2_rank,
        "stage1_physical_site_cost": stage1_cost,
        "stage2_physical_site_cost": stage2_cost,
        "certificate_disagreements": 0,
    }
    if not isinstance(metrics, dict) or any(
        not _close(metrics.get(field), value) for field, value in expected_metrics.items()
    ):
        raise ValidationError("Chimew bank/channel report metrics do not agree")
    return problem


def build_chimew_phase6_qualification(
    schedule: Mapping[str, Any],
    crossings: Mapping[str, Any],
    initial_grouping: Mapping[str, Any],
    positions_document: Mapping[str, Any],
    refined_grouping: Mapping[str, Any],
    rudy_input: Mapping[str, Any],
    rudy_report: Mapping[str, Any],
    bank_input: Mapping[str, Any],
    bank_report: Mapping[str, Any],
    *,
    source_binding: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a replayable certificate for one complete Chimew artifact chain."""

    encodings = validate_chimew_crossings(schedule, crossings)
    positions = validate_chimew_positions(schedule, positions_document)
    initial = _validate_initial_groups(schedule, initial_grouping, encodings)
    refined = _validate_refined_grouping(
        schedule, encodings, positions, initial, refined_grouping
    )
    rudy_problem = _validate_rudy_report(rudy_input, rudy_report)
    bank_problem = validate_chimew_bank_channel_report_artifact(
        bank_input, bank_report
    )
    for label, document in (
        ("RUDY input", rudy_input),
        ("RUDY report", rudy_report),
        ("bank/channel input", bank_input),
        ("bank/channel report", bank_report),
    ):
        _require_identity(schedule, label, document)
    if rudy_report.get("gate_status") != "pass":
        raise ValidationError("Chimew RUDY qualification gate did not pass")

    placement_sha = positions_document["provenance"]["placement_sha256"]
    architecture_sha = rudy_problem["provenance"]["architecture_sha256"]
    if (
        refined_grouping.get("placement_provenance")
        != positions_document.get("provenance")
        or rudy_problem["provenance"]["placement_sha256"] != placement_sha
        or bank_problem["provenance"]["placement_sha256"] != placement_sha
        or bank_problem["provenance"]["architecture_sha256"] != architecture_sha
        or bank_problem["provenance"]["grouping_sha256"]
        != canonical_sha256(refined_grouping)
    ):
        raise ValidationError("Chimew lookahead provenance chain is broken")

    refined_members: Dict[int, set[str]] = defaultdict(set)
    for entry_id, group in refined.items():
        refined_members[group].add(entry_id)
    bank_partitions = []
    for group in bank_problem["groups"]:
        members = {member["id"] for member in group["members"]}
        if any(
            not _close(member["fanout"][1], positions.get(member["id"]))
            for member in group["members"]
        ):
            raise ValidationError("Chimew bank input fanout y does not match lookahead placement")
        bank_partitions.append(members)
    if sorted(map(sorted, bank_partitions)) != sorted(
        map(sorted, refined_members.values())
    ):
        raise ValidationError("Chimew bank groups do not match refined signal groups")

    artifacts = {
        "schedule": canonical_sha256(schedule),
        "crossings": canonical_sha256(crossings),
        "initial_grouping": canonical_sha256(initial_grouping),
        "positions": canonical_sha256(positions_document),
        "refined_grouping": canonical_sha256(refined_grouping),
        "rudy_input": canonical_sha256(rudy_input),
        "rudy_report": canonical_sha256(rudy_report),
        "bank_channel_input": canonical_sha256(bank_input),
        "bank_channel_report": canonical_sha256(bank_report),
    }
    validated_source_binding = (
        _validate_source_binding(source_binding)
        if source_binding is not None
        else None
    )
    if validated_source_binding is not None:
        source_digests = validated_source_binding["digests"]
        if any(
            source_digests[label] != digest
            for label, digest in {
                "routing": crossings["provenance"]["routing_sha256"],
                "placement": placement_sha,
                "netlist": rudy_problem["provenance"]["netlist_sha256"],
                "architecture": architecture_sha,
            }.items()
        ):
            raise ValidationError("Chimew source binding provenance does not agree")

    certificate: Dict[str, Any] = {
        "schema": (
            CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA
            if validated_source_binding is not None
            else CHIMEW_QUALIFICATION_SCHEMA
        ),
        "status": "pass",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": (
            CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER
            if validated_source_binding is not None
            else CHIMEW_QUALIFICATION_PROVIDER
        ),
        "claim_boundary": (
            "paper kernels plus EmuFlow provenance/legality closure; vendor DRC, "
            "timing sign-off, bitstream, and hardware qualification remain external"
        ),
        "provenance": {
            "routing_sha256": crossings["provenance"]["routing_sha256"],
            "placement_sha256": placement_sha,
            "netlist_sha256": rudy_problem["provenance"]["netlist_sha256"],
            "architecture_sha256": architecture_sha,
        },
        "artifacts": artifacts,
        "metrics": {
            "signals": len(refined),
            "groups": len(refined_members),
            "rudy_peak_utilization": rudy_report["metrics"]["peak_utilization"],
            "rudy_overloaded_bins": rudy_report["metrics"]["overloaded_bins"],
            "certified_matchings": bank_report["metrics"]["certified_matchings"],
            "artifact_chain_disagreements": 0,
        },
    }
    if validated_source_binding is not None:
        certificate["source_binding"] = validated_source_binding
        certificate["source_binding_sha256"] = canonical_sha256(
            validated_source_binding
        )
    certificate["qualification_sha256"] = canonical_sha256(certificate)
    return certificate


def validate_chimew_phase6_qualification(
    certificate: Mapping[str, Any],
    schedule: Mapping[str, Any],
    crossings: Mapping[str, Any],
    initial_grouping: Mapping[str, Any],
    positions_document: Mapping[str, Any],
    refined_grouping: Mapping[str, Any],
    rudy_input: Mapping[str, Any],
    rudy_report: Mapping[str, Any],
    bank_input: Mapping[str, Any],
    bank_report: Mapping[str, Any],
    *,
    source_binding: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    expected = build_chimew_phase6_qualification(
        schedule,
        crossings,
        initial_grouping,
        positions_document,
        refined_grouping,
        rudy_input,
        rudy_report,
        bank_input,
        bank_report,
        source_binding=source_binding,
    )
    if dict(certificate) != expected:
        raise ValidationError("Chimew Phase 6 qualification certificate does not agree")
    return {
        "status": "pass",
        "signals": expected["metrics"]["signals"],
        "groups": expected["metrics"]["groups"],
        "qualification_scope": (
            CHIMEW_BYTE_BOUND_SOURCE_SCOPE
            if source_binding is not None
            else "declared-digest-artifact-chain"
        ),
        "qualification_sha256": expected["qualification_sha256"],
    }


def validate_chimew_qualification_seal(
    certificate: Mapping[str, Any], schedule: Mapping[str, Any]
) -> Dict[str, Any]:
    """Validate the certificate identity and self-seal without source artifacts."""
    identity = (certificate.get("schema"), certificate.get("provider"))
    if identity not in {
        (CHIMEW_QUALIFICATION_SCHEMA, CHIMEW_QUALIFICATION_PROVIDER),
        (
            CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA,
            CHIMEW_BYTE_BOUND_QUALIFICATION_PROVIDER,
        ),
    } or certificate.get("status") != "pass":
        raise ValidationError("Chimew Phase 6 qualification certificate is invalid")
    _require_identity(schedule, "qualification certificate", certificate)
    supplied_sha = certificate.get("qualification_sha256")
    unsigned = dict(certificate)
    unsigned.pop("qualification_sha256", None)
    if supplied_sha != canonical_sha256(unsigned):
        raise ValidationError("Chimew Phase 6 qualification self-seal is invalid")
    if identity[0] == CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA:
        source_binding = certificate.get("source_binding")
        if not isinstance(source_binding, dict):
            raise ValidationError("Chimew byte-bound source binding is missing")
        validated_source_binding = _validate_source_binding(source_binding)
        if certificate.get("source_binding_sha256") != canonical_sha256(
            validated_source_binding
        ):
            raise ValidationError("Chimew byte-bound source binding seal is invalid")
    elif "source_binding" in certificate or "source_binding_sha256" in certificate:
        raise ValidationError("legacy Chimew qualification contains a source binding")
    artifacts = certificate.get("artifacts")
    provenance = certificate.get("provenance")
    if (
        not isinstance(artifacts, dict)
        or not isinstance(provenance, dict)
        or artifacts.get("schedule") != canonical_sha256(schedule)
    ):
        raise ValidationError("Chimew Phase 6 qualification schedule binding is broken")
    if identity[0] == CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA and any(
        validated_source_binding["digests"][label] != provenance.get(field)
        for label, field in {
            "routing": "routing_sha256",
            "placement": "placement_sha256",
            "netlist": "netlist_sha256",
            "architecture": "architecture_sha256",
        }.items()
    ):
        raise ValidationError("Chimew byte-bound source provenance is inconsistent")
    metrics = certificate.get("metrics")
    if (
        not isinstance(metrics, dict)
        or metrics.get("artifact_chain_disagreements") != 0
        or metrics.get("rudy_overloaded_bins") != 0
    ):
        raise ValidationError("Chimew Phase 6 qualification gates did not pass")
    return {
        "status": "pass",
        "qualification_sha256": supplied_sha,
        "qualification_scope": (
            CHIMEW_BYTE_BOUND_SOURCE_SCOPE
            if identity[0] == CHIMEW_BYTE_BOUND_QUALIFICATION_SCHEMA
            else "declared-digest-artifact-chain"
        ),
        "signals": metrics.get("signals"),
        "groups": metrics.get("groups"),
    }


def validate_chimew_qualification_binding(
    certificate: Mapping[str, Any],
    schedule: Mapping[str, Any],
    bank_input: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate the self-sealed subset needed by the Phase 6 adapter."""

    validation = validate_chimew_qualification_seal(certificate, schedule)
    artifacts = certificate["artifacts"]
    provenance = certificate["provenance"]
    bank_provenance = bank_input.get("provenance")
    if (
        not isinstance(bank_provenance, dict)
        or artifacts.get("bank_channel_input") != canonical_sha256(bank_input)
        or artifacts.get("refined_grouping")
        != bank_provenance.get("grouping_sha256")
        or provenance.get("placement_sha256")
        != bank_provenance.get("placement_sha256")
        or provenance.get("architecture_sha256")
        != bank_provenance.get("architecture_sha256")
    ):
        raise ValidationError("Chimew Phase 6 qualification binding is broken")
    return validation
