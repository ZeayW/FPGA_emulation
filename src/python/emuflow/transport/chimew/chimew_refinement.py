"""Source-qualified, paper-bounded Chimew position refinement.

The paper specifies sorting equal-encoding signals by source y, swapping only
those signals, and never increasing group SLL crossings.  It does not publish
the complete swap schedule or tie breaks.  This module therefore labels its
deterministic anchor ordering and no-worse pairwise-y acceptance as a
first-party inference instead of claiming an exact unpublished reproduction.
"""

from __future__ import annotations

import hashlib
import math
import struct
import subprocess
import tempfile
import zlib
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from emuflow.transport.chimew.chimew_grouping import CHIMEW_GROUPING_PROVIDER, CHIMEW_GROUPING_SCHEMA, _domain, _integer, _popcount, _tdm_ratio, _tdm_slot, _string, validate_chimew_crossings
from emuflow.common.errors import EmuFlowError, ValidationError
from emuflow.common.native_tools import resolve_native_stage_command


CHIMEW_POSITION_SCHEMA = "emuflow.chimew-lookahead-positions/v1"
CHIMEW_POSITION_PROVIDER = "source-qualified-physical-site-lookahead-v1"
CHIMEW_REFINED_GROUPING_SCHEMA = "emuflow.chimew-refined-signal-groups/v1"
CHIMEW_REFINEMENT_PROVIDER = "chimew-section3.3.2-bounded-inference-v1"

_TYPED_INPUT_MAGIC = b"EMCHRF2\0"
_TYPED_INPUT_END_MAGIC = b"EMCHRFE\0"
_TYPED_INPUT_VERSION = 1
_TYPED_INPUT_ENDIAN_MARKER = 0x01020304
_NATIVE_I32_MAX = (1 << 31) - 1


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label}: expected a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{label}: expected a finite number")
    return result


def _digest(value: Any, label: str) -> str:
    digest = _string(value, label).lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValidationError(f"{label}: expected a SHA-256 digest")
    return digest


def validate_chimew_positions(
    schedule: Mapping[str, Any], document: Mapping[str, Any]
) -> Dict[str, float]:
    """Validate source logic-element y positions in physical site units."""

    if document.get("schema") != CHIMEW_POSITION_SCHEMA:
        raise ValidationError("Chimew lookahead position schema is invalid")
    if document.get("provider") != CHIMEW_POSITION_PROVIDER:
        raise ValidationError("Chimew refinement requires source-qualified placement")
    if document.get("coordinate_system") != "physical-site-y":
        raise ValidationError("Chimew refinement rejects normalized y coordinates")
    if (
        document.get("design") != schedule.get("design")
        or document.get("platform") != schedule.get("platform")
    ):
        raise ValidationError("Chimew positions do not match schedule identity")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("Chimew placement provenance is missing")
    _string(provenance.get("producer"), "chimew.positions.producer")
    _string(provenance.get("producer_version"), "chimew.positions.version")
    _digest(
        provenance.get("placement_sha256"), "chimew.positions.placement_sha256"
    )
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError("Chimew position entries must be an array")
    positions: Dict[str, float] = {}
    for index, record in enumerate(raw_entries):
        if not isinstance(record, dict):
            raise ValidationError(f"chimew.positions[{index}]: expected an object")
        entry_id = _string(
            record.get("schedule_entry"),
            f"chimew.positions[{index}].schedule_entry",
        )
        if entry_id in positions:
            raise ValidationError(f"duplicate Chimew position for {entry_id!r}")
        positions[entry_id] = _number(
            record.get("source_y"), f"chimew.positions[{entry_id}].source_y"
        )
    expected = {entry["id"] for entry in schedule.get("entries", [])}
    if set(positions) != expected:
        raise ValidationError("Chimew positions must cover schedule entries exactly")
    metrics = document.get("metrics")
    if not isinstance(metrics, dict) or metrics.get("signals") != len(positions):
        raise ValidationError("Chimew position metrics do not agree")
    return positions


