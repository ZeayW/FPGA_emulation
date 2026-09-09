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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import ValidationError
from .native_tools import resolve_native_executable
from .opensta import render_opensta_liberty


@dataclass(frozen=True)
class EventCheck:
    path: str
    role: str
    event: str
    launch_ns: float
    arcs_ns: tuple[float, ...]
    required_ns: float


def validate_checks(checks: Iterable[EventCheck]) -> list[EventCheck]:
    rows = list(checks)
    seen = set()
    for row in rows:
        key = (row.path, row.role, row.event)
        if key in seen or not row.path or not row.event:
            raise ValidationError("global STA has duplicate/empty check identity")
        seen.add(key)
        if row.role not in {"target", "runtime", "tx", "commit"}:
            raise ValidationError("global STA has an unsupported check role")
        for value in (row.launch_ns, row.required_ns, *row.arcs_ns):
            if isinstance(value, bool) or not math.isfinite(value):
                raise ValidationError("global STA times must be finite numbers")
        if row.launch_ns < 0 or any(x < 0 for x in row.arcs_ns):
            raise ValidationError("global STA launch/arc delay must be nonnegative")
    if not rows:
        raise ValidationError("global STA has no timing checks")
    target = {r.path for r in rows if r.role == "target"}
    runtime = {r.path for r in rows if r.role == "runtime"}
    if target != runtime or not target:
        raise ValidationError("global STA target/runtime population disagrees")
    counts = Counter((r.path, r.role) for r in rows)
    if any(counts[p, role] != 1
           for p in target for role in ("target", "runtime")):
        raise ValidationError("global STA must observe each original path once")
    observations = {(r.path, r.role): r for r in rows
                    if r.role in {"target", "runtime"}}
    for row in rows:
        if row.path not in target:
            raise ValidationError("global STA event has no original-path observation")
    for path in target:
        a, b = observations[path, "target"], observations[path, "runtime"]
        if (a.launch_ns, a.arcs_ns) != (b.launch_ns, b.arcs_ns):
            raise ValidationError("global STA target/runtime physical chains disagree")
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
  set ::emuflow_cell [[sta::top_instance] cell]
  set constraints [open global_timing.sdc r]
  while {{[gets $constraints line] >= 0}} {{
    uplevel #0 [string map [list {{[get_ports }} {{[$::emuflow_cell find_port }}] $line]
  }}
  close $constraints
  set out [open measurements.tsv w]
  puts $out "endpoint\\tarrival_ns\\trequired_ns\\tslack_ns"
  puts "global STA: query checks"
  set paths [find_timing_paths -path_delay max -group_count {len(rows)} -endpoint_count 1]
  puts "global STA: serialize checks"
  foreach p $paths {{
    # PathEnd scalar APIs use seconds. Do not expand/copy every PathRef point
    # merely to obtain the endpoint arrival; the scalar API is sufficient.
    set arrival [expr {{[$p data_arrival_time] * 1.0e9}}]
    set required [expr {{[$p data_required_time] * 1.0e9}}]
    set slack [expr {{[$p slack] * 1.0e9}}]
    set endpoint [get_property [get_property $p endpoint] full_name]
    puts $out "$endpoint\\t$arrival\\t$required\\t$slack"
  }}
  close $out
}}
if {{[catch {{analyze}} message]}} {{ puts stderr $message; exit 2 }}
exit 0
''')
    return rows


def read_measurements(path: Path, rows: list[EventCheck]) -> list[dict]:
    values = {}
    with path.open() as stream:
        if next(stream, "").strip() != "endpoint\tarrival_ns\trequired_ns\tslack_ns":
            raise ValidationError("invalid global STA measurements header")
        for line in stream:
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 4 or fields[0] in values:
                raise ValidationError("duplicate/malformed global STA endpoint")
            try:
                numbers = tuple(float(v) for v in fields[1:])
            except ValueError as exc:
                raise ValidationError("invalid global STA measurement") from exc
            if not all(math.isfinite(x) for x in numbers):
                raise ValidationError("nonfinite global STA measurement")
            values[fields[0]] = numbers
    if set(values) != {f"o{i}" for i in range(len(rows))}:
        raise ValidationError("global STA missing/extra/unconstrained endpoints")
    # Validate the exported event binding, including TX/commit rows which do
    # not belong to the original-path TNS population. A corrupt positive slack
    # must not conceal a late event. This is a linear independent arc check,
    # not an optimization replay or a second persisted timing population.
    for i, row in enumerate(rows):
        arrival = math.fsum(row.arcs_ns)
        required = row.required_ns - row.launch_ns
        for actual, expected in zip(values[f"o{i}"],
                                    (arrival, required, required-arrival)):
            if abs(actual-expected) > max(1e-3, 4 * 2**-23 * abs(expected)):
                raise ValidationError(f"global STA event binding disagrees at o{i}")
    return [{"path": r.path, "role": r.role, "event": r.event,
             "arrival_ns": values[f"o{i}"][0] + r.launch_ns,
             "required_ns": values[f"o{i}"][1] + r.launch_ns,
             "slack_ns": values[f"o{i}"][2]} for i, r in enumerate(rows)]


def run_event_checks(checks: Iterable[EventCheck], directory: Path,
                     executable: str | None = None) -> list[dict]:
    rows = export_event_checks(checks, directory)
    tool = resolve_native_executable("sta", executable)
    output = directory / "measurements.tsv"
    output.unlink(missing_ok=True)
    with (directory / "opensta.log").open("w") as log:
        result = subprocess.run([tool, "-exit", "analyze.tcl"], cwd=directory,
                                stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode or not output.is_file():
        raise ValidationError("global OpenSTA failed; see opensta.log")
    return read_measurements(output, rows)


def read_engine_identity(log_path: Path) -> dict:
    """Read the existing process banner, without another tool invocation."""
    with log_path.open() as stream:
        for _ in range(16):
            line = stream.readline(4096)
            match = re.match(r"OpenSTA\s+(\S+)\s+([0-9a-f]{7,40})\b", line)
            if match:
                return {"name": "OpenSTA", "version": match[1], "revision": match[2]}
    raise ValidationError("global OpenSTA log lacks engine version/revision")


def bind_physical_checks(runtime, routes, schedule, physical, platform):
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
    entries = {e["id"]: e for e in schedule["entries"]}
    demand_by_net = {r["net"]: r["id"] for r in routes["routes"]}
    uncertainty = float(physical.get("static_exact_clock_uncertainty_ns", 0.0))
    if uncertainty < 0 or not math.isfinite(uncertainty):
        raise ValidationError("invalid global STA uncertainty")
    virtual = float(runtime["virtual_dut_clock"]["nominal_period_ns"])
    result = []
    for record in reconstruct_tdm_schedule_timing_paths(routes, platform, schedule):
        by_demand = {}
        for hop in record["scheduled_hops"]:
            by_demand.setdefault(hop["demand"], []).append(hop)
        members = record.get("compressed_path_ids", [record["path"]])
        for member in members:
            chain = sorted(segments.get(record["path"], {}).get(member, []),
                           key=lambda s: s["cut_index"])
            if [s["cut_index"] for s in chain] != list(range(len(record["cut_nets"])+1)):
                raise ValidationError("global STA missing original-member logic segments")
            origin = 0.0
            trailing = ()
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
                    entry = entries[eid]
                    for field in ("link", "from", "to"):
                        if entry[field] != hop[field]:
                            raise ValidationError("global STA hop/event identity disagrees")
                    period = float(hop["tdm_slot_ns"])
                    periods.add(period)
                    tx = entry["slot"] * period
                    arcs = trailing + (float(chain[k]["delay_ns"]) if not j else
                                       endpoints[hop["tx_endpoint"]],)
                    # Previous-frame registered values are stable at first TX.
                    # Only that check is exempt; relays retain current-frame causality.
                    if not (schedule["transport_semantics"] == "registered-boundary" and not j):
                        result.append(EventCheck(member, "tx", eid, origin, arcs, tx-uncertainty))
                    origin = tx
                    trailing = (links[hop["link"], hop["from"], hop["to"]],
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
                result.append(EventCheck(member, role, "capture", origin, final_arcs, deadline))
    for database in physical.get("local_path_timing", {}).values():
        for path in database["paths"]:
            required = float(path.get("required_time_ns", path["clock_period_ns"]))
            for role, deadline in (("target", required),
                                   ("runtime", virtual-(path["clock_period_ns"]-required))):
                result.append(EventCheck(path["id"], role, "capture", 0.0,
                                         (float(path["delay_ns"]),), deadline))
    return validate_checks(result)


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

    This projection is separate from qualification. The flow must not enable
    it until its complete physical qualification gate has passed. Keep the
    independent composer as the comparison input, not a second persisted path
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
