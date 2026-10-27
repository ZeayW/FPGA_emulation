"""Fixed-event timing binding for an external OpenSTA engine.

This is a timing abstraction, not a synthesizable transport implementation.
An event cutpoint has an absolute launch time, a chain of measured arcs, and
an explicit capture deadline. OpenSTA propagates the arcs and checks the
deadline. TX readiness checks MUST accompany terminal path observations:
observing the last TX alone cannot prove that it transported the current value.
No Python-computed arrival, slack, or TDM wait enters the exported circuit.
"""

from __future__ import annotations

import json
import math
import os
import re
from array import array
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .global_event_store import (
    EventChecks as _IndexedEventChecks, EventBuilder as _EventCheckBuilder,
    PathMetadata as _IndexedPathMetadata, MetadataBuilder as _PathMetadataBuilder,
    export_event_model, event_model_sources,
)
from .native_tools import resolve_native_executable
from .opensta import render_opensta_liberty, require_opensta_engine


@dataclass(frozen=True)
class EventCheck:
    path: str
    role: str
    event: str
    launch_ns: float
    arcs_ns: tuple[float, ...]
    required_ns: float


class _IndexedMeasurements(Sequence[Mapping[str, object]]):
    """Fixed-width OpenSTA scalar results over an existing check sequence.

    Check identity already belongs to ``rows``.  Retaining it again in one
    Python dictionary per measurement used substantially more memory than the
    three engine scalars, especially for saved-result validation where every
    original TX/commit/check row is read at once.  This view keeps exactly
    three doubles per row and materializes a mapping only for the row currently
    consumed by stitching or validation.
    """

    def __init__(
        self,
        rows: Sequence[EventCheck],
        arrivals: array,
        required: array,
        slacks: array,
    ):
        if (
            arrivals.typecode != "d"
            or required.typecode != "d"
            or slacks.typecode != "d"
            or len(arrivals) != len(rows)
            or len(required) != len(rows)
            or len(slacks) != len(rows)
        ):
            raise ValidationError("global STA indexed measurements are malformed")
        self._rows = rows
        self._arrivals = arrivals
        self._required = required
        self._slacks = slacks

    def __len__(self) -> int:
        return len(self._rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        row = self._rows[index]
        return {
            "path": row.path,
            "role": row.role,
            "event": row.event,
            "arrival_ns": self._arrivals[index],
            "required_ns": self._required[index],
            "slack_ns": self._slacks[index],
        }

    def __iter__(self):
        guard = self._rows._owner.borrow() if isinstance(self._rows, _IndexedEventChecks) else nullcontext()
        with guard:
            for index in range(len(self)):
                yield self[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))

    @property
    def typed_payload_bytes(self) -> int:
        return sum(
            values.itemsize * len(values)
            for values in (self._arrivals, self._required, self._slacks)
        )


_UINT32_MAX = (1 << 32) - 1
_UINT64_MASK = (1 << 64) - 1


def _event_check_values_hash(
    role: str,
    launch_ns: float,
    arcs_ns: tuple[float, ...],
    required_ns: float,
) -> int:
    """Return a compact candidate key; callers still compare exact values."""

    return hash((role, launch_ns, arcs_ns, required_ns)) & _UINT64_MASK


def _event_check_row_hash(row: EventCheck) -> int:
    return _event_check_values_hash(
        row.role, row.launch_ns, row.arcs_ns, row.required_ns
    )


class _RepresentativeBundle:
    __slots__ = ("start", "count", "numerical_key", "collision")

    def __init__(
        self,
        start: int,
        count: int,
        numerical_key: tuple | None,
        collision: _RepresentativeBundle | None = None,
    ):
        self.start = start
        self.count = count
        self.numerical_key = numerical_key
        self.collision = collision


class _IndexedCheckSelection(Sequence[EventCheck]):
    """A fixed-width row selection over another check sequence."""

    def __init__(self, rows: Sequence[EventCheck], indices: array):
        self._rows = rows
        self._indices = indices

    def __len__(self) -> int:
        return len(self._indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        selected = self._indices[index]
        if selected >= len(self._rows):
            raise ValidationError("global STA representative index is malformed")
        return self._rows[selected]

    def __iter__(self):
        for index in self._indices:
            if index >= len(self._rows):
                raise ValidationError("global STA representative index is malformed")
            yield self._rows[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))