def _validate_initial_groups(
    schedule: Mapping[str, Any],
    initial: Mapping[str, Any],
    encodings: Mapping[str, int],
    *, crossing_document: Optional[Mapping[str, Any]] = None,
) -> Dict[str, int]:
    if initial.get("schema") != CHIMEW_GROUPING_SCHEMA:
        raise ValidationError("Chimew initial grouping schema is invalid")
    if initial.get("provider") != CHIMEW_GROUPING_PROVIDER:
        raise ValidationError("position refinement requires Algorithm 1 groups")
    if (
        initial.get("design") != schedule.get("design")
        or initial.get("platform") != schedule.get("platform")
    ):
        raise ValidationError("Chimew initial groups do not match schedule identity")
    raw_entries = initial.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError("Chimew initial group entries must be an array")
    groups: Dict[str, int] = {}
    for index, record in enumerate(raw_entries):
        if not isinstance(record, dict):
            raise ValidationError(f"chimew.groups[{index}]: expected an object")
        entry_id = _string(
            record.get("schedule_entry"), f"chimew.groups[{index}].schedule_entry"
        )
        if entry_id in groups:
            raise ValidationError(f"duplicate Chimew group for {entry_id!r}")
        group = _integer(record.get("group"), f"chimew.groups[{entry_id}].group")
        if record.get("encoding") != encodings.get(entry_id):
            raise ValidationError("Chimew initial group encoding does not agree")
        groups[entry_id] = group
    entries = {entry["id"]: entry for entry in schedule.get("entries", [])}
    if set(groups) != set(entries):
        raise ValidationError("Chimew initial groups must cover schedule exactly")
    if crossing_document is not None:
        from emuflow.transport.chimew.chimew_grouping import _crossing_timing_guard
        protected = _crossing_timing_guard(schedule, crossing_document)
        if protected is not None:
            lane_key = lambda entry: (entry["link"],entry["from"],entry["to"],entry["lane"])
            guarded_lanes = {lane_key(entry) for entry in entries.values() if entry["id"] in protected}
            for record in raw_entries:
                lane = lane_key(entries[record["schedule_entry"]])
                expected = list(lane) if lane in guarded_lanes else None
                if record.get("timing_guard_lane") != expected:
                    raise ValidationError("Chimew initial groups do not preserve declared lookahead guards")
    grouped: Dict[int, list[str]] = defaultdict(list)
    for entry_id, group in groups.items():
        grouped[group].append(entry_id)
    crossing_bits = 0
    for group, members in grouped.items():
        domains = {_domain(entries[entry_id]) for entry_id in members}
        ratios = {_tdm_ratio(entries[entry_id]) for entry_id in members}
        if len(domains) != 1 or len(ratios) != 1:
            raise ValidationError(f"Chimew group {group} crosses a grouping domain")
        ratio = next(iter(ratios))
        if len(members) > ratio:
            raise ValidationError(f"Chimew group {group} exceeds its TDM ratio")
        explicit_slots = [
            _tdm_slot(entries[entry_id], 0)
            for entry_id in members
            if "slot" in entries[entry_id]
        ]
        if explicit_slots and (
            len(explicit_slots) != len(members)
            or len(set(explicit_slots)) != len(explicit_slots)
        ):
            raise ValidationError(
                f"Chimew group {group} reuses a frozen Phase 5 slot"
            )
        encoding = 0
        for entry_id in members:
            encoding |= encodings[entry_id]
        crossing_bits += _popcount(encoding)
    metrics = initial.get("metrics")
    if not isinstance(metrics, dict) or (
        metrics.get("signals") != len(entries)
        or metrics.get("groups") != len(grouped)
        or metrics.get("group_physical_sll_crossings") != crossing_bits
    ):
        raise ValidationError("Chimew initial group metrics do not agree")
    return groups


