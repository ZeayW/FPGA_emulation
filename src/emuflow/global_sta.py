"""Fixed-event timing binding for an external OpenSTA engine.

This is a timing abstraction, not a synthesizable transport implementation.
An event cutpoint has an absolute launch time, a chain of measured arcs, and
an explicit capture deadline. OpenSTA propagates the arcs and checks the
deadline. TX readiness checks MUST accompany terminal path observations:
observing the last TX alone cannot prove that it transported the current value.
No Python-computed arrival, slack, or TDM wait enters the exported circuit.
"""

from __future__ import annotations

import math
import re
import subprocess
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
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


def _validate_interleaved_checks(rows: list[EventCheck]) -> None:
    """Compatibility validator for callers that interleave path bundles."""
    seen = set()
    observations: dict[str, list[EventCheck | None]] = {}
    for row in rows:
        _validate_check_values(row)
        key = (row.path, row.role, row.event)
        if key in seen:
            raise ValidationError("global STA has duplicate/empty check identity")
        seen.add(key)
        if row.role in {"target", "runtime"}:
            pair = observations.setdefault(row.path, [None, None])
            position = 0 if row.role == "target" else 1
            if pair[position] is not None:
                raise ValidationError(
                    "global STA must observe each original path once"
                )
            pair[position] = row
    if not observations or any(
        target is None or runtime is None
        for target, runtime in observations.values()
    ):
        raise ValidationError("global STA target/runtime population disagrees")
    for row in rows:
        if row.path not in observations:
            raise ValidationError("global STA event has no original-path observation")
    for target, runtime in observations.values():
        assert target is not None and runtime is not None
        if (target.launch_ns, target.arcs_ns) != (
            runtime.launch_ns,
            runtime.arcs_ns,
        ):
            raise ValidationError("global STA target/runtime physical chains disagree")


def validate_checks(checks: Iterable[EventCheck]) -> list[EventCheck]:
    rows = list(checks)
    if not rows:
        raise ValidationError("global STA has no timing checks")

    # bind_physical_checks emits one contiguous bundle per original path.
    # Validate that order while retaining only the live bundle's event
    # identities and observation pair.  This removes the check-sized identity
    # set and path-sized observation table from the production data plane.
    # External callers that interleave bundles retain the original behavior
    # through the compatibility validator below.
    completed_paths = set()
    current_path = None
    current_identities = set()
    target = None
    runtime = None
    interleaved = False

    def finish_bundle() -> bool:
        if current_path is None:
            return True
        if target is None or runtime is None:
            return False
        if (target.launch_ns, target.arcs_ns) != (
            runtime.launch_ns,
            runtime.arcs_ns,
        ):
            raise ValidationError("global STA target/runtime physical chains disagree")
        completed_paths.add(current_path)
        return True

    for row in rows:
        _validate_check_values(row)
        if row.path != current_path:
            if not finish_bundle() or row.path in completed_paths:
                interleaved = True
                break
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
    if not interleaved and not finish_bundle():
        interleaved = True
    if interleaved:
        _validate_interleaved_checks(rows)
    return rows


def export_event_checks(checks: Iterable[EventCheck], directory: Path) -> list[EventCheck]:
    """Export Verilog/Liberty/SDC and one batch query; names never contain user text.

Fixed-edge register cutpoints lower to SDC input launch times and output
deadlines relative to one epoch clock. Unlike periodic generated clocks this
cannot silently wrap a missed slot into the following frame. The measured arc
chain remains explicit; a scalar Liberty cell is shared for each unique delay.
"""
    rows = validate_checks(checks)
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


def read_measurements(path: Path, rows: list[EventCheck], *, verify_arcs=True) -> list[dict]:
    result = []
    with path.open() as stream:
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
            result.append(
                {
                    "path": row.path,
                    "role": row.role,
                    "event": row.event,
                    "arrival_ns": numbers[0] + row.launch_ns,
                    "required_ns": numbers[1] + row.launch_ns,
                    "slack_ns": numbers[2],
                }
            )
    if len(result) != len(rows):
        raise ValidationError("global STA missing/extra/unconstrained endpoints")
    return result


