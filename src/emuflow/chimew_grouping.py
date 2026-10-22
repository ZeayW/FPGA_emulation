"""Standalone, source-qualified reproduction of Chimew Algorithm 1.

This module deliberately stops at the paper's encoding-based initial grouping.
It does not silently substitute normalized placement regions for physical SLLs,
and its output is not a Phase 6 pin plan until the placement refinement and
two-stage bank/channel assignment gates are implemented.
"""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import subprocess
import tempfile
import zlib
from array import array
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .errors import EmuFlowError, ValidationError
from .native_tools import resolve_native_stage_command


CHIMEW_CROSSING_SCHEMA = "emuflow.chimew-crossing-encodings/v1"
CHIMEW_GROUPING_SCHEMA = "emuflow.chimew-signal-groups/v1"
CHIMEW_GROUPING_PROVIDER = "chimew-algorithm1-encoding-grouping-v1"
CHIMEW_CROSSING_PROVIDER = "source-qualified-physical-sll-routing-v1"
CHIMEW_ACADEMIC_CROSSING_PROVIDER = (
    "academic-virtual-region-routing-lookahead-v1"
)
CHIMEW_SCHEDULE_RATIO_PROVIDER = (
    "emuflow-lane-occupancy-ratio-materializer-v1"
)
CHIMEW_TIMING_GUARD_PROVIDER = "emuflow-chimew-critical-lane-guard-v1"

_TYPED_INPUT_MAGIC = b"EMCHGR3\0"
_TYPED_INPUT_END_MAGIC = b"EMCHGRE\0"
_TYPED_INPUT_VERSION = 1
_TYPED_INPUT_ENDIAN_MARKER = 0x01020304
_NATIVE_I32_MAX = (1 << 31) - 1


def _popcount(value: int) -> int:
    return bin(value).count("1")


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{label}: expected an integer >= {minimum}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label}: expected a non-empty string")
    return value


def _crossing_mask(values: Any, count: int, label: str) -> int:
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, int) for value in values
    ):
        raise ValidationError(f"{label}: expected an integer list")
    if len(values) != len(set(values)):
        raise ValidationError(f"{label}: duplicate SLL index")
    if any(value < 0 or value >= count for value in values):
        raise ValidationError(f"{label}: SLL index is out of range")
    mask = 0
    for value in values:
        mask |= 1 << value
    return mask


def validate_chimew_crossings(
    schedule: Mapping[str, Any], document: Mapping[str, Any]
) -> Dict[str, int]:
    """Validate exact physical-SLL encodings against a schedule."""

    if document.get("schema") != CHIMEW_CROSSING_SCHEMA:
        raise ValidationError("Chimew crossing encoding schema is invalid")
    provider = document.get("provider")
    if provider not in {
        CHIMEW_CROSSING_PROVIDER,
        CHIMEW_ACADEMIC_CROSSING_PROVIDER,
    }:
        raise ValidationError(
            "Chimew crossings require source-qualified physical SLL routing"
        )
    if provider == CHIMEW_ACADEMIC_CROSSING_PROVIDER and (
        document.get("qualification")
        != "academic-virtual-region-lookahead"
        or document.get("coordinate_system") != "normalized-placement-y"
    ):
        raise ValidationError(
            "academic Chimew crossings require an explicit virtual-region "
            "qualification"
        )
    if (
        document.get("design") != schedule.get("design")
        or document.get("platform") != schedule.get("platform")
    ):
        raise ValidationError("Chimew crossings do not match schedule identity")
    slls_per_fpga = _integer(
        document.get("slls_per_fpga"), "chimew.slls_per_fpga", minimum=1
    )
    if slls_per_fpga > 31:
        raise ValidationError("Chimew v1 supports at most 31 SLLs per FPGA")
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        raise ValidationError("Chimew crossing provenance is missing")
    _string(provenance.get("producer"), "chimew.provenance.producer")
    _string(provenance.get("producer_version"), "chimew.provenance.version")
    digest = _string(
        provenance.get("routing_sha256"), "chimew.provenance.routing_sha256"
    ).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValidationError("Chimew routing provenance SHA-256 is invalid")

    expected = {entry["id"] for entry in schedule.get("entries", [])}
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValidationError("Chimew crossing entries must be an array")
    encodings: Dict[str, int] = {}
    total_crossings = 0
    for index, record in enumerate(raw_entries):
        if not isinstance(record, dict):
            raise ValidationError(f"chimew.entries[{index}]: expected an object")
        entry_id = _string(
            record.get("schedule_entry"), f"chimew.entries[{index}].schedule_entry"
        )
        if entry_id in encodings:
            raise ValidationError(f"duplicate Chimew crossing for {entry_id!r}")
        source = _crossing_mask(
            record.get("source_slls"), slls_per_fpga, f"{entry_id}.source_slls"
        )
        sink = _crossing_mask(
            record.get("sink_slls"), slls_per_fpga, f"{entry_id}.sink_slls"
        )
        encoding = source | (sink << slls_per_fpga)
        if record.get("encoding") != encoding:
            raise ValidationError(
                f"Chimew crossing {entry_id!r} encoding is not independently derived"
            )
        encodings[entry_id] = encoding
        total_crossings += _popcount(encoding)
    if set(encodings) != expected:
        raise ValidationError("Chimew crossings must cover schedule entries exactly")
    metrics = document.get("metrics")
    if not isinstance(metrics, dict) or (
        metrics.get("signals") != len(encodings)
        or metrics.get("physical_sll_crossings") != total_crossings
    ):
        raise ValidationError("Chimew crossing metrics do not agree")
    return encodings


