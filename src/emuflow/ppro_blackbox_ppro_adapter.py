"""Adapter for compact metrics from ordinary PPro 2026 user reports.

The adapter accepts only the normal partition, system-route, and system-timing
reports selected by the caller.  Physical FPGA names are replaced through a
runtime-only alias map before any value leaves this module.  No project XML,
STF, BoardDB, pin map, timing table, or other internal platform file is read.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Mapping

from .errors import ValidationError


PPRO_2026_REPORT_PROFILE = "ppro-2026-ordinary-reports-v1"
_REPORTS = {
    "resource_summary",
    "partition_summary",
    "route_summary",
    "system_timing",
}
_LOGICAL_FPGA = re.compile(r"^F[0-9]+$")
_NORMALIZED_DELAY = re.compile(
    r"normalized\s+delay\s+([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE
)
_RESOURCE_MAP = {
    "LUT": "lut",
    "FF": "ff",
    # A controlled 32 Kib inferred-memory probe maps one-for-one to the
    # ordinary-report BRAM count.  Together with the reported utilization,
    # this identifies the report unit as one RAMB36-class block, not one
    # RAMB18.  Keep that unit explicit here; BoardDB conversion happens only
    # when the calibrated platform is materialized.
    "BRAM": "bram36k",
    "DSP": "dsp48",
    "URAM": "uram288",
}


def _read_text(path: Path, *, maximum_bytes: int = 16 * 1024 * 1024) -> str:
    if not path.is_file():
        raise ValidationError(f"missing ordinary report: {path.name}")
    if path.stat().st_size > maximum_bytes:
        raise ValidationError(f"ordinary report {path.name}: exceeds parser bound")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(f"ordinary report {path.name}: cannot be read") from error


def _pipe_cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def _integer(value: str, context: str) -> int:
    if not re.fullmatch(r"[0-9]+", value.strip()):
        raise ValidationError(f"{context}: expected an integer")
    return int(value)


def _aliases(value: Mapping[str, str]) -> Dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValidationError("PPro report adapter requires runtime FPGA aliases")
    normalized: Dict[str, str] = {}
    logical_seen: set[str] = set()
    for raw_source, raw_target in value.items():
        source = str(raw_source).strip().upper()
        target = str(raw_target).strip().upper()
        if not _LOGICAL_FPGA.fullmatch(source) or not _LOGICAL_FPGA.fullmatch(target):
            raise ValidationError("PPro FPGA aliases must use F<n> names")
        if source in normalized or target in logical_seen:
            raise ValidationError("PPro FPGA aliases are not one-to-one")
        normalized[source] = target
        logical_seen.add(target)
    return normalized


def _logical_fpga(raw: str, aliases: Mapping[str, str], context: str) -> str:
    source = raw.strip().upper()
    if source.isdigit():
        source = f"F{source}"
    if source not in aliases:
        raise ValidationError(f"{context}: unmapped physical FPGA alias")
    return aliases[source]


def _parse_partition_report(
    text: str, aliases: Mapping[str, str]
) -> tuple[Dict[str, float], list[Dict[str, Any]], list[Dict[str, str]]]:
    header: list[str] | None = None
    device_demand: Dict[str, Dict[str, float]] = {}
    device_utilization: Dict[str, Dict[str, float]] = {}
    total_demand: Dict[str, float] | None = None
    for line in text.splitlines():
        cells = _pipe_cells(line)
        if not cells:
            continue
        if cells[0] == "Resource Type":
            header = cells
            continue
        if header is None or len(cells) != len(header):
            continue
        row_name = cells[0]
        if row_name not in aliases and row_name not in {"Total Resource", "Total Util"}:
            continue
        selected: Dict[str, float] = {}
        for column, raw_value in zip(header[1:], cells[1:]):
            resource = _RESOURCE_MAP.get(column)
            if resource is None:
                continue
            if raw_value.endswith("%"):
                amount = _integer(raw_value[:-1].strip(), f"{row_name} {column} utilization")
                if amount > 100:
                    raise ValidationError("PPro resource utilization exceeds 100 percent")
                selected[resource] = amount / 100.0
            else:
                selected[resource] = float(_integer(raw_value, f"{row_name} {column} demand"))
        if not selected:
            continue
        if row_name == "Total Resource":
            total_demand = selected
        elif row_name == "Total Util":
            continue
        elif any(raw.endswith("%") for raw in cells[1:]):
            device_utilization[_logical_fpga(row_name, aliases, "partition report")] = selected
        else:
            device_demand[_logical_fpga(row_name, aliases, "partition report")] = selected

    if not device_demand or not device_utilization:
        raise ValidationError("PPro partition report lacks device resource tables")
    if set(device_demand) != set(device_utilization):
        raise ValidationError("PPro partition report resource tables disagree on active FPGAs")
    summed = {
        resource: sum(row.get(resource, 0.0) for row in device_demand.values())
        for resource in sorted(set().union(*device_demand.values()))
    }
    if total_demand is None or any(
        total_demand.get(resource) != amount for resource, amount in summed.items()
    ):
        raise ValidationError("PPro partition report total resource row is inconsistent")
    active = sorted(device_demand)
    return (
        {name: total_demand[name] for name in sorted(total_demand)},
        [
            {"fpga": fpga, "resources": device_utilization[fpga]}
            for fpga in active
        ],
        [
            {"partition": f"P{index}", "fpga": fpga}
            for index, fpga in enumerate(active)
        ],
    )


def _shortest_hops(
    edges: set[tuple[str, str]], source: str, sink: str
) -> int | None:
    queue = deque([(source, 0)])
    seen = {source}
    while queue:
        node, depth = queue.popleft()
        for left, right in sorted(edges):
            if left != node or right in seen:
                continue
            if right == sink:
                return depth + 1
            seen.add(right)
            queue.append((right, depth + 1))
    return None


def _parse_route_report(
    text: str, aliases: Mapping[str, str]
) -> tuple[list[Dict[str, Any]], Dict[str, float]]:
    section = ""
    tdm_counts: Dict[tuple[str, str], int] = defaultdict(int)
    untdm_counts: Dict[tuple[str, str], int] = defaultdict(int)
    direct_edges: set[tuple[str, str]] = set()
    ratio_bounds: Dict[tuple[str, str], int] = defaultdict(int)
    for line in text.splitlines():
        lowered = line.strip().lower()
        if lowered.startswith("2.1 fpga tdm connect net num"):
            section = "tdm"
            continue
        if lowered.startswith("2.2 fpga untdm connect net num"):
            section = "untdm"
            continue
        if lowered.startswith("2.3 tdm cable info report"):
            section = "cable"
            continue
        if lowered.startswith("2.4 tdm_info report"):
            section = "info"
            continue
        if lowered.startswith("2.5 "):
            section = ""
            continue
        cells = _pipe_cells(line)
        if not cells:
            continue
        if section in {"tdm", "untdm"} and len(cells) == 3:
            if not cells[0].isdigit() or not cells[1].isdigit() or not cells[2].isdigit():
                continue
            pair = (
                _logical_fpga(cells[0], aliases, "route source"),
                _logical_fpga(cells[1], aliases, "route sink"),
            )
            (tdm_counts if section == "tdm" else untdm_counts)[pair] += int(cells[2])
        elif section == "cable" and len(cells) == 4:
            if not cells[0].isdigit() or not cells[1].isdigit():
                continue
            source = _logical_fpga(cells[0], aliases, "cable source")
            sink = _logical_fpga(cells[1], aliases, "cable sink")
            direct_edges.add((source, sink))
            direct_edges.add((sink, source))
        elif section == "info" and len(cells) == 9:
            if not re.fullmatch(r"F[0-9]+", cells[0], re.IGNORECASE):
                continue
            source = _logical_fpga(cells[0], aliases, "TDM source")
            sink = _logical_fpga(cells[1], aliases, "TDM sink")
            ratio_bounds[(source, sink)] = max(
                ratio_bounds[(source, sink)], _integer(cells[8], "TDM max ratio")
            )

    all_pairs = sorted(set(tdm_counts) | set(untdm_counts))
    routes = []
    for index, pair in enumerate(all_pairs):
        signal_count = tdm_counts[pair] + untdm_counts[pair]
        if signal_count <= 0:
            continue
        hops = _shortest_hops(direct_edges, pair[0], pair[1])
        if hops is None:
            raise ValidationError("PPro route report cannot explain an observed FPGA pair")
        routes.append(
            {
                "id": f"route{index}",
                "source": pair[0],
                "sinks": [pair[1]],
                "effective_hops": hops,
                "signal_count": signal_count,
            }
        )
    if all_pairs and not routes:
        raise ValidationError("PPro route report contains no usable route load")
    active_ratios = [ratio_bounds[pair] for pair in tdm_counts if tdm_counts[pair] > 0]
    maximum_ratio = max(active_ratios, default=1 if untdm_counts else 0)
    if tdm_counts and maximum_ratio < 1:
        raise ValidationError("PPro route report lacks a TDM ratio for TDM traffic")
    return routes, {
        "cross_fpga_path_count": float(sum(route["signal_count"] for route in routes)),
        "maximum_tdm_ratio": float(maximum_ratio),
        "route_count": float(len(routes)),
        "tdm_signal_count": float(sum(tdm_counts.values())),
        "untdm_signal_count": float(sum(untdm_counts.values())),
    }


def _parse_timing_report(text: str) -> Dict[str, float]:
    values = [float(match) for match in _NORMALIZED_DELAY.findall(text)]
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValidationError("PPro timing report contains an invalid normalized delay")
    if not values:
        # A normal PPro application run can emit an empty SSTA report while
        # still producing valid partition and system-route reports.  Preserve
        # those independent black-box observations instead of rejecting the
        # entire run.  Latency-fit and promotion gates separately require the
        # normalized-delay metric, so an empty report cannot become timing
        # calibration or promotion evidence.
        return {}
    return {"sr0_worst_cross_fpga_delay_ns": max(values)}


def parse_ppro_2026_ordinary_reports(
    report_paths: Mapping[str, Path],
    design_metrics: Mapping[str, float],
    fpga_aliases: Mapping[str, str],
) -> Dict[str, Any]:
    """Return vendor-neutral compact metrics from explicitly selected reports."""
    if set(report_paths) != _REPORTS:
        raise ValidationError("PPro report adapter received an incomplete allowlist")
    aliases = _aliases(fpga_aliases)
    partition_text = _read_text(report_paths["partition_summary"])
    if report_paths["resource_summary"].resolve() != report_paths[
        "partition_summary"
    ].resolve():
        resource_text = _read_text(report_paths["resource_summary"])
        if resource_text != partition_text:
            raise ValidationError(
                "PPro 2026 adapter requires the same pa0 report for resource and partition summaries"
            )
    resource_demand, utilization, assignments = _parse_partition_report(
        partition_text, aliases
    )
    routes = []
    communication: Dict[str, float] = {}
    if report_paths["route_summary"].is_file():
        route_text = _read_text(report_paths["route_summary"])
        routes, communication = _parse_route_report(route_text, aliases)
    timing: Dict[str, float] = {}
    if report_paths["system_timing"].is_file():
        timing_text = _read_text(report_paths["system_timing"])
        timing = _parse_timing_report(timing_text)
    return {
        "design": dict(design_metrics),
        "resource_demand": resource_demand,
        "fpga_utilization": utilization,
        "assignments": assignments,
        "routes": routes,
        "communication": communication,
        "timing": timing,
    }