def _compress_path_equivalent_checks(
        rows: list[EventCheck]) -> tuple[list[EventCheck], Sequence[int]]:
    """Deduplicate complete, numerically identical original-path bundles.

    Every exported check is an independent timing chain.  Two original paths
    therefore have identical OpenSTA results exactly when their ordered
    target/runtime/transport check bundles have the same role, launch, arcs
    and deadline.  Event and path identities are deliberately excluded from
    the numerical signature, but the whole bundle is the unit of compression:
    individual TX, commit or observation rows are never merged in isolation.
    """
    representatives: list[EventCheck] = []
    representative_bundles: dict[tuple, tuple[int, ...]] = {}
    # This relation has one value per exported check. A Python ``list[int]``
    # costs at least one pointer per entry, while representative indices fit a
    # fixed-width unsigned value.  Reserve UINT32_MAX as the fail-closed
    # sentinel and keep the production relation at exactly four bytes/check.
    missing_representative = (1 << 32) - 1
    if len(rows) >= missing_representative:
        raise ValidationError(
            "global STA check population exceeds indexed expansion"
        )
    original_to_representative = array(
        "I", (missing_representative for _ in rows)
    )

    def add_bundle(indices: list[int]) -> None:
        signature = tuple(
            (rows[index].role, rows[index].launch_ns,
             rows[index].arcs_ns, rows[index].required_ns)
            for index in indices
        )
        representative_indices = representative_bundles.get(signature)
        if representative_indices is None:
            start = len(representatives)
            representatives.extend(rows[index] for index in indices)
            representative_indices = tuple(range(start, len(representatives)))
            representative_bundles[signature] = representative_indices
        for index, representative_index in zip(indices, representative_indices):
            original_to_representative[index] = representative_index

    # bind_physical_checks emits one contiguous bundle per original path. Keep
    # only the active bundle in that production order instead of a dictionary
    # containing one index list for every path. Preserve the public helper's
    # historical acceptance of interleaved callers through a compatibility
    # fallback.
    seen_paths = set()
    current_path = None
    current_indices: list[int] = []
    interleaved = False
    for index, row in enumerate(rows):
        if row.path != current_path:
            if row.path in seen_paths:
                interleaved = True
                break
            if current_indices:
                add_bundle(current_indices)
            current_path = row.path
            seen_paths.add(row.path)
            current_indices = []
        current_indices.append(index)
    if interleaved:
        representatives.clear()
        representative_bundles.clear()
        original_to_representative = array(
            "I", (missing_representative for _ in rows)
        )
        path_indices: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            path_indices.setdefault(row.path, []).append(index)
        for indices in path_indices.values():
            add_bundle(indices)
    elif current_indices:
        add_bundle(current_indices)
    if any(
        index == missing_representative
        for index in original_to_representative
    ):
        raise ValidationError("global STA path compression lost a timing check")
    return representatives, original_to_representative