def _validate_check_values(row: EventCheck) -> None:
    if not row.path or not row.event:
        raise ValidationError("global STA has duplicate/empty check identity")
    if row.role not in {"target", "runtime", "tx", "commit"}:
        raise ValidationError("global STA has an unsupported check role")
    for value in (row.launch_ns, row.required_ns, *row.arcs_ns):
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValidationError("global STA times must be finite numbers")
    if row.launch_ns < 0 or any(x < 0 for x in row.arcs_ns):
        raise ValidationError("global STA launch/arc delay must be nonnegative")


def validate_checks(checks: Iterable[EventCheck]) -> Sequence[EventCheck]:
    # Preserve the public API's historical snapshot contract for every
    # caller-owned container, including list, tuple, and custom Sequence
    # implementations. Only the two private immutable/indexed views may stay
    # zero-copy across validation and export.
    rows = (
        checks
        if isinstance(checks, (_IndexedEventChecks, _IndexedCheckSelection))
        else list(checks)
    )
    if not rows:
        raise ValidationError("global STA has no timing checks")
    if isinstance(rows, _IndexedEventChecks):
        rows.require_valid()
        return rows

    # bind_physical_checks emits one contiguous bundle per original path.
    # Validate that order while retaining only the live bundle's event
    # identities and observation pair.  This removes the check-sized identity
    # set and path-sized observation table from the production data plane.
    completed_paths = set()
    current_path = None
    current_identities = set()
    target = None
    runtime = None

    def finish_bundle() -> None:
        if current_path is None:
            return
        if target is None and runtime is None:
            raise ValidationError("global STA event has no original-path observation")
        if target is None or runtime is None:
            raise ValidationError("global STA target/runtime population disagrees")
        if (target.launch_ns, target.arcs_ns) != (
            runtime.launch_ns,
            runtime.arcs_ns,
        ):
            raise ValidationError("global STA target/runtime physical chains disagree")
        completed_paths.add(current_path)

    for row in rows:
        _validate_check_values(row)
        if row.path != current_path:
            finish_bundle()
            if row.path in completed_paths:
                raise ValidationError("global STA requires contiguous original-path bundles")
            current_path = row.path
            current_identities = set()
            target = None
            runtime = None
        identity = (row.role, row.event)
        if identity in current_identities:
            raise ValidationError("global STA has duplicate/empty check identity")
        current_identities.add(identity)
        if row.role == "target":
            if target is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            target = row
        elif row.role == "runtime":
            if runtime is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            runtime = row
    finish_bundle()
    return rows