def _timing_guards(
    schedule: Mapping[str, Any], initial: Mapping[str, Any]
) -> Dict[str, Optional[tuple[Any, ...]]]:
    schedule_by_id = {entry["id"]: entry for entry in schedule.get("entries", [])}
    guards: Dict[str, Optional[tuple[Any, ...]]] = {}
    for record in initial.get("entries", []):
        raw = record.get("timing_guard_lane")
        if raw is not None and (
            not isinstance(raw, list)
            or len(raw) != 4
            or not all(isinstance(value, str) and value for value in raw[:3])
            or isinstance(raw[3], bool)
            or not isinstance(raw[3], int)
            or raw[3] < 0
        ):
            raise ValidationError("Chimew timing guard lane is invalid")
        entry_id = record["schedule_entry"]
        guard = tuple(raw) if raw is not None else None
        if guard is not None:
            entry = schedule_by_id[entry_id]
            expected = (
                entry["link"], entry["from"], entry["to"], entry.get("lane")
            )
            if guard != expected:
                raise ValidationError("Chimew timing guard lane disagrees with schedule")
        guards[entry_id] = guard
    guarded_lanes = {guard for guard in guards.values() if guard is not None}
    for entry_id, entry in schedule_by_id.items():
        lane = (entry["link"], entry["from"], entry["to"], entry.get("lane"))
        if (lane in guarded_lanes) != (guards.get(entry_id) is not None):
            raise ValidationError("Chimew timing guard does not cover a complete lane")
    return guards


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _pairwise_objective(
    members: Mapping[int, set[str]], positions: Mapping[str, float], groups: set[int]
) -> float:
    """Return the sum of absolute y differences within each group.

    For sorted values ``y[i]``, every value contributes ``i * y[i]`` as the
    larger endpoint and the prefix sum as the smaller endpoints.  This is
    mathematically identical to enumerating every pair, but avoids the
    quadratic replay cost for large TDM groups.
    """

    objective = 0.0
    for group in groups:
        values = sorted(
            positions[entry_id] for entry_id in members.get(group, set())
        )
        prefix = 0.0
        for index, value in enumerate(values):
            objective += value * index - prefix
            prefix += value
    return objective


def _oracle_refine(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    positions: Mapping[str, float],
    initial: Mapping[str, int],
    guards: Optional[Mapping[str, Optional[tuple[Any, ...]]]] = None,
) -> Tuple[Dict[str, int], int, int, float, float]:
    assignment = dict(initial)
    all_groups = set(assignment.values())
    members: Dict[int, set[str]] = defaultdict(set)
    for entry_id, group in assignment.items():
        members[group].add(entry_id)
    before_total = _pairwise_objective(members, positions, all_groups)
    guards = guards or {}
    buckets: Dict[
        Tuple[
            Tuple[str, str, str],
            Optional[tuple[Any, ...]],
            Optional[int],
            int,
            int,
        ],
        list[str],
    ] = defaultdict(list)
    for index, entry in enumerate(entries):
        buckets[
            (
                _domain(entry),
                guards.get(entry["id"]),
                (
                    _tdm_slot(entry, index)
                    if "slot" in entry
                    else None
                ),
                _tdm_ratio(entry),
                encodings[entry["id"]],
            )
        ].append(entry["id"])
    accepted = moved = 0
    guard_sentinel = ("", "", "", -1)
    # The native map iterates (domain_index, ratio, encoding).  domain_index is
    # assigned in (domain, guard, frozen_slot) order, so the independent replay
    # must use that complete order before it mutates group anchors.
    for (_, _guard, _frozen_slot, _, encoding), bucket in sorted(
        buckets.items(),
        key=lambda item: (
            item[0][0],
            item[0][1] is not None,
            item[0][1] or guard_sentinel,
            item[0][2] is not None,
            item[0][2] if item[0][2] is not None else -1,
            item[0][3],
            item[0][4],
        ),
    ):
        affected = {assignment[entry_id] for entry_id in bucket}
        if len(bucket) < 2 or len(affected) < 2:
            continue
        slots = {group: sum(assignment[item] == group for item in bucket) for group in affected}
        anchors = []
        for group in affected:
            values = [
                positions[item]
                for item in members[group]
                if encodings[item] != encoding
            ]
            if not values:
                values = [
                    positions[item]
                    for item in members[group]
                    if encodings[item] == encoding
                ]
            anchors.append((_median(values), group))
        ordered_groups = sorted(anchors)
        ordered_signals = sorted(bucket, key=lambda item: (positions[item], item))
        old_groups = [assignment[item] for item in ordered_signals]
        before = _pairwise_objective(members, positions, affected)
        position = 0
        for _, group in ordered_groups:
            for _ in range(slots[group]):
                assignment[ordered_signals[position]] = group
                position += 1
        for entry_id, group in zip(ordered_signals, old_groups):
            members[group].remove(entry_id)
        for entry_id in ordered_signals:
            members[assignment[entry_id]].add(entry_id)
        after = _pairwise_objective(members, positions, affected)
        if after > before + 1e-12:
            for entry_id, group in zip(ordered_signals, old_groups):
                members[assignment[entry_id]].remove(entry_id)
                assignment[entry_id] = group
                members[group].add(entry_id)
            continue
        changed = sum(
            assignment[entry_id] != group
            for entry_id, group in zip(ordered_signals, old_groups)
        )
        if changed:
            accepted += 1
            moved += changed
    after_total = _pairwise_objective(members, positions, all_groups)
    return assignment, accepted, moved, before_total, after_total