class _ExpandedMeasurementSequence(Sequence):
    """Lazy identity overlay for expanded representative measurements.

    OpenSTA evaluates only representative path bundles.  The persisted TSV
    still contains every original check, but the in-process consumer only
    needs an indexed view which combines the original identity with the
    representative scalar row.  Keeping that overlay lazy avoids allocating a
    second check-sized population of Python dictionaries before stitching.
    """

    def __init__(self, rows: list[EventCheck], representative_values: list[dict],
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

    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        return all(left == right for left, right in zip(self, other))


def _write_expanded_measurements(path: Path, rows: list[EventCheck],
                                 representative_values: list[dict],
                                 original_to_representative: Sequence[int]
                                 ) -> Sequence[dict]:
    """Restore the full TSV and return a lazy original-identity overlay."""
    result = _ExpandedMeasurementSequence(
        rows, representative_values, original_to_representative
    )
    temporary = path.with_name(f".{path.name}.expanded.tmp")
    try:
        with temporary.open("w") as stream:
            stream.write("endpoint\tarrival_ns\trequired_ns\tslack_ns\n")
            for index, row in enumerate(rows):
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
    rows = validate_checks(checks)
    representatives, original_to_representative = (
        _compress_path_equivalent_checks(rows)
    )
    export_event_checks(representatives, directory)
    tool = resolve_native_executable("sta", executable)
    engine = require_opensta_engine(tool)
    output = directory / "measurements.tsv"
    output.unlink(missing_ok=True)
    with (directory / "opensta.log").open("w") as log:
        # Some packaged OpenSTA builds report ``GITDIR-NOT`` because their
        # source archive had no .git directory. Seal the exact executable in
        # the process log so engine identity remains independently replayable.
        log.write(
            "EmuFlow OpenSTA executable SHA256 "
            f"{engine['executable_sha256']}\n"
        )
        log.flush()
        result = subprocess.run([tool, "-exit", "analyze.tcl"], cwd=directory,
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode or not output.is_file():
        raise ValidationError("global OpenSTA failed; see opensta.log")
    representative_values = read_measurements(
        output, representatives, verify_arcs=verify_arcs
    )
    expanded_values = _write_expanded_measurements(
        output, rows, representative_values, original_to_representative
    )
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
            return result
        if executable_sha256 is not None:
            return {
                "name": "OpenSTA",
                "version": version,
                "revision": f"binary-sha256:{executable_sha256}",
                "reported_revision": reported_revision,
                "executable_sha256": executable_sha256,
            }
    raise ValidationError("global OpenSTA log lacks engine version/revision")


def bind_physical_checks(runtime, routes, schedule, physical, platform, *, metadata=None,
                         routes_artifact_sha256=None):
    """Bind canonical measurements to event checks without composing delays.

Shared database readers validate identities only. This exporter does not call
the Python event propagator or consume its per-path numerical results.
"""
    from .system_timing import (
        _board_link_delay_database, _endpoint_delay_database,
        _logic_segment_database,
    )
    from .tdm import is_fixed_slot_transport_schedule, reconstruct_tdm_schedule_timing_paths

    if not is_fixed_slot_transport_schedule(schedule):
        raise ValidationError("global OpenSTA requires fixed-slot transport")
    endpoints = _endpoint_delay_database(physical)
    segments = _logic_segment_database(physical)
    links = _board_link_delay_database(physical, platform)
    if links is None:
        # Preserve the ordinary academic flow's declared BoardDB cycle latency,
        # explicitly model-only, never guessed or supposedly measured timing.
        from .board_link_timing import build_board_link_timing_model
        links = _board_link_delay_database(
            {"board_link_timing": build_board_link_timing_model(platform)}, platform)
    if endpoints is None or segments is None or links is None:
        raise ValidationError("global OpenSTA requires physical logic and endpoint measurements")
    # Timing reconstruction already binds each routed hop to its schedule
    # entry and carries the concrete TX time/ratio in ``scheduled_hops``.
    # Keep a compatibility lookup only for older injected reconstruction
    # records; production must not rebuild a second hop-sized entry map.
    legacy_entries = None

    def legacy_entry(identity):
        nonlocal legacy_entries
        if legacy_entries is None:
            legacy_entries = {e["id"]: e for e in schedule["entries"]}
        try:
            return legacy_entries[identity]
        except KeyError as exc:
            raise ValidationError(
                "global STA references a missing scheduled event"
            ) from exc
    board_links = {link.id: link for link in platform.links}
    demand_by_net = {r["net"]: r["id"] for r in routes["routes"]}
    uncertainty = float(physical.get("static_exact_clock_uncertainty_ns", 0.0))
    if uncertainty < 0 or not math.isfinite(uncertainty):
        raise ValidationError("invalid global STA uncertainty")
    from .runtime import runtime_clock_period_ns
    result = []
    path_metadata = {}
    representatives = 0
    for record in reconstruct_tdm_schedule_timing_paths(routes, platform, schedule):
        virtual = runtime_clock_period_ns(runtime, record.get("clock_domain"))
        representatives += 1
        by_demand = {}
        for hop in record["scheduled_hops"]:
            by_demand.setdefault(hop["demand"], []).append(hop)
        members = record.get("compressed_path_ids", [record["path"]])
        for member in members:
            chain = sorted(segments.get(record["path"], {}).get(member, []),
                           key=lambda s: s["cut_index"])
            if [s["cut_index"] for s in chain] != list(range(len(record["cut_nets"])+1)):
                raise ValidationError("global STA missing original-member logic segments")
            path_metadata[member] = {
                "representative_path": record["path"], "path_scope": "cross-fpga",
                "clock_domain": record.get("clock_domain"), "target_period_ns": record["clock_period_ns"],
                "physical_logic_segments_cone_bound": any(
                    s.get("measurement") == "cut-net-cone-upper-bound" for s in chain),
            }
            origin = 0.0
            trailing = ()
            ratio_mode = any(board_links[h["link"]].tdm_delay_model is not None
                             for h in record["scheduled_hops"])
            if ratio_mode and not all(board_links[h["link"]].tdm_delay_model is not None
                                      for h in record["scheduled_hops"]):
                raise ValidationError("one path cannot mix total net-ratio and fixed-slot delay models")
            model_arcs = []
            if ratio_mode:
                path_metadata[member]["interconnect_delay_model"] = "linear-ratio-v1"
            periods = set()
            seen_entries = set()
            for k, net in enumerate(record["cut_nets"]):
                hops = by_demand.get(demand_by_net[net], [])
                if not hops or chain[k]["replace_tx_endpoint"] != hops[0]["tx_endpoint"]:
                    raise ValidationError("global STA cut/segment binding disagrees")
                for j, hop in enumerate(hops):
                    eid = hop["schedule_entry"]
                    if eid in seen_entries:
                        raise ValidationError("global STA reused a scheduled event")
                    seen_entries.add(eid)
                    period = float(hop["tdm_slot_ns"])
                    periods.add(period)
                    if "fixed_slot_tx_time_ns" in hop:
                        tx = float(hop["fixed_slot_tx_time_ns"])
                        ratio = hop.get("tdm_ratio")
                    else:
                        entry = legacy_entry(eid)
                        for field in ("link", "from", "to"):
                            if entry[field] != hop[field]:
                                raise ValidationError(
                                    "global STA hop/event identity disagrees"
                                )
                        tx = entry["slot"] * period
                        ratio = entry.get("tdm_ratio")
                    if not math.isfinite(tx) or tx < 0.0:
                        raise ValidationError("global STA TX time is invalid")
                    arcs = trailing + (float(chain[k]["delay_ns"]) if not j else
                                       endpoints[hop["tx_endpoint"]],)
                    delay_model = board_links[hop["link"]].tdm_delay_model
                    link_delay = (delay_model.delay_ns(ratio)
                                  if delay_model is not None else
                                  links[hop["link"], hop["from"], hop["to"]])
                    if ratio_mode:
                        model_arcs.extend((arcs[-1], link_delay, endpoints[hop["rx_endpoint"]]))
                    # Previous-frame registered values are stable at first TX.
                    # Only that check is exempt; relays retain current-frame causality.
                    if not (schedule["transport_semantics"] == "registered-boundary" and not j):
                        result.append(EventCheck(member, "tx", eid, origin, arcs, tx-uncertainty))
                    origin = tx
                    trailing = (link_delay,
                                endpoints[hop["rx_endpoint"]])
            if len(periods) != 1 or len(seen_entries) != len(record["scheduled_hops"]):
                raise ValidationError("global STA incomplete event coverage or mixed slot periods")
            if chain[-1]["kind"] != "capture" or chain[-1]["replace_tx_endpoint"] is not None:
                raise ValidationError("global STA final segment is not capture")
            final_arcs = trailing + (float(chain[-1]["delay_ns"]), uncertainty)
            required = float(record.get("required_time_ns", record["clock_period_ns"]))
            commit = (schedule["route_constraints"]["frame_slots"]-1)*periods.pop()
            for role, deadline in (("target", required),
                                   ("runtime", virtual-(record["clock_period_ns"]-required)),
                                   ("commit", commit)):
                # The total net-ratio model already includes TDM delay.
                # Keep absolute TX/commit readiness checks separate; never
                # add their slot wait again to the modeled path delay.
                if ratio_mode and role in {"target", "runtime"}:
                    result.append(EventCheck(member, role, "capture", 0.0,
                        tuple(model_arcs) + (float(chain[-1]["delay_ns"]), uncertainty), deadline))
                else:
                    result.append(EventCheck(member, role, "capture", origin, final_arcs, deadline))
    source = None
    from .local_path_timing import validate_local_path_timing, path_id_set_sha256
    for fpga, database in physical.get("local_path_timing", {}).items():
        if metadata is not None:
            if validate_local_path_timing(database)["fpga"] != fpga:
                raise ValidationError("global STA local FPGA identity disagrees")
            if source is not None and source != database["source"]:
                raise ValidationError("global STA local source seals disagree")
            source = database["source"]
        for path in database["paths"]:
            if path["id"] in path_metadata:
                raise ValidationError("global STA local/crossing path overlap")
            path_metadata[path["id"]] = {
                "representative_path": path["id"], "path_scope": "same-fpga-local",
                "clock_domain": path.get("clock_domain"), "target_period_ns": path["clock_period_ns"],
                "physical_logic_segments_cone_bound": path.get("measurement") != "explicit-routed-path-chain",
            }
            required = float(path.get("required_time_ns", path["clock_period_ns"]))
            virtual = runtime_clock_period_ns(
                runtime, path.get("clock_domain")
            )
            for role, deadline in (("target", required),
                                   ("runtime", virtual-(path["clock_period_ns"]-required))):
                result.append(EventCheck(path["id"], role, "capture", 0.0,
                                         (float(path["delay_ns"]),), deadline))
    rows = validate_checks(result)
    if metadata is not None:
        if source is not None and (
            source["routes_sha256"] != routes_artifact_sha256
            or source["original_paths"] != len(path_metadata)
            or source["original_path_ids_sha256"] != path_id_set_sha256(list(path_metadata))
        ):
            raise ValidationError("global STA original population/source binding disagrees")
        metadata.update(paths=path_metadata, source_binding=source,
                        compressed_representative_paths=representatives)
    return rows


def build_opensta_timing(runtime, metadata, measurements):
    """Build the canonical report directly from engine scalars, never Python timing.

    Binding owns coverage and physical qualifications. OpenSTA owns numerical
    propagation. Python only aggregates the project's original-path metric.
    """
    metadata_paths = metadata["paths"]

    class InterleavedMeasurements(Exception):
        pass

    def ordered_paths():
        # bind_physical_checks emits one contiguous bundle per original path.
        # Validate identities within the live bundle and construct the final
        # path row when that bundle ends. This avoids an H-sized identity set
        # and a second P-sized target/runtime record map in production.
        paths = []
        completed = set()
        current_path = None
        current_rows = {}
        current_identities = set()
        failures = 0
        checks = 0

        def finish_bundle():
            if current_path is None:
                return
            if set(current_rows) != {"target", "runtime"}:
                raise InterleavedMeasurements
            info = metadata_paths[current_path]
            target = current_rows["target"]
            path = {
                "path": current_path,
                **info,
                "system_delay_bound_ns": target["arrival_ns"],
            }
            for role in ("target", "runtime"):
                row = current_rows[role]
                path[f"{role}_required_time_ns"] = row["required_ns"]
                path[f"{role}_clock_slack_bound_ns"] = row["slack_ns"]
            paths.append(path)
            completed.add(current_path)

        for row in measurements:
            checks += 1
            path_id = row.get("path")
            role = row.get("role")
            event = row.get("event")
            if (
                path_id not in metadata_paths
                or role not in {"target", "runtime", "tx", "commit"}
                or not isinstance(event, str)
                or not event
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            if path_id != current_path:
                if path_id in completed:
                    raise InterleavedMeasurements
                finish_bundle()
                current_path = path_id
                current_rows = {}
                current_identities = set()
            identity = (role, event)
            if identity in current_identities:
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            current_identities.add(identity)
            if any(
                isinstance(row.get(key), bool)
                or not isinstance(row.get(key), (int, float))
                or not math.isfinite(row[key])
                for key in ("arrival_ns", "required_ns", "slack_ns")
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            if role in {"target", "runtime"}:
                if role in current_rows:
                    raise ValidationError(
                        "global STA invalid standalone measurement population"
                    )
                current_rows[role] = row
            elif row["slack_ns"] < -1e-3:
                failures += 1
        finish_bundle()
        if not paths or len(paths) != len(metadata_paths):
            raise ValidationError(
                "global STA invalid standalone measurement population"
            )
        paths.sort(key=lambda path: path["path"])
        return paths, failures, checks

    try:
        paths, failures, measurement_count = ordered_paths()
    except InterleavedMeasurements:
        # Preserve compatibility for external callers that do not retain the
        # canonical path-bundle order. This is outside the production path.
        observed = {}
        identities = set()
        failures = 0
        for row in measurements:
            identity = (row.get("path"), row.get("role"), row.get("event"))
            if (
                row.get("path") not in metadata_paths
                or row.get("role") not in {"target", "runtime", "tx", "commit"}
                or identity in identities
                or any(
                    isinstance(row.get(key), bool)
                    or not isinstance(row.get(key), (int, float))
                    or not math.isfinite(row[key])
                    for key in ("arrival_ns", "required_ns", "slack_ns")
                )
            ):
                raise ValidationError(
                    "global STA invalid standalone measurement population"
                )
            identities.add(identity)
            if row["role"] in {"target", "runtime"}:
                key = (row["path"], row["role"])
                if key in observed:
                    raise ValidationError(
                        "global STA invalid standalone measurement population"
                    )
                observed[key] = row
            elif row["slack_ns"] < -1e-3:
                failures += 1
        expected = {
            (path_id, role)
            for path_id in metadata_paths
            for role in ("target", "runtime")
        }
        if set(observed) != expected:
            raise ValidationError(
                "global STA invalid standalone measurement population"
            )
        paths = []
        for path_id, info in sorted(metadata_paths.items()):
            target = observed[path_id, "target"]
            path = {
                "path": path_id,
                **info,
                "system_delay_bound_ns": target["arrival_ns"],
            }
            for role in ("target", "runtime"):
                row = observed[path_id, role]
                path[f"{role}_required_time_ns"] = row["required_ns"]
                path[f"{role}_clock_slack_bound_ns"] = row["slack_ns"]
            paths.append(path)
        measurement_count = len(measurements)
    if not paths:
        raise ValidationError("global STA empty standalone result")
    source = metadata["source_binding"]
    scope = "whole-original-design" if source is not None else "cross-fpga-path-subset"
    local = sum(p["path_scope"] == "same-fpga-local" for p in paths)
    bounds = sum(p["physical_logic_segments_cone_bound"] for p in paths)
    timing = {"schema": "emuflow.system-timing/v2", "status": "fail" if failures else "pass",
              "qualification": "opensta-fixed-event-physical-bounds", "timing_scope": scope,
              "source_binding": source, "paths": paths,
              "path_exactness": {"physical_model": "routed-staging-chain-upper-bounds",
                  "endpoint_exact_logic_paths": len(paths)-bounds, "cone_bound_logic_paths": bounds,
                  "fallback_logic_paths": 0, "discontinuous_compressed_paths": 0,
                  "physical_logic_segment_bounds": True, "fixed_slot_event_failures": failures},
              "summary": {"timing_paths": len(paths), "original_paths": source["original_paths"] if source else None,
                  "original_local_paths": local, "original_cross_fpga_paths": len(paths)-local,
                  "compressed_representative_paths": metadata["compressed_representative_paths"],
                  "original_path_coverage": 1.0 if source else None,
                  "original_path_ids_sha256": source["original_path_ids_sha256"] if source else None,
                  "maximum_system_delay_bound_ns": max(p["system_delay_bound_ns"] for p in paths)}}
    metrics = {}
    for role in ("target", "runtime"):
        worst = min(paths, key=lambda p:(p[f"{role}_clock_slack_bound_ns"],p["path"]))
        slacks = [p[f"{role}_clock_slack_bound_ns"] for p in paths]
        tns = math.fsum(min(0.0,s) for s in slacks)
        metrics[role] = {"wns_ns": min(slacks), "original_path_tns_ns": tns,
                         "negative_paths": sum(s<0 for s in slacks)}
        timing[f"{role}_clock"] = {"closure_gate": role=="runtime", "worst_path": worst["path"],
            "worst_slack_bound_ns": min(slacks), "tns_bound_ns": tns,
            "total_negative_slack_bound_ns": tns, "negative_slack_paths": metrics[role]["negative_paths"]}
    clock_periods = {
        clock: float(record["nominal_period_ns"])
        for clock, record in runtime["virtual_dut_clocks"].items()
    }
    period = min(clock_periods.values())
    timing["runtime_clock"].update(
        period_ns=period,
        frequency_mhz=1000.0 / period,
        period_scope="fastest-domain-summary",
        clock_periods_ns=clock_periods,
    )
    maximum = timing["summary"]["maximum_system_delay_bound_ns"]
    timing["runtime_clock"].update(minimum_safe_period_bound_ns=maximum,
        maximum_safe_frequency_bound_mhz=1000.0/maximum if maximum > 0 else None)
    if metrics["runtime"]["wns_ns"] < 0:
        timing["status"] = "fail"
    timing["global_opensta"] = {"schema": "emuflow.global-opensta-check/v1",
        "authority": "opensta", "execution": "standalone", "status": timing["status"],
        "timing_scope": scope, "original_paths": len(paths), "checks": measurement_count,
        "event_failures": failures, "metrics": metrics,
        "tns_definition": "sum-negative-slack-once-per-original-TimingPathDB-path"}
    return timing


def compare_system_timing(measurements, reference, *, tolerance_ns=1.0e-3):
    """Compare every original path, not merely extrema; return a compact gate.

    OpenSTA uses float-based timing internally. The floor is 1 ps, with four
    float32 relative epsilons on the specific compared value (not the global
    frame). A near-zero slack retains the strict floor even in a huge frame.
"""
    if tolerance_ns <= 0 or not math.isfinite(tolerance_ns):
        raise ValidationError("invalid global STA comparison tolerance")
    expected = {p["path"]: p for p in reference["paths"]}
    if not expected or len(expected) != len(reference["paths"]):
        raise ValidationError("global STA reference has empty/duplicate original paths")
    observed = {}
    identities = set()
    max_error = 0.0
    max_tolerance = tolerance_ns
    failures = 0
    for row in measurements:
        role = row["role"]
        identity = (row["path"], role, row["event"])
        if (role not in {"target", "runtime", "tx", "commit"}
                or row["path"] not in expected or identity in identities):
            raise ValidationError("global STA invalid/duplicate measurement identity")
        identities.add(identity)
        if any(isinstance(row[k], bool) or not math.isfinite(row[k])
               for k in ("arrival_ns", "required_ns", "slack_ns")):
            raise ValidationError("global STA nonfinite measurement")
        if role in {"tx", "commit"}:
            failures += row["slack_ns"] < -tolerance_ns
            continue
        key = (row["path"], role)
        if key in observed or row["path"] not in expected:
            raise ValidationError("global OpenSTA original-path population mismatch")
        observed[key] = row
        old = expected[row["path"]]
        for actual, value in ((row["arrival_ns"], old["system_delay_bound_ns"]),
                              (row["required_ns"], old[f"{role}_required_time_ns"]),
                              (row["slack_ns"], old[f"{role}_clock_slack_bound_ns"])):
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValidationError("global STA nonfinite reference")
            error = abs(actual-value)
            max_error = max(max_error, error)
            allowance = max(tolerance_ns, 4 * 2**-23 * abs(value))
            max_tolerance = max(max_tolerance, allowance)
            if error > allowance:
                raise ValidationError(f"global OpenSTA disagrees at {key}: error {error} ns")
    if set(observed) != {(p, r) for p in expected for r in ("target", "runtime")}:
        raise ValidationError("global OpenSTA original-path coverage is incomplete")
    summary = {}
    for role in ("target", "runtime"):
        slacks = [row["slack_ns"] for row in measurements if row["role"] == role]
        summary[role] = {"wns_ns": min(slacks),
                         "original_path_tns_ns": math.fsum(min(0.0, s) for s in slacks),
                         "negative_paths": sum(s < 0 for s in slacks)}
    return {"schema": "emuflow.global-opensta-check/v1", "status": "fail" if failures else "pass",
            "authority": "qualification-only-pending-full-flow-validation",
            "timing_scope": reference["timing_scope"], "original_paths": len(expected),
            "event_failures": failures, "checks": len(measurements),
            "tolerance_ns": tolerance_ns, "maximum_difference_ns": max_error,
            "maximum_comparison_tolerance_ns": max_tolerance,
            "relative_float32_epsilons": 4,
            "metrics": summary,
            "tns_definition": "sum-negative-slack-once-per-original-TimingPathDB-path"}


def adopt_opensta_results(timing, measurements):
    """Project checked engine values into the canonical report, in place.

    Keep the independent composer as the comparison input, not a second persisted path
    population. All public scalar aliases are updated together.
    """
    gate = compare_system_timing(measurements, timing)
    observed = {(r["path"], r["role"]): r for r in measurements
                if r["role"] in {"target", "runtime"}}
    for path in timing["paths"]:
        path["system_delay_bound_ns"] = observed[path["path"], "target"]["arrival_ns"]
        for role in ("target", "runtime"):
            row = observed[path["path"], role]
            path[f"{role}_required_time_ns"] = row["required_ns"]
            path[f"{role}_clock_slack_bound_ns"] = row["slack_ns"]
    for role in ("target", "runtime"):
        worst = min(timing["paths"], key=lambda p: (
            p[f"{role}_clock_slack_bound_ns"], p["path"]))
        metrics = gate["metrics"][role]
        timing[f"{role}_clock"].update({
            "worst_path": worst["path"],
            "worst_slack_bound_ns": metrics["wns_ns"],
            "negative_slack_paths": metrics["negative_paths"],
            "total_negative_slack_bound_ns": metrics["original_path_tns_ns"],
            "tns_bound_ns": metrics["original_path_tns_ns"],
        })
    maximum = max(p["system_delay_bound_ns"] for p in timing["paths"])
    timing["summary"]["maximum_system_delay_bound_ns"] = maximum
    timing["runtime_clock"]["minimum_safe_period_bound_ns"] = maximum
    timing["runtime_clock"]["maximum_safe_frequency_bound_mhz"] = (
        1000.0 / maximum if maximum > 0 else None)
    if gate["status"] != "pass" or gate["metrics"]["runtime"]["wns_ns"] < 0:
        timing["status"] = "fail"
    gate["authority"] = "opensta"
    gate["cross_checker"] = "independent-python-event-composer"
    timing["global_opensta"] = gate
    return gate