def export_event_checks(checks: Iterable[EventCheck], directory: Path) -> Sequence[EventCheck]:
    """Export Verilog/Liberty/SDC and one batch query; names never contain user text.

Fixed-edge register cutpoints lower to SDC input launch times and output
deadlines relative to one epoch clock. Unlike periodic generated clocks this
cannot silently wrap a missed slot into the following frame. The measured arc
chain remains explicit; a scalar Liberty cell is shared for each unique delay.
"""
    rows = validate_checks(checks)
    if isinstance(rows, _IndexedEventChecks):
        export_event_model(rows, directory)
        return rows
    directory.mkdir(parents=True, exist_ok=True)
    delays = sorted({0.0, *(v for r in rows for v in r.arcs_ns)})
    cells = {v: f"D{i}" for i, v in enumerate(delays)}
    model = {"name": "emuflow_event_arcs", "cells": {
        name: {"kind": "combinational", "inputs": ["A"], "output": "Y",
               "delay_ns": value} for value, name in cells.items()}}
    (directory / "global_timing.lib").write_text(render_opensta_liberty(model))
    # Translate each independent check to its launch epoch. A global, very
    # long frame clock loses precision when subtracted from short deadlines.
    # Fixed I/O constraints allow negative output delays without edge wrap.
    epoch = 1.0
    with (directory / "global_timing.v").open("w") as v, (directory / "global_timing.sdc").open("w") as s:
        v.write("module global_timing(\n" + ",\n".join(
            f"i{i}, o{i}" for i in range(len(rows))) + ");\n")
        for i in range(len(rows)):
            v.write(f"input i{i};\noutput o{i};\n")
        s.write(f"create_clock -name epoch -period {epoch:.17g}\n")
        for i, row in enumerate(rows):
            chain = row.arcs_ns or (0.0,)
            prev = f"i{i}"
            for j, value in enumerate(chain):
                net = f"o{i}" if j == len(chain) - 1 else f"n{i}_{j}"
                if net != f"o{i}":
                    v.write(f"wire {net};\n")
                v.write(f"{cells[value]} a{i}_{j} (.A({prev}), .Y({net}));\n")
                prev = net
            s.write(f"set_input_delay -clock epoch -max 0 [get_ports i{i}]\n")
            s.write(f"set_input_transition 0 [get_ports i{i}]\n")
            s.write(f"set_output_delay -clock epoch -max {epoch-(row.required_ns-row.launch_ns):.17g} [get_ports o{i}]\n")
        v.write("endmodule\n")
    (directory / "analyze.tcl").write_text(f'''proc analyze {{}} {{
  puts "global STA: read model"
  read_liberty global_timing.lib
  read_verilog global_timing.v
  link_design global_timing
  puts "global STA: read constraints"
  # These generated constraints use only exact internal port names. OpenSTA's
  # get_ports scans every port per call; bind through the indexed cell API.
  # Stream the same portable SDC, without writing another constraint copy.
  set ::emuflow_top [sta::top_instance]
  set ::emuflow_cell [$::emuflow_top cell]
  set constraints [open global_timing.sdc r]
  while {{[gets $constraints line] >= 0}} {{
    uplevel #0 [string map [list {{[get_ports }} {{[$::emuflow_cell find_port }}] $line]
  }}
  close $constraints
  set out [open measurements.tsv w]
  puts $out "endpoint\\tarrival_ns\\trequired_ns\\tslack_ns"
  puts "global STA: propagate timing"
  find_timing
  puts "global STA: query endpoint scalars"
  puts "global STA: serialize checks"
  for {{set i 0}} {{$i < {len(rows)}}} {{incr i}} {{
    set pin [$::emuflow_top find_pin o$i]
    if {{$pin == "NULL"}} {{ error "missing global STA endpoint o$i" }}
    set p "NULL"
    foreach vertex [$pin vertices] {{
      if {{$vertex != "NULL"}} {{
        set candidate [sta::vertex_worst_slack_path $vertex max]
        if {{$candidate != "NULL"}} {{ set p $candidate; break }}
      }}
    }}
    if {{$p == "NULL"}} {{ error "unconstrained global STA endpoint o$i" }}
    # Path scalar APIs use seconds. Querying the known endpoint directly is
    # linear and avoids materializing/sorting every PathEnd in one giant list.
    set arrival [expr {{[$p arrival] * 1.0e9}}]
    # Path.required/slack are vertex-tag values and omit the output-delay
    # PathEnd constraint. endpoint_slack includes that constraint and returns
    # user time units.  All generated checks belong to the epoch path group;
    # an empty group name matches no PathEnd in OpenSTA 3.1.
    set slack [sta::endpoint_slack $pin "epoch" max]
    set required [expr {{$arrival + $slack}}]
    puts $out "o$i\\t$arrival\\t$required\\t$slack"
  }}
  close $out
}}
if {{[catch {{analyze}} message]}} {{ puts stderr $message; exit 2 }}
exit 0
''')
    return rows