def _native_i32(value: Any, label: str, *, minimum: int = 0) -> int:
    value = _integer(value, label, minimum=minimum)
    if value > _NATIVE_I32_MAX:
        raise ValidationError(f"{label}: exceeds the native 32-bit limit")
    return value


def _refinement_domains(
    entries: Sequence[Mapping[str, Any]],
    guards: Mapping[str, Optional[tuple[Any, ...]]],
) -> Dict[tuple[Any, ...], int]:
    guard_sentinel = ("", "", "", -1)
    domains = sorted(
        {
            (
                _domain(entry),
                guards.get(entry["id"]),
                _tdm_slot(entry, index) if "slot" in entry else None,
            )
            for index, entry in enumerate(entries)
        },
        key=lambda item: (
            item[0],
            item[1] is not None,
            item[1] or guard_sentinel,
            item[2] is not None,
            item[2] if item[2] is not None else -1,
        ),
    )
    return {domain: index for index, domain in enumerate(domains)}


def _typed_refinement_records(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    positions: Mapping[str, float],
    initial: Mapping[str, int],
    guards: Mapping[str, Optional[tuple[Any, ...]]],
    domain_index: Mapping[tuple[Any, ...], int],
):
    for index, entry in enumerate(entries):
        entry_id = entry["id"]
        domain = (
            _domain(entry),
            guards.get(entry_id),
            _tdm_slot(entry, index) if "slot" in entry else None,
        )
        encoding = _integer(encodings[entry_id], f"{entry_id}.encoding")
        if encoding >= 1 << 64:
            raise ValidationError(
                f"{entry_id}.encoding: exceeds the native 64-bit limit"
            )
        source_y = _number(positions[entry_id], f"{entry_id}.source_y")
        yield struct.pack(
            "<B4iQd",
            ord("S"),
            index,
            domain_index[domain],
            _native_i32(_tdm_ratio(entry), f"{entry_id}.tdm_ratio", minimum=1),
            _native_i32(initial[entry_id], f"{entry_id}.group"),
            encoding,
            source_y,
        )


def _typed_refinement_sha256(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    positions: Mapping[str, float],
    initial: Mapping[str, int],
    guards: Mapping[str, Optional[tuple[Any, ...]]],
    domain_index: Optional[Mapping[tuple[Any, ...], int]] = None,
) -> str:
    if domain_index is None:
        domain_index = _refinement_domains(entries, guards)
    digest = hashlib.sha256()
    for payload in _typed_refinement_records(
        entries, encodings, positions, initial, guards, domain_index
    ):
        digest.update(payload)
    return digest.hexdigest()




def _serialize_typed_refinement(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    positions: Mapping[str, float],
    initial: Mapping[str, int],
    guards: Mapping[str, Optional[tuple[Any, ...]]],
    stream: Any,
    *,
    logical_sha256: Optional[str] = None,
) -> Mapping[str, Any]:
    """Stream refinement records without a signal-sized text buffer or file."""

    if not entries or len(entries) > _NATIVE_I32_MAX:
        raise ValidationError("Chimew refinement signal count is invalid")
    domain_index = _refinement_domains(entries, guards)
    if logical_sha256 is None:
        logical_sha256 = _typed_refinement_sha256(
            entries,
            encodings,
            positions,
            initial,
            guards,
            domain_index,
        )
    if (
        not isinstance(logical_sha256, str)
        or len(logical_sha256) != 64
        or any(character not in "0123456789abcdef" for character in logical_sha256)
    ):
        raise ValidationError("Chimew refinement logical digest is invalid")
    logical_digest = bytes.fromhex(logical_sha256)
    header = struct.pack(
        "<8sIIQ32s",
        _TYPED_INPUT_MAGIC,
        _TYPED_INPUT_VERSION,
        _TYPED_INPUT_ENDIAN_MARKER,
        len(entries),
        logical_digest,
    )
    stream.write(header)
    checksum = zlib.crc32(header)
    emitted_digest = hashlib.sha256()
    observed = 0
    batch = bytearray()
    for payload in _typed_refinement_records(
        entries, encodings, positions, initial, guards, domain_index
    ):
        batch.extend(payload)
        observed += 1
        if len(batch) >= 1 << 20:
            emitted_digest.update(batch)
            stream.write(batch)
            checksum = zlib.crc32(batch, checksum)
            batch.clear()
    if batch:
        emitted_digest.update(batch)
        stream.write(batch)
        checksum = zlib.crc32(batch, checksum)
    if observed != len(entries):
        raise ValidationError("Chimew refinement record count disagrees")
    if emitted_digest.digest() != logical_digest:
        raise ValidationError("Chimew refinement input changed while streaming")
    stream.write(
        struct.pack(
            "<8sQ32s", _TYPED_INPUT_END_MAGIC, checksum, logical_digest
        )
    )
    return {
        "records": observed,
        "logical_sha256": logical_sha256,
        "stream_checksum": checksum,
    }