def _domain(entry: Mapping[str, Any]) -> Tuple[str, str, str]:
    return entry["link"], entry["from"], entry["to"]


def _tdm_ratio(entry: Mapping[str, Any]) -> int:
    """Return the serialization ratio; direct-lane schedules imply ratio one."""

    return _integer(
        entry.get("tdm_ratio", 1), f"{entry['id']}.tdm_ratio", minimum=1
    )


def _tdm_slot(entry: Mapping[str, Any], index: int) -> int:
    """Return a frozen Phase 5 slot, or a unique paper-kernel-only slot."""

    return _integer(entry.get("slot", index), f"{entry['id']}.slot")


def materialize_chimew_schedule_ratios(
    schedule: Mapping[str, Any],
) -> Dict[str, Any]:
    """Make implicit lane occupancy explicit for the Chimew adapter.

    This is an EmuFlow integration transform, not a Chimew paper kernel.  A
    baseline schedule already fixes a direction-qualified logical lane and a
    unique slot for every signal.  The number of signals occupying that lane
    is therefore the exact serialization capacity of its existing group.
    """

    raw_entries = schedule.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValidationError("Chimew ratio materialization requires schedule entries")
    explicit = ["tdm_ratio" in entry for entry in raw_entries]
    if any(explicit):
        if not all(explicit):
            raise ValidationError(
                "Chimew schedule mixes explicit and implicit TDM ratios"
            )
        raise ValidationError("Chimew schedule ratios are already explicit")

    entry_ids = set()
    groups: Dict[Tuple[str, str, str, int], list[str]] = defaultdict(list)
    occupancy = set()
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            raise ValidationError(f"schedule.entries[{index}] is invalid")
        entry_id = _string(entry.get("id"), f"schedule.entries[{index}].id")
        link = _string(entry.get("link"), f"{entry_id}.link")
        source = _string(entry.get("from"), f"{entry_id}.from")
        sink = _string(entry.get("to"), f"{entry_id}.to")
        lane = _integer(entry.get("lane"), f"{entry_id}.lane")
        slot = _integer(entry.get("slot"), f"{entry_id}.slot")
        if entry_id in entry_ids:
            raise ValidationError("Chimew ratio materialization found duplicate IDs")
        collision = (link, source, sink, lane, slot)
        if collision in occupancy:
            raise ValidationError(
                "Chimew ratio materialization found a lane/slot collision"
            )
        entry_ids.add(entry_id)
        occupancy.add(collision)
        groups[(link, source, sink, lane)].append(entry_id)

    result = copy.deepcopy(dict(schedule))
    ratio_by_id = {
        entry_id: len(member_ids)
        for member_ids in groups.values()
        for entry_id in member_ids
    }
    for entry in result["entries"]:
        entry["tdm_ratio"] = ratio_by_id[entry["id"]]
    source_sha256 = hashlib.sha256(
        json.dumps(
            schedule, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    result["chimew_ratio_materialization"] = {
        "provider": CHIMEW_SCHEDULE_RATIO_PROVIDER,
        "scope": "EmuFlow adapter, not a Chimew paper claim",
        "source_schedule_sha256": source_sha256,
        "direction_lane_groups": len(groups),
        "max_lane_occupancy": max(map(len, groups.values())),
    }
    return result


def _nearest_key(
    encoding: int, target: int, multiplicity: Mapping[int, int], index: int
) -> tuple[int, int, int, int, int, int]:
    category = 0 if encoding == target else 1 if encoding | target == target else 2
    return (
        category,
        _popcount(encoding ^ target) if category == 2 else 0,
        -_popcount(encoding),
        multiplicity[encoding],
        -encoding,
        index,
    )


class _GuardedLaneDomains(Sequence[Optional[Tuple[str, str, str, int]]]):
    """Lazy parallel guard column; no signal-sized dictionary is retained."""

    def __init__(
        self,
        entries: Sequence[Mapping[str, Any]],
        protected_lanes: set[Tuple[str, str, str, int]],
    ) -> None:
        self._entries = entries
        self._protected_lanes = protected_lanes

    def __len__(self) -> int:
        return len(self._entries)

    def __getitem__(
        self, index: int | slice
    ) -> Any:
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < -len(self._entries) or index >= len(self._entries):
            raise IndexError(index)
        if not self._protected_lanes:
            return None
        entry = self._entries[index]
        lane = (
            entry["link"],
            entry["from"],
            entry["to"],
            _integer(entry.get("lane"), f"{entry['id']}.lane"),
        )
        return lane if lane in self._protected_lanes else None


def _guard_at(
    guarded_lanes: Sequence[Optional[Tuple[str, str, str, int]]]
    | Mapping[str, Optional[Tuple[str, str, str, int]]],
    entries: Sequence[Mapping[str, Any]],
    index: int,
) -> Optional[Tuple[str, str, str, int]]:
    if isinstance(guarded_lanes, Mapping):
        return guarded_lanes.get(entries[index]["id"])
    return guarded_lanes[index]


def _guarded_lane_domains(
    entries: Sequence[Mapping[str, Any]], protected_entries: set[str]
) -> _GuardedLaneDomains:
    """Keep every member of a protected entry's frozen lane together.

    This is an EmuFlow timing extension around Chimew Algorithm 1.  Protecting
    the whole lane (rather than only the critical member) preserves the exact
    Phase 5 mux membership seen by the timing prepass.
    """

    unknown = set(protected_entries)
    for entry in entries:
        unknown.discard(entry["id"])
    if unknown:
        raise ValidationError(
            "Chimew timing guard references unknown schedule entries: "
            + ", ".join(sorted(unknown)[:10])
        )
    protected_lanes = {
        (
            entry["link"],
            entry["from"],
            entry["to"],
            _integer(entry.get("lane"), f"{entry['id']}.lane"),
        )
        for entry in entries
        if entry["id"] in protected_entries
    }
    return _GuardedLaneDomains(entries, protected_lanes)


def _schedule_timing_guard(schedule: Mapping[str, Any]) -> Optional[set[str]]:
    raw = schedule.get("chimew_timing_guard")
    if raw is None:
        return None
    if (
        not isinstance(raw, Mapping)
        or raw.get("provider") != CHIMEW_TIMING_GUARD_PROVIDER
        or raw.get("scope") != "EmuFlow extension, not a Chimew paper claim"
    ):
        raise ValidationError("Chimew schedule timing guard is invalid")
    entries = raw.get("protected_entries")
    if (
        not isinstance(entries, list)
        or any(not isinstance(entry, str) or not entry for entry in entries)
        or entries != sorted(set(entries))
    ):
        raise ValidationError("Chimew schedule timing guard entries are invalid")
    return set(entries)


def _oracle_group_array(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Optional[
        Sequence[Optional[Tuple[str, str, str, int]]]
        | Mapping[str, Optional[Tuple[str, str, str, int]]]
    ] = None,
) -> tuple[array, int, int]:
    # Independent replay remains linear in signal count by design.  Fixed-width
    # assignments and bucket ordinals remove dict/object amplification, but the
    # qualification oracle still has to visit and retain every live signal.
    guarded_lanes = guarded_lanes or ()
    buckets: Dict[
        tuple[
            Tuple[str, str, str],
            int,
            Optional[Tuple[str, str, str, int]],
        ],
        array,
    ] = defaultdict(lambda: array("q"))
    for index, entry in enumerate(entries):
        buckets[
            (
                _domain(entry),
                _tdm_ratio(entry),
                _guard_at(guarded_lanes, entries, index)
                if guarded_lanes
                else None,
            )
        ].append(index)
    assignment = array("q", [-1]) * len(entries)
    group_count = 0
    crossing_bits = 0
    guard_sentinel = ("", "", "", -1)
    for (_, ratio, _guard), indices in sorted(
        buckets.items(),
        key=lambda item: (
            item[0][0],
            item[0][2] is not None,
            item[0][2] or guard_sentinel,
            item[0][1],
        ),
    ):
        remaining_by_encoding: Dict[int, deque[int]] = defaultdict(deque)
        for index in indices:
            remaining_by_encoding[encodings[entries[index]["id"]]].append(index)
        multiplicity = Counter(
            {encoding: len(remaining) for encoding, remaining in remaining_by_encoding.items()}
        )
        remaining_count = len(indices)
        while remaining_count:
            target = min(
                (
                    encoding
                    for encoding, remaining in remaining_by_encoding.items()
                    if remaining
                ),
                key=lambda encoding: (
                    -_popcount(encoding),
                    -encoding,
                    remaining_by_encoding[encoding][0],
                ),
            )
            members = []
            used_slots: set[int] = set()
            while remaining_count and len(members) < ratio:
                compatible = {
                    encoding: next(
                        (
                            signal
                            for signal in remaining
                            if _tdm_slot(entries[signal], signal) not in used_slots
                        ),
                        None,
                    )
                    for encoding, remaining in remaining_by_encoding.items()
                    if remaining
                }
                compatible = {
                    encoding: signal
                    for encoding, signal in compatible.items()
                    if signal is not None
                }
                if not compatible:
                    break
                selected_encoding = min(
                    compatible,
                    key=lambda encoding: _nearest_key(
                        encoding,
                        target,
                        multiplicity,
                        compatible[encoding],
                    ),
                )
                selected = compatible[selected_encoding]
                remaining_by_encoding[selected_encoding].remove(selected)
                members.append(selected)
                used_slots.add(_tdm_slot(entries[selected], selected))
                target |= selected_encoding
                multiplicity[selected_encoding] -= 1
                remaining_count -= 1
            for index in members:
                assignment[index] = group_count
            crossing_bits += _popcount(target)
            group_count += 1
    return assignment, group_count, crossing_bits


def _oracle_groups(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Optional[
        Sequence[Optional[Tuple[str, str, str, int]]]
        | Mapping[str, Optional[Tuple[str, str, str, int]]]
    ] = None,
) -> tuple[Dict[str, int], int, int]:
    """Compatibility view for small reference tests.

    Production uses the fixed-width assignment returned by
    :func:`_oracle_group_array`; only explicit reference callers pay for the
    historical string-keyed dictionary.
    """

    assignment, group_count, crossing_bits = _oracle_group_array(
        entries, encodings, guarded_lanes
    )
    return (
        {entry["id"]: assignment[index] for index, entry in enumerate(entries)},
        group_count,
        crossing_bits,
    )


def _native_i32(value: int, label: str, *, minimum: int = 0) -> int:
    value = _integer(value, label, minimum=minimum)
    if value > _NATIVE_I32_MAX:
        raise ValidationError(f"{label}: exceeds the native 32-bit limit")
    return value


def _typed_signal_records(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Sequence[Optional[Tuple[str, str, str, int]]]
    | Mapping[str, Optional[Tuple[str, str, str, int]]],
    domain_rank: Optional[Mapping[Tuple[str, str, str], int]] = None,
):
    """Yield deterministic fixed-width records in schedule-entry order.

    The native kernel needs lexical domain order, not the domain strings.
    Guard lanes remain fixed-width fields and never expand the domain-rank map;
    a guard lane always belongs to the entry's own direction-qualified domain.
    """

    if domain_rank is None:
        domains = sorted({_domain(entry) for entry in entries})
        domain_rank = {value: index for index, value in enumerate(domains)}
    for index, entry in enumerate(entries):
        guard = _guard_at(guarded_lanes, entries, index)
        guard_lane = -1
        if guard is not None:
            if guard[:3] != _domain(entry):
                raise ValidationError("Chimew timing guard domain disagrees")
            guard_lane = _native_i32(guard[3], f"{entry['id']}.guard_lane")
        encoding = _integer(
            encodings[entry["id"]], f"{entry['id']}.encoding"
        )
        if encoding >= 1 << 64:
            raise ValidationError(
                f"{entry['id']}.encoding: exceeds the native 64-bit limit"
            )
        yield struct.pack(
            "<B4iQ",
            ord("S"),
            domain_rank[_domain(entry)],
            guard_lane,
            _native_i32(_tdm_ratio(entry), f"{entry['id']}.tdm_ratio", minimum=1),
            _native_i32(_tdm_slot(entry, index), f"{entry['id']}.slot"),
            encoding,
        )


def _serialize_typed(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Sequence[Optional[Tuple[str, str, str, int]]]
    | Mapping[str, Optional[Tuple[str, str, str, int]]],
    stream: Any,
    *,
    logical_sha256: Optional[str] = None,
) -> Mapping[str, Any]:
    """Stream a sealed fixed-width native input without scratch publication."""

    if not entries or len(entries) > _NATIVE_I32_MAX:
        raise ValidationError("Chimew typed input signal count is invalid")
    domains = sorted({_domain(entry) for entry in entries})
    domain_rank = {value: index for index, value in enumerate(domains)}
    if logical_sha256 is None:
        logical_sha256 = _typed_logical_sha256(
            entries, encodings, guarded_lanes, domain_rank=domain_rank
        )
    if (
        not isinstance(logical_sha256, str)
        or len(logical_sha256) != 64
        or any(character not in "0123456789abcdef" for character in logical_sha256)
    ):
        raise ValidationError("Chimew typed input logical digest is invalid")
    logical_digest = bytes.fromhex(logical_sha256)
    # CRC-32 covers the exact header and record bytes.  The two-pass SHA costs
    # more CPU than the historical one-pass text builder, but avoids its joined
    # graph-sized text and scratch publication.  The second-pass SHA is
    # computed from bytes actually handed to the pipe and must still equal the
    # first-pass digest before a trailer is emitted.  This makes a mutable
    # source mapping fail closed instead of publishing a mixed-generation
    # stream.
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
    for payload in _typed_signal_records(
        entries, encodings, guarded_lanes, domain_rank
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
        raise ValidationError("Chimew typed input record count disagrees")
    if emitted_digest.digest() != logical_digest:
        raise ValidationError("Chimew typed input changed while streaming")
    stream.write(
        struct.pack(
            "<8sQ32s", _TYPED_INPUT_END_MAGIC, checksum, logical_digest
        )
    )
    return {
        "records": observed,
        "logical_sha256": logical_digest.hex(),
        "stream_checksum": checksum,
    }


def _typed_logical_sha256(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Sequence[Optional[Tuple[str, str, str, int]]]
    | Mapping[str, Optional[Tuple[str, str, str, int]]],
    *,
    domain_rank: Optional[Mapping[Tuple[str, str, str], int]] = None,
) -> str:
    """Hash exactly the canonical record bytes accepted by the C++ parser."""

    logical = hashlib.sha256()
    batch = bytearray()
    for payload in _typed_signal_records(
        entries, encodings, guarded_lanes, domain_rank
    ):
        batch.extend(payload)
        if len(batch) >= 1 << 20:
            logical.update(batch)
            batch.clear()
    if batch:
        logical.update(batch)
    return logical.hexdigest()


def _serialize_legacy_text(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Sequence[Optional[Tuple[str, str, str, int]]]
    | Mapping[str, Optional[Tuple[str, str, str, int]]],
    path: Path,
) -> None:
    """Write the historical text protocol for explicit differential tests."""

    guard_sentinel = ("", "", "", -1)
    domains = sorted(
        {
            (_domain(entry), _guard_at(guarded_lanes, entries, index))
            for index, entry in enumerate(entries)
        },
        key=lambda item: (
            item[0], item[1] is not None, item[1] or guard_sentinel
        ),
    )
    domain_index = {domain: index for index, domain in enumerate(domains)}
    with path.open("w", encoding="utf-8") as stream:
        stream.write("EMUFLOW_CHIMEW_GROUPER_INPUT_V2\n")
        for index, entry in enumerate(entries):
            guard = _guard_at(guarded_lanes, entries, index)
            stream.write(
                f"SIGNAL {index} {domain_index[(_domain(entry), guard)]} "
                f"{_tdm_ratio(entry)} {_tdm_slot(entry, index)} "
                f"{encodings[entry['id']]}\n"
            )


def _parse_native_output(stream: Any, signal_count: int) -> tuple[array, int, int]:
    def read_fields(expected: bytes, size: int) -> list[bytes]:
        raw = stream.readline()
        fields = raw.split()
        if len(fields) != size or fields[:1] != [expected]:
            raise EmuFlowError("Chimew signal grouper output is malformed")
        return fields

    header = stream.readline().rstrip(b"\r\n")
    if header != b"EMUFLOW_CHIMEW_GROUPER_OUTPUT_V2":
        raise EmuFlowError("Chimew signal grouper output header is invalid")
    fields = read_fields(b"METRIC", 3)
    try:
        group_count, crossing_bits = int(fields[1]), int(fields[2])
    except ValueError as error:
        raise EmuFlowError("Chimew signal grouper metric is malformed") from error
    if not 0 < group_count <= signal_count or crossing_bits < 0:
        raise EmuFlowError("Chimew signal grouper metric is invalid")
    assignment = array("q", [-1]) * signal_count
    for expected_index in range(signal_count):
        fields = read_fields(b"ASSIGN", 3)
        try:
            index, group = int(fields[1]), int(fields[2])
        except ValueError as error:
            raise EmuFlowError(
                "Chimew signal grouper assignment is malformed"
            ) from error
        if index != expected_index or not 0 <= group < group_count:
            raise EmuFlowError(
                "Chimew grouper returned an invalid signal assignment"
            )
        assignment[index] = group
    if stream.read(1):
        raise EmuFlowError("Chimew signal grouper output has trailing data")
    return assignment, group_count, crossing_bits


def _raise_after_native_parse_error(
    process: subprocess.Popen[Any], stderr_stream: Any, grammar_error: EmuFlowError
) -> None:
    if process.stdout is not None:
        process.stdout.close()
    try:
        returncode = process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        returncode = process.wait()
    stderr_stream.flush()
    stderr_stream.seek(0)
    detail = stderr_stream.read().strip()
    if returncode != 0:
        raise EmuFlowError(
            "Chimew signal grouper failed "
            f"with exit status {returncode}"
            + (f": {detail}" if detail else "")
        ) from grammar_error
    raise grammar_error


def _run_native(
    entries: Sequence[Mapping[str, Any]],
    encodings: Mapping[str, int],
    guarded_lanes: Sequence[Optional[Tuple[str, str, str, int]]]
    | Mapping[str, Optional[Tuple[str, str, str, int]]],
    executable: Optional[str],
) -> tuple[array, int, int]:
    with tempfile.TemporaryDirectory(prefix="emuflow-chimew-grouping-") as temporary:
        root = Path(temporary)
        stderr_path = root / "stderr.txt"
        logical_sha256 = _typed_logical_sha256(
            entries, encodings, guarded_lanes
        )
        native_command = resolve_native_stage_command(
            "emuflow_chimew_signal_grouper",
            "phase6",
            "group-signals",
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
                raise EmuFlowError("Chimew signal grouper pipe is unavailable")
            try:
                _serialize_typed(
                    entries,
                    encodings,
                    guarded_lanes,
                    process.stdin,
                    logical_sha256=logical_sha256,
                )
                process.stdin.close()
                process.stdin = None
            except BrokenPipeError as error:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                process.stdin = None
                returncode = process.wait()
                process.stdout.close()
                stderr_stream.flush()
                stderr_stream.seek(0)
                detail = stderr_stream.read().strip()
                raise EmuFlowError(
                    "Chimew signal grouper rejected typed input "
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
                assignment, group_count, crossing_bits = _parse_native_output(
                    process.stdout, len(entries)
                )
            except EmuFlowError as error:
                _raise_after_native_parse_error(process, stderr_stream, error)
            process.stdout.close()
            returncode = process.wait()
            if returncode != 0:
                stderr_stream.flush()
                stderr_stream.seek(0)
                detail = stderr_stream.read().strip()
                raise EmuFlowError(
                    "Chimew signal grouper failed "
                    f"with exit status {returncode}"
                    + (f": {detail}" if detail else "")
                )
    return assignment, group_count, crossing_bits


def build_chimew_initial_groups(
    schedule: Mapping[str, Any],
    crossing_document: Mapping[str, Any],
    *,
    executable: Optional[str] = None,
    protected_entries: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Run paper Algorithm 1 and independently replay every group decision."""

    encodings = validate_chimew_crossings(schedule, crossing_document)
    entries = sorted(schedule["entries"], key=lambda entry: entry["id"])
    scheduled_guard = _schedule_timing_guard(schedule)
    if (
        protected_entries is not None
        and scheduled_guard is not None
        and protected_entries != scheduled_guard
    ):
        raise ValidationError("Chimew explicit and scheduled timing guards differ")
    effective_guard = (
        protected_entries
        if protected_entries is not None
        else scheduled_guard or set()
    )
    guarded_lanes = _guarded_lane_domains(entries, effective_guard)
    native = _run_native(entries, encodings, guarded_lanes, executable)
    oracle = _oracle_group_array(entries, encodings, guarded_lanes)
    if native != oracle:
        raise EmuFlowError("native Chimew grouping disagrees with Python replay")
    assignment, group_count, crossing_bits = native
    return {
        "schema": CHIMEW_GROUPING_SCHEMA,
        "status": "standalone_paper_kernel",
        "design": schedule["design"],
        "platform": schedule["platform"],
        "provider": CHIMEW_GROUPING_PROVIDER,
        "paper_scope": "FPGA-2026-Algorithm-1-initial-grouping",
        "integration_status": "not-a-phase6-pin-plan",
        "tie_break": "stable-encoding-count-then-schedule-entry-order",
        "integration_constraints": "frozen-phase5-slot-unique-within-group",
        "timing_guard": {
            "scope": "EmuFlow extension, not a Chimew paper claim",
            "protected_entries": sum(
                guard is not None for guard in guarded_lanes
            ),
            "protected_lane_groups": len(
                {guard for guard in guarded_lanes if guard is not None}
            ),
        },
        "metrics": {
            "signals": len(entries),
            "groups": group_count,
            "group_physical_sll_crossings": crossing_bits,
            "oracle_disagreements": 0,
        },
        "entries": [
            {
                "schedule_entry": entry["id"],
                "group": assignment[index],
                "encoding": encodings[entry["id"]],
                **(
                    {"timing_guard_lane": list(guarded_lanes[index])}
                    if guarded_lanes[index] is not None
                    else {}
                ),
            }
            for index, entry in enumerate(entries)
        ],
    }