def read_measurements(
    path: Path,
    rows: Sequence[EventCheck],
    *,
    verify_arcs=True,
) -> _IndexedMeasurements:
    if isinstance(rows, _IndexedEventChecks):
        from .global_measurements import import_measurements
        return import_measurements(path, rows, verify_arcs=verify_arcs)
    arrivals = array("d")
    required_values = array("d")
    slacks = array("d")
    guard = rows._owner.borrow() if isinstance(rows, _IndexedEventChecks) else nullcontext()
    with guard, path.open() as stream:
        if next(stream, "").strip() != "endpoint\tarrival_ns\trequired_ns\tslack_ns":
            raise ValidationError("invalid global STA measurements header")
        for index, line in enumerate(stream):
            fields = line.rstrip("\n").split("\t")
            if (
                index >= len(rows)
                or len(fields) != 4
                or fields[0] != f"o{index}"
            ):
                raise ValidationError("duplicate/malformed global STA endpoint")
            try:
                numbers = tuple(float(v) for v in fields[1:])
            except ValueError as exc:
                raise ValidationError("invalid global STA measurement") from exc
            if not all(math.isfinite(x) for x in numbers):
                raise ValidationError("nonfinite global STA measurement")
            row = rows[index]
            if verify_arcs:
                # Check TX/commit rows as well as original-path observations.
                # This is an independent linear arc check, performed while the
                # measurement row is live instead of retaining a second map.
                arrival = math.fsum(row.arcs_ns)
                required = row.required_ns - row.launch_ns
                for actual, expected in zip(
                    numbers,
                    (arrival, required, required - arrival),
                ):
                    if abs(actual - expected) > max(
                        1e-3, 4 * 2**-23 * abs(expected)
                    ):
                        raise ValidationError(
                            f"global STA event binding disagrees at o{index}"
                        )
            arrivals.append(numbers[0] + row.launch_ns)
            required_values.append(numbers[1] + row.launch_ns)
            slacks.append(numbers[2])
    if len(arrivals) != len(rows):
        raise ValidationError("global STA missing/extra/unconstrained endpoints")
    return _IndexedMeasurements(rows, arrivals, required_values, slacks)


def _compress_path_equivalent_checks(
        rows: Sequence[EventCheck]) -> tuple[Sequence[EventCheck], Sequence[int]]:
    """Deduplicate complete, numerically identical original-path bundles.

    Every exported check is an independent timing chain.  Two original paths
    therefore have identical OpenSTA results exactly when their ordered
    target/runtime/transport check bundles have the same role, launch, arcs
    and deadline.  Event and path identities are deliberately excluded from
    the numerical signature, but the whole bundle is the unit of compression:
    individual TX, commit or observation rows are never merged in isolation.
    """
    if isinstance(rows, _IndexedEventChecks):
        return rows.compressed()

    representative_indices = array("I")
    # A machine-word hash keeps one bounded key per distinct numerical bundle
    # instead of retaining every role/float/arc tuple a second time. Hash
    # matches are compared against the source rows, so hash collisions cannot
    # merge distinct path bundles.
    representative_bundles: dict[int, _RepresentativeBundle] = {}
    # This relation has one value per exported check. A Python ``list[int]``
    # costs at least one pointer per entry, while representative indices fit a
    # fixed-width unsigned value.  Reserve UINT32_MAX as the fail-closed
    # sentinel and keep the production relation at exactly four bytes/check.
    missing_representative = _UINT32_MAX
    if len(rows) >= missing_representative:
        raise ValidationError(
            "global STA check population exceeds indexed expansion"
        )
    original_to_representative = array(
        "I", (missing_representative for _ in rows)
    )

    def numerical_hash(index: int) -> int:
        return _event_check_row_hash(rows[index])

    def numerical_key(index: int) -> tuple:
        row = rows[index]
        return row.role, row.launch_ns, row.arcs_ns, row.required_ns

    def bundle_hash(indices: Sequence[int]) -> int:
        value = len(indices)
        for index in indices:
            value = ((value * 1_000_003) ^ numerical_hash(index)) & _UINT64_MASK
        return value

    def add_bundle(indices: Sequence[int]) -> None:
        signature = bundle_hash(indices)
        start = None
        candidate_key = None
        candidate = representative_bundles.get(signature)
        while candidate is not None:
            if candidate.count != len(indices):
                candidate = candidate.collision
                continue
            if candidate.numerical_key is None:
                representative_key = tuple(
                    numerical_key(
                        representative_indices[candidate.start + offset]
                    )
                    for offset in range(candidate.count)
                )
                candidate.numerical_key = representative_key
            else:
                representative_key = candidate.numerical_key
            if candidate_key is None:
                candidate_key = tuple(numerical_key(index) for index in indices)
            if candidate_key == representative_key:
                start = candidate.start
                break
            candidate = candidate.collision
        if start is None:
            start = len(representative_indices)
            if start + len(indices) >= _UINT32_MAX:
                raise ValidationError(
                    "global STA representative population exceeds indexed storage"
                )
            representative_indices.extend(indices)
            representative_bundles[signature] = _RepresentativeBundle(
                start,
                len(indices),
                candidate_key,
                representative_bundles.get(signature),
            )
        for offset, index in enumerate(indices):
            original_to_representative[index] = start + offset

    # Keep only the active original-path bundle; interleaved input is invalid.
    seen_paths = set()
    current_path = None
    current_indices: list[int] = []
    path_identities = ((index, row.path) for index, row in enumerate(rows))
    for index, path_identity in path_identities:
        if path_identity != current_path:
            if path_identity in seen_paths:
                raise ValidationError("global STA requires contiguous original-path bundles")
            if current_indices:
                add_bundle(current_indices)
            current_path = path_identity
            seen_paths.add(path_identity)
            current_indices = []
        current_indices.append(index)
    if current_indices:
        add_bundle(current_indices)
    if any(
        index == missing_representative
        for index in original_to_representative
    ):
        raise ValidationError("global STA path compression lost a timing check")
    return _IndexedCheckSelection(rows, representative_indices), original_to_representative