def _parse_native_refinement_output(
    stream: Any, entries: Sequence[Mapping[str, Any]]
) -> Tuple[Dict[str, int], int, int, float, float]:
    if stream.readline().rstrip(b"\r\n") != b"EMUFLOW_CHIMEW_REFINER_OUTPUT_V1":
        raise EmuFlowError("Chimew position refiner output header is invalid")
    fields = stream.readline().split()
    if len(fields) != 5 or fields[:1] != [b"METRIC"]:
        raise EmuFlowError("Chimew position refiner metric is malformed")
    try:
        accepted, moved = int(fields[1]), int(fields[2])
        before, after = float(fields[3]), float(fields[4])
    except ValueError as error:
        raise EmuFlowError("Chimew position refiner metric is malformed") from error
    if (
        accepted < 0
        or moved < 0
        or not math.isfinite(before)
        or not math.isfinite(after)
    ):
        raise EmuFlowError("Chimew position refiner metric is invalid")
    assignment: Dict[str, int] = {}
    for expected_index, entry in enumerate(entries):
        fields = stream.readline().split()
        if len(fields) != 3 or fields[:1] != [b"ASSIGN"]:
            raise EmuFlowError("Chimew position refiner output is malformed")
        try:
            index, group = int(fields[1]), int(fields[2])
        except ValueError as error:
            raise EmuFlowError(
                "Chimew position refiner assignment is malformed"
            ) from error
        if index != expected_index or group < 0:
            raise EmuFlowError("Chimew refiner returned an invalid signal")
        assignment[entry["id"]] = group
    if stream.read(1):
        raise EmuFlowError("Chimew position refiner output has trailing data")
    return assignment, accepted, moved, before, after


