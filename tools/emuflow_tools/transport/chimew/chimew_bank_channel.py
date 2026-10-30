"""Chimew bank input readers and bounded host for native typed assignment."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from emuflow.common.errors import EmuFlowError, ValidationError


CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1 = "emuflow.chimew-bank-channel-input/v1"
CHIMEW_BANK_CHANNEL_INPUT_SCHEMA = "emuflow.chimew-bank-channel-input/v2"
CHIMEW_BANK_CHANNEL_INPUT_PROVIDER = "source-qualified-physical-bank-channel-v1"
CHIMEW_BANK_CHANNEL_REPORT_SCHEMA = "emuflow.chimew-bank-channel-report/v1"
CHIMEW_BANK_CHANNEL_PROVIDER = "chimew-section3.4-two-stage-assignment-v1"
CHIMEW_BANK_CHANNEL_STATUS = "standalone_paper_kernel"
CHIMEW_BANK_CHANNEL_INTEGRATION_STATUS = "not-a-phase6-pin-plan"
CHIMEW_BANK_CHANNEL_PAPER_SCOPE = "FPGA-2026-Sections-3.4.1-through-3.4.3"
CHIMEW_BANK_CHANNEL_ALGORITHM2_INTERPRETATION = (
    "per-signal-fanout-distance-plus-mean-fanin-distance; optional "
    "source-bound timing weights are an EmuFlow integration extension"
)
CHIMEW_BANK_CHANNEL_NUMERIC_POLICY = (
    "nearest-integer-cost-rank-with-explicit-scale"
)

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


def _normalize_chimew_group(
    raw: Mapping[str, Any],
    index: int,
    *,
    schema: str,
    domain_by_id: Mapping[str, int],
) -> Dict[str, Any]:
    """Validate one group without retaining sibling groups.

    The indexed Phase 6 input store calls this helper while walking its ordered
    group relation.  Keeping the validator record-local prevents a second
    graph-sized normalized group tree from being built by production readers.
    """

    group_id = _string(raw.get("id"), f"chimew.assignment.groups[{index}].id")
    domain_id = _string(raw.get("domain"), f"{group_id}.domain")
    kind = raw.get("kind")
    direction = raw.get("direction")
    if domain_id not in domain_by_id:
        raise ValidationError(f"invalid Chimew group domain for {group_id!r}")
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
    members = []
    seen_member_ids = set()
    for member_index, raw_member in enumerate(raw_members):
        if not isinstance(raw_member, dict):
            raise ValidationError(f"{group_id}.members[{member_index}] is invalid")
        member_id = _string(raw_member.get("id"), f"{group_id}.member.id")
        raw_fanins = raw_member.get("fanins")
        if (
            member_id in seen_member_ids
            or not isinstance(raw_fanins, list)
            or not raw_fanins
        ):
            raise ValidationError(f"invalid Chimew signal member {member_id!r}")
        seen_member_ids.add(member_id)
        timing_weight = _number(
            raw_member.get("timing_weight", 1.0),
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
                "fanout": _point(
                    raw_member.get("fanout"), f"{group_id}.{member_id}.fanout"
                ),
                "fanins": fanins,
            }
        )
    normalized_direction = (
        0 if direction == "a_to_b" else 1 if direction == "b_to_a" else 2
    )
    if normalized_direction == 2 and {
        member["direction"] for member in members
    } != {0, 1}:
        raise ValidationError(
            "a bidirectional Chimew bundle must contain both directions"
        )
    return {
        "id": group_id,
        "domain": domain_by_id[domain_id],
        "kind": 0 if kind == "tdm_group" else 1,
        "direction": normalized_direction,
        "members": members,
    }


class _NormalizedDocumentGroups(Sequence[Mapping[str, Any]]):
    """Normalize one legacy JSON group at a time without a sibling tree."""

    def __init__(
        self,
        values: Sequence[Mapping[str, Any]],
        *,
        schema: str,
        domain_by_id: Mapping[str, int],
    ):
        self._values = values
        self._schema = schema
        self._domain_by_id = dict(domain_by_id)

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return list(self)[index]
        normalized_index = index if index >= 0 else len(self) + index
        return _normalize_chimew_group(
            self._values[index],
            normalized_index,
            schema=self._schema,
            domain_by_id=self._domain_by_id,
        )

    def __iter__(self):
        for index, raw in enumerate(self._values):
            yield _normalize_chimew_group(
                raw,
                index,
                schema=self._schema,
                domain_by_id=self._domain_by_id,
            )


def _normalize_chimew_bank(
    raw: Mapping[str, Any],
    index: int,
    *,
    domain_by_id: Mapping[str, int],
    first_channel_index: int,
) -> Tuple[Dict[str, Any], int]:
    """Validate one bank pair and assign stable global channel indices."""

    bank_id = _string(raw.get("id"), f"chimew.assignment.bank_pairs[{index}].id")
    domain_id = _string(raw.get("domain"), f"{bank_id}.domain")
    if domain_id not in domain_by_id:
        raise ValidationError(f"invalid Chimew bank pair {bank_id!r}")
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
    channel_index = first_channel_index
    seen_ids = set()
    for local_index, raw_channel in enumerate(raw_channels):
        if not isinstance(raw_channel, dict):
            raise ValidationError(f"{bank_id}.channels[{local_index}] is invalid")
        channel_id = _string(
            raw_channel.get("id"), f"{bank_id}.channels[{local_index}].id"
        )
        order = _integer(raw_channel.get("order"), f"{channel_id}.order")
        if channel_id in seen_ids or order in orders:
            raise ValidationError(f"duplicate Chimew channel/order in {bank_id!r}")
        seen_ids.add(channel_id)
        orders.add(order)
        channels.append(
            {
                "id": channel_id,
                "index": channel_index,
                "order": order,
                "pin_a": _point(raw_channel.get("pin_a"), f"{channel_id}.pin_a"),
                "pin_b": _point(raw_channel.get("pin_b"), f"{channel_id}.pin_b"),
            }
        )
        channel_index += 1
    if orders != set(range(len(channels))):
        raise ValidationError(f"Chimew channel order for {bank_id!r} is not contiguous")
    channels.sort(key=lambda channel: channel["order"])
    return (
        {
            "id": bank_id,
            "domain": domain_by_id[domain_id],
            "bank_a": normalized_bank_a,
            "bank_b": normalized_bank_b,
            "channels": channels,
        },
        channel_index,
    )


def _normalize_chimew_problem_header(
    document: Mapping[str, Any],
    *,
    metrics: Mapping[str, int],
) -> Dict[str, Any]:
    """Derive every bounded normalized fact from the sealed logical header.

    Indexed readers use this independently of the stored ``problem_header``.
    The latter is an acceleration/checking record, never an authority for
    design identity, cost scale, provenance, domains, or metrics.
    """

    schema = document.get("schema")
    if schema not in {
        CHIMEW_BANK_CHANNEL_INPUT_SCHEMA_V1,
        CHIMEW_BANK_CHANNEL_INPUT_SCHEMA,
    }:
        raise ValidationError("Chimew bank/channel input schema is invalid")
    if document.get("provider") != CHIMEW_BANK_CHANNEL_INPUT_PROVIDER:
        raise ValidationError("Chimew bank/channel input is not source-qualified")
    if document.get("coordinate_system") != "physical-site-xy":
        raise ValidationError(
            "Chimew bank/channel assignment rejects normalized coordinates"
        )
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("Chimew bank/channel provenance is missing")
    normalized_provenance = {
        "producer": _string(provenance.get("producer"), "chimew.assignment.producer"),
        "producer_version": _string(
            provenance.get("producer_version"),
            "chimew.assignment.producer_version",
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
    seen_domains = set()
    for index, raw in enumerate(raw_domains):
        if not isinstance(raw, dict):
            raise ValidationError(
                f"chimew.assignment.domains[{index}]: expected an object"
            )
        domain_id = _string(raw.get("id"), f"chimew.assignment.domains[{index}].id")
        fpga_a = _string(raw.get("fpga_a"), f"{domain_id}.fpga_a")
        fpga_b = _string(raw.get("fpga_b"), f"{domain_id}.fpga_b")
        if fpga_a == fpga_b or domain_id in seen_domains:
            raise ValidationError(f"invalid or duplicate Chimew domain {domain_id!r}")
        seen_domains.add(domain_id)
        domains.append({"id": domain_id, "fpga_a": fpga_a, "fpga_b": fpga_b})

    return {
        "design": _string(document.get("design"), "chimew.assignment.design"),
        "platform": _string(document.get("platform"), "chimew.assignment.platform"),
        "cost_scale": _integer(
            document.get("cost_quantization_per_site"),
            "chimew.assignment.cost_quantization_per_site",
            1,
        ),
        "provenance": normalized_provenance,
        "domains": domains,
        "metrics": dict(metrics),
    }


def validate_chimew_bank_channel_input(document: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate source-qualified physical groups, banks, channels, and sites."""

    from emuflow_tools.transport.chimew.chimew_bank_channel_storage import BankChannelInputView

    if isinstance(document, BankChannelInputView):
        return document.validated_problem()

    schema = document.get("schema")
    raw_metrics = document.get("metrics")
    if not isinstance(raw_metrics, dict):
        raise ValidationError("Chimew bank/channel input metrics do not agree")
    # Record-derived metrics are checked below.  This first pass derives all
    # other bounded semantics from the logical header itself.
    provisional_metrics = {
        field: raw_metrics.get(field)
        for field in (
            "groups",
            "signals",
            "fanins",
            "bank_pairs",
            "channels",
            "bidirectional_bundles",
        )
        if field in raw_metrics
    }
    bounded = _normalize_chimew_problem_header(
        document, metrics=provisional_metrics
    )
    domains = bounded["domains"]
    domain_by_id = {domain["id"]: index for index, domain in enumerate(domains)}

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
        bank, next_channel_count = _normalize_chimew_bank(
            raw,
            index,
            domain_by_id=domain_by_id,
            first_channel_index=channel_count,
        )
        bank_id = bank["id"]
        if bank_id in seen_bank_ids:
            raise ValidationError(f"invalid or duplicate Chimew bank pair {bank_id!r}")
        seen_bank_ids.add(bank_id)
        for channel in bank["channels"]:
            if channel["id"] in seen_channel_ids:
                raise ValidationError(f"duplicate Chimew channel {channel['id']!r}")
            seen_channel_ids.add(channel["id"])
        channel_count = next_channel_count
        banks.append(bank)

    raw_groups = document.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValidationError("Chimew signal groups are missing")
    seen_group_ids = set()
    signal_count = fanin_count = 0
    for index, raw in enumerate(raw_groups):
        if not isinstance(raw, dict):
            raise ValidationError(f"chimew.assignment.groups[{index}]: expected an object")
        group = _normalize_chimew_group(
            raw, index, schema=schema, domain_by_id=domain_by_id
        )
        group_id = group["id"]
        if group_id in seen_group_ids:
            raise ValidationError(f"invalid or duplicate Chimew group {group_id!r}")
        seen_group_ids.add(group_id)
        signal_count += len(group["members"])
        fanin_count += sum(len(member["fanins"]) for member in group["members"])
    groups = _NormalizedDocumentGroups(
        raw_groups, schema=schema, domain_by_id=domain_by_id
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
    metrics = raw_metrics
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
    bounded["metrics"] = expected_metrics
    return {
        **bounded,
        "banks": banks,
        "groups": groups,
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


def evaluate_chimew_bank_channel_assignment(
    document: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    output_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    """Assign and certify a leased typed input in the shared native owner."""
    import json
    import os
    import shutil
    import tempfile

    from emuflow.common.native_tools import native_file_generation, resolve_native_executable
    from emuflow.common.tool_runtime import NativeToolProcess
    from emuflow.storage.store_publication import bind_view_sources, require_store_sources_unchanged
    from emuflow_tools.transport.chimew.chimew_bank_channel_storage import BankChannelInputView, canonical_bank_channel_input_sha256
    from emuflow_tools.transport.chimew.chimew_bank_channel_report_storage import read_chimew_bank_channel_report

    if not isinstance(document, BankChannelInputView) or output_path is None:
        raise ValidationError("Chimew assignment requires a typed bank input and an output selector")
    sources = bind_view_sources((document, "chimew-bank-input-store-v1"))
    native = resolve_native_executable("emuflow-native")
    if executable is not None:
        try:
            same = Path(shutil.which(str(executable)) or executable).samefile(native)
        except OSError:
            same = False
        if not same:
            raise ValidationError("Chimew assignment requires the unified native executable")
    output = Path(output_path)
    if output.is_symlink():
        raise ValidationError("Chimew assignment output selector is a symlink")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".chimew-bank-host-", dir=output.parent) as temporary:
        root = Path(temporary)
        receipt = root / "operation.json"
        with NativeToolProcess(native, [native, "--report", str(receipt), "phase6",
                "assign-bank-store", str(document.manifest_path), str(output)],
                environment=dict(os.environ), scratch_parent=root) as process:
            completed = process.finish()
        if not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
            raise ValidationError("native Chimew assignment receipt is missing or exceeds bound")
        operation = json.loads(receipt.read_text())
        if completed.returncode:
            raise EmuFlowError("native Chimew assignment failed: " +
                operation.get("error", {}).get("message", completed.stdout))
        if (operation.get("schema") != "emuflow.native-stage-result/v1"
                or operation.get("stage") != "phase6.assign-bank-store"
                or operation.get("status") != "pass"
                or len(operation.get("inputs", [])) != 2
                or len(operation.get("outputs", [])) != 2):
            raise ValidationError("native Chimew assignment receipt identity disagrees")
        names = ("device", "inode", "bytes", "mtime_seconds", "mtime_nanoseconds",
                 "ctime_seconds", "ctime_nanoseconds")
        expected = {(str(source.path), source.schema): source.generation for source in sources}
        observed = {}
        for row in operation["inputs"]:
            key = (row.get("path"), row.get("schema"))
            generation = tuple(row["generation"][name] for name in names)
            if key in observed or key not in expected or generation != expected[key]:
                raise ValidationError("native Chimew assignment input owner disagrees")
            observed[key] = generation
        if observed != expected:
            raise ValidationError("native Chimew assignment input coverage disagrees")
        outputs = operation["outputs"]
        if (outputs[0].get("schema") != "chimew-bank-report-store-v1"
                or outputs[1].get("schema") != "chimew-bank-report-selector-v1"
                or outputs[1].get("path") != str(output)):
            raise ValidationError("native Chimew assignment output owner disagrees")
        for row in outputs:
            if native_file_generation(Path(row["path"])) != tuple(row["generation"][name] for name in names):
                raise ValidationError("native Chimew assignment output generation changed")
        require_store_sources_unchanged(sources)
        report = read_chimew_bank_channel_report(output)
        try:
            if (str(report.store_path) != outputs[0]["path"]
                    or report["input_sha256"] != canonical_bank_channel_input_sha256(document)):
                raise ValidationError("native Chimew assignment logical owner disagrees")
            for row in outputs:
                if native_file_generation(Path(row["path"])) != tuple(row["generation"][name] for name in names):
                    raise ValidationError("native Chimew assignment output generation changed")
            require_store_sources_unchanged(sources)
            return report
        except BaseException:
            report.close()
            raise