class _ExpandedMeasurementSequence(Sequence):
    """Lazy identity overlay for expanded representative measurements.

    OpenSTA evaluates only representative path bundles.  The persisted TSV
    still contains every original check, but the in-process consumer only
    needs an indexed view which combines the original identity with the
    representative scalar row.  Keeping that overlay lazy avoids allocating a
    second check-sized population of Python dictionaries before stitching.
    """

    def __init__(self, rows: Sequence[EventCheck], representative_values: Sequence,
                 original_to_representative: Sequence[int]):
        if len(original_to_representative) != len(rows):
            raise ValidationError("global STA expansion population disagrees")
        self._rows = rows
        self._representative_values = representative_values
        self._original_to_representative = original_to_representative

    def __len__(self):
        return len(self._rows)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(len(self)))]
        if index < 0:
            index += len(self)
        if index < 0 or index >= len(self):
            raise IndexError(index)
        representative_index = self._original_to_representative[index]
        if (
            isinstance(representative_index, bool)
            or not isinstance(representative_index, int)
            or representative_index < 0
            or representative_index >= len(self._representative_values)
        ):
            raise ValidationError(
                "global STA expansion references a missing representative"
            )
        row = self._rows[index]
        value = self._representative_values[representative_index]
        return {
            "path": row.path,
            "role": row.role,
            "event": row.event,
            "arrival_ns": value["arrival_ns"],
            "required_ns": value["required_ns"],
            "slack_ns": value["slack_ns"],
        }

    def __iter__(self):
        guard = self._rows._owner.borrow() if isinstance(self._rows, _IndexedEventChecks) else nullcontext()
        with guard:
            for index in range(len(self)):
                yield self[index]

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))


def _write_expanded_measurements(path: Path, rows: Sequence[EventCheck],
                                 representative_values: Sequence,
                                 original_to_representative: Sequence[int],
                                 representative_checks: Sequence[EventCheck]
                                 | None = None,
                                 ) -> Sequence[dict]:
    """Restore the full TSV and return a lazy original-identity overlay."""
    if (
        representative_checks is not None
        and len(representative_checks) != len(representative_values)
    ):
        raise ValidationError("global STA representative population disagrees")
    result = _ExpandedMeasurementSequence(
        rows, representative_values, original_to_representative
    )
    temporary = path.with_name(f".{path.name}.expanded.tmp")
    try:
        with temporary.open("w") as stream:
            stream.write("endpoint\tarrival_ns\trequired_ns\tslack_ns\n")
            for index, row in enumerate(rows):
                representative_index = original_to_representative[index]
                if representative_checks is not None:
                    if representative_index >= len(representative_checks):
                        raise ValidationError(
                            "global STA expansion references a missing representative"
                        )
                    representative = representative_checks[representative_index]
                    if (
                        row.role,
                        row.launch_ns,
                        row.arcs_ns,
                        row.required_ns,
                    ) != (
                        representative.role,
                        representative.launch_ns,
                        representative.arcs_ns,
                        representative.required_ns,
                    ):
                        raise ValidationError(
                            "global STA expansion mapping disagrees"
                        )
                value = result[index]
                local_arrival = value["arrival_ns"] - row.launch_ns
                local_required = value["required_ns"] - row.launch_ns
                stream.write(
                    f"o{index}\t{local_arrival:.17g}\t{local_required:.17g}\t"
                    f"{value['slack_ns']:.17g}\n"
                )
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return result