def _run_native(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    positions: Mapping[str, float],
    initial: Mapping[str, int],
    guards: Mapping[str, Optional[tuple[Any, ...]]],
    executable: Optional[str],
) -> Tuple[Dict[str, int], int, int, float, float]:
    logical_sha256 = _typed_refinement_sha256(
        entries, encodings, positions, initial, guards
    )
    with tempfile.TemporaryDirectory(prefix="emuflow-chimew-refinement-") as temporary:
        root = Path(temporary)
        stderr_path = root / "stderr.txt"
        native_command = resolve_native_stage_command(
            "phase6",
            "refine-groups",
            explicit=executable,
        )
        with stderr_path.open("w+", encoding="utf-8") as stderr_stream:
            process = subprocess.Popen(
                [
                    *native_command,
                    "--typed-input",
                    "-",
                    "-",
                    logical_sha256,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_stream,
            )
            if process.stdin is None or process.stdout is None:
                raise EmuFlowError("Chimew position refiner pipe is unavailable")
            try:
                _serialize_typed_refinement(
                    entries,
                    encodings,
                    positions,
                    initial,
                    guards,
                    process.stdin,
                    logical_sha256=logical_sha256,
                )
                process.stdin.close()
                process.stdin = None
            except BrokenPipeError as error:
                process.stdin = None
                returncode = process.wait()
                process.stdout.close()
                stderr_stream.flush()
                stderr_stream.seek(0)
                detail = stderr_stream.read().strip()
                raise EmuFlowError(
                    "Chimew position refiner rejected typed input "
                    f"with exit status {returncode}"
                    + (f": {detail}" if detail else "")
                ) from error
            except BaseException:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                process.stdin = None
                process.stdout.close()
                if process.poll() is None:
                    process.kill()
                process.wait()
                raise
            try:
                result = _parse_native_refinement_output(process.stdout, entries)
            except EmuFlowError as grammar_error:
                process.stdout.close()
                returncode = process.wait()
                stderr_stream.flush()
                stderr_stream.seek(0)
                detail = stderr_stream.read().strip()
                if returncode != 0:
                    raise EmuFlowError(
                        "Chimew position refiner failed "
                        f"with exit status {returncode}"
                        + (f": {detail}" if detail else "")
                    ) from grammar_error
                raise
            process.stdout.close()
            returncode = process.wait()
            stderr_stream.flush()
            stderr_stream.seek(0)
            detail = stderr_stream.read().strip()
            if returncode != 0:
                raise EmuFlowError(
                    "Chimew position refiner failed "
                    f"with exit status {returncode}"
                    + (f": {detail}" if detail else "")
                )
    return result


def refine_chimew_groups(
    schedule: Mapping[str, Any],
    crossing_document: Mapping[str, Any],
    initial_grouping: Mapping[str, Any],
    position_document: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
) -> Dict[str, Any]:
    """Refine equal-encoding signals and verify the native decision trace."""

    encodings = validate_chimew_crossings(schedule, crossing_document)
    positions = validate_chimew_positions(schedule, position_document)
    initial = _validate_initial_groups(schedule, initial_grouping, encodings, crossing_document=crossing_document)
    guards = _timing_guards(schedule, initial_grouping)
    entries = sorted(schedule["entries"], key=lambda entry: entry["id"])
    native = _run_native(entries, encodings, positions, initial, guards, executable)
    oracle = _oracle_refine(entries, encodings, positions, initial, guards)
    if native[:3] != oracle[:3] or any(
        not math.isclose(native[index], oracle[index], rel_tol=1e-12, abs_tol=1e-9)
        for index in (3, 4)
    ):
        raise EmuFlowError("native Chimew refinement disagrees with Python replay")
    assignment, accepted, moved, before, after = native
    if after > before + 1e-9:
        raise EmuFlowError("Chimew refinement increased the position objective")
    initial_crossings: Dict[int, int] = defaultdict(int)
    refined_crossings: Dict[int, int] = defaultdict(int)
    for entry in entries:
        entry_id = entry["id"]
        initial_crossings[initial[entry_id]] |= encodings[entry_id]
        refined_crossings[assignment[entry_id]] |= encodings[entry_id]
    if initial_crossings != refined_crossings:
        raise EmuFlowError("Chimew refinement changed a group SLL encoding")
    refined_members: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in entries:
        refined_members[assignment[entry["id"]]].append(entry)
    for group, members in refined_members.items():
        explicit_slots = [entry["slot"] for entry in members if "slot" in entry]
        if explicit_slots and (
            len(explicit_slots) != len(members)
            or len(set(explicit_slots)) != len(explicit_slots)
        ):
            raise EmuFlowError(
                f"Chimew refinement reused a frozen Phase 5 slot in group {group}"
            )
    return {
        "schema": CHIMEW_REFINED_GROUPING_SCHEMA,
        "status": "standalone_paper_bounded_inference",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": CHIMEW_REFINEMENT_PROVIDER,
        "paper_scope": "FPGA-2026-Section-3.3.2-position-refinement",
        "claim_boundary": (
            "paper specifies equal-encoding source-y sorting/swaps and SLL "
            "non-increase; group-anchor ordering and pairwise-y acceptance "
            "are deterministic first-party inference"
        ),
        "integration_status": "not-a-phase6-pin-plan",
        "metrics": {
            "signals": len(entries),
            "groups": len(set(assignment.values())),
            "accepted_encoding_buckets": accepted,
            "moved_signals": moved,
            "pairwise_source_y_before": before,
            "pairwise_source_y_after": after,
            "group_physical_sll_crossings": sum(
                _popcount(value) for value in refined_crossings.values()
            ),
            "oracle_disagreements": 0,
        },
        "placement_provenance": dict(position_document["provenance"]),
        "entries": [
            {
                "schedule_entry": entry["id"],
                "group": assignment[entry["id"]],
                "encoding": encodings[entry["id"]],
                "source_y": positions[entry["id"]],
            }
            for entry in entries
        ],
    }