def run_event_checks(checks: Iterable[EventCheck], directory: Path,
                     executable: str | None = None, *, verify_arcs=True
                     ) -> Sequence[dict]:
    if not isinstance(checks, _IndexedEventChecks):
        raise ValidationError("global OpenSTA requires native event ownership; inline execution is retired")
    rows = validate_checks(checks)
    representatives, _ = rows.compressed()
    export_event_checks(representatives, directory)
    model_directory, model_sources = event_model_sources(directory)
    tool = resolve_native_executable("sta", executable)
    engine = require_opensta_engine(tool)
    with (directory / "opensta.log").open("w") as log:
        # Some packaged OpenSTA builds report ``GITDIR-NOT`` because their
        # source archive had no .git directory. Seal the exact executable in
        # the process log so engine identity remains independently replayable.
        log.write(
            "EmuFlow OpenSTA executable SHA256 "
            f"{engine['executable_sha256']}\n"
        )
        if "launcher_sha256" in engine:
            log.write(f"EmuFlow OpenSTA launcher SHA256 {engine['launcher_sha256']}\n")
        log.flush()
        from .store_publication import require_store_sources_unchanged
        from .tool_runtime import NativeToolProcess
        rows.require_valid()
        require_store_sources_unchanged(model_sources)
        runner = resolve_native_executable("emuflow-native")
        with NativeToolProcess(
            runner, [tool, "-exit", "analyze.tcl"], cwd=model_directory,
            environment=dict(os.environ), forward_stdout=True,
            scratch_parent=rows._owner.root,
        ) as process:
            for line in process.stdout:
                log.write(line.decode("utf-8", errors="replace"))
            result = process.finish()
        rows.require_valid()
        require_store_sources_unchanged(model_sources)
    engine_output = model_directory / "measurements.tsv"
    if result.returncode or not engine_output.is_file():
        raise ValidationError("global OpenSTA failed; see opensta.log")
    from .global_measurements import import_measurements
    expanded_values = import_measurements(
        engine_output, representatives, verify_arcs=verify_arcs, directory=directory
    )
    engine_output.unlink()
    with (directory / "opensta.log").open("a") as log:
        log.write(
            "EmuFlow exact path-bundle compression "
            f"original_checks={len(rows)} "
            f"representative_checks={len(representatives)}\n"
        )
    return expanded_values


def read_engine_identity(log_path: Path) -> dict:
    """Read the existing process banner, without another tool invocation."""
    executable_sha256 = None
    launcher_sha256 = None
    banner = None
    with log_path.open() as stream:
        for _ in range(32):
            line = stream.readline(4096)
            if not line:
                break
            seal = re.fullmatch(
                r"EmuFlow OpenSTA executable SHA256 ([0-9a-f]{64})\n?", line
            )
            if seal:
                executable_sha256 = seal[1]
                continue
            launcher_seal = re.fullmatch(
                r"EmuFlow OpenSTA launcher SHA256 ([0-9a-f]{64})\n?", line
            )
            if launcher_seal:
                launcher_sha256 = launcher_seal[1]
                continue
            match = re.match(r"OpenSTA\s+(\S+)\s+(\S+)", line)
            if match:
                banner = (match[1], match[2])
    if banner is not None:
        version, reported_revision = banner
        if re.fullmatch(r"[0-9a-f]{7,40}", reported_revision):
            result = {
                "name": "OpenSTA",
                "version": version,
                "revision": reported_revision,
            }
            if executable_sha256 is not None:
                result["executable_sha256"] = executable_sha256
            if launcher_sha256 is not None:
                result["launcher_sha256"] = launcher_sha256
            return result
        if executable_sha256 is not None:
            return {
                "name": "OpenSTA",
                "version": version,
                "revision": f"binary-sha256:{executable_sha256}",
                "reported_revision": reported_revision,
                "executable_sha256": executable_sha256,
                **({"launcher_sha256": launcher_sha256} if launcher_sha256 is not None else {}),
            }
    raise ValidationError("global OpenSTA log lacks engine version/revision")


def bind_physical_checks(runtime, routes, schedule, physical, platform, *, metadata=None,
                         routes_artifact_sha256=None):
    """Bind only current typed physical facts through the shared native owner."""
    from .global_binding_native import has_native_physical_inputs, bind_native_physical_checks
    if not has_native_physical_inputs(routes, schedule, physical):
        raise ValidationError("global STA requires current typed physical owners; inline binding is retired")
    return bind_native_physical_checks(
        runtime, routes, schedule, physical, platform, metadata=metadata,
        routes_artifact_sha256=routes_artifact_sha256,
    )


def build_opensta_timing(runtime, metadata, measurements):
    """Publish the native complete-original-path reduction of OpenSTA scalars."""
    from .global_measurements import NativeMeasurements
    if not isinstance(measurements, NativeMeasurements):
        raise ValidationError("global STA requires native measurement ownership; inline reduction is retired")
    return _build_native_opensta_timing(runtime, metadata, measurements)


def _build_native_opensta_timing(runtime, metadata, measurements):
    from .global_measurements import NativeTimingPaths
    measurements.require()
    paths = NativeTimingPaths(measurements, metadata["paths"])
    reduced = measurements.header['reduction']
    source = metadata['source_binding']
    count, bounds, failures = reduced['paths'], reduced['bounds_paths'], reduced['event_failures']
    scope = 'whole-original-design' if source is not None else 'cross-fpga-path-subset'
    status = 'fail' if failures or reduced['runtime']['wns_ns'] < 0 else 'pass'
    metrics = {role: {key: reduced[role][key] for key in ('wns_ns','original_path_tns_ns','negative_paths')}
               for role in ('target','runtime')}
    timing = {'schema':'emuflow.system-timing/v2','status':status,
              'qualification':'opensta-fixed-event-physical-bounds','timing_scope':scope,
              'source_binding':source,'paths':paths,
              'path_exactness': {'physical_model':'routed-staging-chain-upper-bounds',
                  'endpoint_exact_logic_paths':count-bounds,'cone_bound_logic_paths':bounds,
                  'fallback_logic_paths':0,'discontinuous_compressed_paths':0,
                  'physical_logic_segment_bounds':True,'fixed_slot_event_failures':failures},
              'summary': {'timing_paths':count,'original_paths':source['original_paths'] if source else None,
                  'original_local_paths':reduced['local_paths'],'original_cross_fpga_paths':count-reduced['local_paths'],
                  'compressed_representative_paths':metadata['compressed_representative_paths'],
                  'original_path_coverage':1.0 if source else None,
                  'original_path_ids_sha256':source['original_path_ids_sha256'] if source else None,
                  'maximum_system_delay_bound_ns':reduced['maximum_delay_ns']}}
    for role in ('target','runtime'):
        values=reduced[role];tns=values['original_path_tns_ns']
        timing[role+'_clock']={'closure_gate':role=='runtime','worst_path':values['worst_path'],
            'worst_slack_bound_ns':values['wns_ns'],'tns_bound_ns':tns,
            'total_negative_slack_bound_ns':tns,'negative_slack_paths':values['negative_paths']}
    periods={clock:float(record['nominal_period_ns']) for clock,record in runtime['virtual_dut_clocks'].items()}
    period=min(periods.values());maximum=reduced['maximum_delay_ns']
    timing['runtime_clock'].update(period_ns=period,frequency_mhz=1000.0/period,
        period_scope='fastest-domain-summary',clock_periods_ns=periods,
        minimum_safe_period_bound_ns=maximum,maximum_safe_frequency_bound_mhz=1000.0/maximum if maximum>0 else None)
    timing['global_opensta']={'schema':'emuflow.global-opensta-check/v1','authority':'opensta','execution':'standalone',
        'status':status,'timing_scope':scope,'original_paths':count,'checks':reduced['checks'],
        'event_failures':failures,'metrics':metrics,'tns_definition':'sum-negative-slack-once-per-original-TimingPathDB-path',
        'implementation_id':'native-global-measurements-v1',
        'measurement_logical_sha256':reduced['measurements_sha256']}
    return timing
