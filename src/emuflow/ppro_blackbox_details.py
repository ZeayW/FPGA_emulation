"""Compact, path-preserving facts from ordinary PPro calibration reports.

These are observations, not a fitted platform.  In particular ``channelNum``
is retained as a reported quantity, not relabelled as a bank's capacity, and a
run-wide maximum TDM ratio is never attached to a timing path.  Until a report
exposes an unambiguous cross-FPGA segment and its ratio, single-hop delay is
explicitly not identifiable.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping

from .errors import ValidationError


DETAILS_SCHEMA = "emuflow.ppro-calibration-details/v2"
_LEGACY_SCHEMA = "emuflow.ppro-calibration-details/v1"
_FPGA = re.compile(r"^F[0-9]+$")
_SUMMARY = re.compile(
    r"^(Path[0-9]+)\s+(.+?)\s{2,}([0-9]+)\s+"
    r"(-?[0-9]+(?:\.[0-9]+)?)\s{2,}"
)
_PATH = re.compile(r"^(Path[0-9]+)\s*$")
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_ARRIVAL = re.compile(rf"^\s*({_NUMBER})\s+data arrival time\s*$", re.I)
_POINT = re.compile(rf"^\s*({_NUMBER})\s+({_NUMBER})\s+(.+?)\s*$")
_RATIO = re.compile(r"\bTDM\s+(?:ratio|Ratio)\s*[:=]\s*([0-9]+)\b", re.I)
_FPGA_IN_NAME = re.compile(r"(?<![A-Za-z0-9_])(?:FPGA_([0-9]+)|F([0-9]+))(?=/|\b)")
_BANK_HEADER = [
    "FPGAID", "targetFPGAID", "BankID", "targetBankID", "dir",
    "eringDevID", "lineRate", "channelNum", "maxRatio",
]
_REPORT_ROLES = {"resource_summary", "partition_summary", "route_summary", "system_timing", "route_timing", "net_tdm"}
_ROUTE_POINT = re.compile(rf"^\s*({_NUMBER})\s+({_NUMBER})\s+FPGA_([0-9]+)\s+(?:([0-9]+)\s+)?(.+?)\s*$")
_ROUTE_ARRIVAL = re.compile(rf"^\s*({_NUMBER})\s+data arrival time\s+\(\s*normalized delay\s+({_NUMBER})\s*\)\s*$", re.I)


def _safe_label(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(c in value for c in "\n\r\x00") or value.startswith(("/", "~")) or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", value):
        raise ValidationError(f"{context}: invalid compact label")
    return value


def _parse_net_tdm(text: str, aliases: Mapping[str, str]) -> tuple[list[dict[str, Any]], str]:
    """Parse only the observed ordinary untdm table; unknown tables stay partial."""
    if not text.strip():
        return [], "unavailable"
    section = ""
    header_seen = False
    complete = True
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        title = re.fullmatch(r"[0-9]+\.\s*(.+?)\s+report", stripped, re.I)
        if title:
            section = title.group(1).lower()
            header_seen = False
            # Unknown sections may use non-table text or an empty/new table
            # format. A later recognized untdm table must not turn an omitted
            # section into a claim that the whole net report was captured.
            if section not in {"untdm", "tdm_set"}:
                complete = False
            continue
        if not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if section == "untdm" and cells == ["net name", "FPGA Pair", "is clock"]:
            header_seen = True
            continue
        if section == "tdm_set":
            # This is a user constraint table, not an observed routing table.
            continue
        if not cells or all(not cell or re.fullmatch(r"[-+]+", cell) for cell in cells):
            continue
        if section != "untdm" or not header_seen:
            complete = False
            continue
        if len(cells) != 3:
            raise ValidationError("PPro net TDM report: unsupported untdm columns")
        pair = re.fullmatch(r"\(\s*(F[0-9]+)\s*,\s*(F[0-9]+)\s*\)", cells[1])
        if pair is None or cells[2] not in {"0", "1"}:
            raise ValidationError("PPro net TDM report: invalid pair or clock flag")
        net_name, _ = _descriptor(cells[0], aliases)
        rows.append({
            "net_token": _token("net", net_name), "net_name": net_name,
            "source_fpga": _logical(pair.group(1), aliases),
            "sink_fpga": _logical(pair.group(2), aliases),
            "transport_kind": "untdm", "is_clock": cells[2] == "1",
        })
    return rows, "complete" if complete and header_seen else "partial"


def _route_endpoint(value: str, aliases: Mapping[str, str]) -> str:
    match = re.fullmatch(r"FPGA_([0-9]+)_ins/(.+)", value.strip())
    if match:
        return _logical(match.group(1), aliases) + "/" + _safe_label(match.group(2), "route endpoint")
    sanitized, _ = _descriptor(value, aliases)
    return sanitized


def _route_crossings(points: list[dict[str, Any]], nets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    crossings = []
    unbound = 0
    for previous, point in zip(points, points[1:]):
        if previous["fpga"] == point["fpga"]:
            continue
        if previous["pin_token"] != point["pin_token"] or point["reported_tdm_ratio"] is None:
            unbound += 1
            continue
        delay = point["cumulative_ns"] - previous["cumulative_ns"]
        if delay < 0 or not math.isclose(delay, point["increment_ns"], abs_tol=0.011):
            raise ValidationError("PPro route timing: crossing increment/cumulative disagreement")
        pin_name = point["descriptor"].split(" (", 1)[0]
        candidates = {pin_name}
        # The observed system-router BUF_X1 repeats the exact data net on
        # each Die as '<net>_buf/Z'. No generic suffix/prefix fuzzy matching.
        if point["descriptor"].endswith(" (BUF_X1)") and pin_name.endswith("_buf/Z"):
            candidates.add(pin_name[:-len("_buf/Z")])
        matches = [net for net in nets if net["net_name"] in candidates
                   and net["source_fpga"] == previous["fpga"]
                   and net["sink_fpga"] == point["fpga"]]
        bound = matches[0] if len(matches) == 1 else None
        crossings.append({
            "source_fpga": previous["fpga"], "sink_fpga": point["fpga"],
            "source_point_id": previous["id"], "sink_point_id": point["id"],
            "pin_token": point["pin_token"],
            "reported_tdm_ratio": point["reported_tdm_ratio"],
            "delay_ns": point["increment_ns"],
            "net_token": bound["net_token"] if bound else None,
            "net_name": bound["net_name"] if bound else None,
            "transport_kind": bound["transport_kind"] if bound else "unbound",
        })
    return crossings, unbound


def _parse_route_timing(text: str, aliases: Mapping[str, str], nets: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int | None]:
    limit_match = re.search(r"Critical Path Report \(Top ([0-9]+) records will be listed at most", text)
    limit = int(limit_match.group(1)) if limit_match else None
    paths: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    endpoint_kind: str | None = None
    table_header = False
    for line in text.splitlines():
        stripped = line.strip()
        start = re.fullmatch(r"Startpoint\s*:\s*(.+)", stripped)
        if start:
            current = {"id": f"route-path{len(paths)}", "startpoint_token": _token("pin", _route_endpoint(start.group(1), aliases)),
                       "endpoint_token": None, "source_clock": None, "sink_clock": None,
                       "arrival_ns": None, "normalized_delay_ns": None,
                       "points": [], "crossings": [], "unbound_crossing_count": 0}
            paths.append(current)
            endpoint_kind = "source_clock"
            table_header = False
            continue
        if current is None or current["arrival_ns"] is not None:
            continue
        end = re.fullmatch(r"Endpoint\s*:\s*(.+)", stripped)
        if end:
            current["endpoint_token"] = _token("pin", _route_endpoint(end.group(1), aliases))
            endpoint_kind = "sink_clock"
            continue
        clock = re.fullmatch(r"\(clocked by (.+)\)", stripped)
        if clock and endpoint_kind:
            current[endpoint_kind] = _safe_label(clock.group(1), "route clock")
            continue
        if stripped.split() == ["Delay(ns)", "Time(ns)", "Die", "TDM_ratio", "pin_name"]:
            table_header = True
            continue
        arrival = _ROUTE_ARRIVAL.fullmatch(line)
        if arrival:
            current["arrival_ns"] = _number(float(arrival.group(1)), "route arrival")
            current["normalized_delay_ns"] = _number(float(arrival.group(2)), "route normalized delay")
            continue
        point = _ROUTE_POINT.fullmatch(line)
        if point:
            if not table_header:
                raise ValidationError("PPro route timing: unverified point columns")
            descriptor, _ = _descriptor(point.group(5), aliases)
            current["points"].append({
                "id": f"point{len(current['points'])}", "pin_token": _token("pin", descriptor),
                "descriptor": descriptor, "fpga": _logical(point.group(3), aliases),
                "increment_ns": _number(float(point.group(1)), "route increment", nonnegative=False),
                "cumulative_ns": _number(float(point.group(2)), "route cumulative", nonnegative=False),
                "reported_tdm_ratio": int(point.group(4)) if point.group(4) else None, "kind": "pin",
            })
        elif table_header and _POINT.match(line):
            raise ValidationError("PPro route timing: unsupported numeric point row")
    for path in paths:
        path["crossings"], path["unbound_crossing_count"] = _route_crossings(path["points"], nets)
    if paths and limit is None:
        raise ValidationError("PPro route timing: missing report coverage limit")
    return paths, limit


def _token(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _number(value: Any, context: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{context}: expected a number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValidationError(f"{context}: invalid finite number")
    return result


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(f"{context}: expected a nonnegative integer")
    return value


def _keys(value: Any, keys: set[str], context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValidationError(f"{context}: unexpected or missing fields")
    return value


def _list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{context}: expected a list")
    return value


def _logical(value: str, aliases: Mapping[str, str]) -> str:
    raw = value.upper()
    if raw.isdigit():
        raw = f"F{raw}"
    if raw not in aliases:
        raise ValidationError("PPro bank report: unmapped physical FPGA alias")
    return aliases[raw]


def _descriptor(value: str, aliases: Mapping[str, str]) -> tuple[str, str | None]:
    """Keep short design/cell labels, replace physical FPGA identities.

    ``FPGA_n`` is not assumed to mean the physical route label F<n>.  Its
    opaque token and a null logical FPGA preserve that ambiguity.  Relative
    design hierarchy is useful evidence; absolute paths are never retained.
    """
    value = re.sub(r"^[\^v]\s+", "", value.strip())
    if len(value) > 256 or not value or any(c in value for c in "\n\r\x00"):
        raise ValidationError("PPro SSTA point: invalid descriptor")
    if value.startswith(("/", "~")) or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", value):
        raise ValidationError("PPro SSTA point: absolute paths are forbidden")
    found: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        logical = aliases.get(raw) if match.group(2) else None
        if logical is not None:
            found.add(logical)
            return logical
        return "unmapped-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    sanitized = _FPGA_IN_NAME.sub(replace, value)
    return sanitized, next(iter(found)) if len(found) == 1 else None


def _parse_banks(text: str, aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    in_section = False
    header_seen = False
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("2.4 tdm_info report"):
            in_section = True
            continue
        if in_section and re.match(r"2\.[0-9]+\s", stripped):
            break
        if not in_section or not (stripped.startswith("|") and stripped.endswith("|")):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if cells == _BANK_HEADER:
            header_seen = True
            continue
        if not cells or not _FPGA.fullmatch(cells[0].upper()):
            continue
        if not header_seen or len(cells) != len(_BANK_HEADER):
            raise ValidationError("PPro bank report: unsupported tdm_info columns")
        local, peer = _logical(cells[0], aliases), _logical(cells[1], aliases)
        direction = cells[4].lower()
        if local == peer or direction not in {"input", "output"}:
            raise ValidationError("PPro bank report: invalid endpoints or direction")
        if not all(re.fullmatch(r"[0-9]+", cells[i]) for i in (2, 3, 7, 8)):
            raise ValidationError("PPro bank report: invalid count or bank identifier")
        try:
            line_rate = float(cells[6])
        except ValueError as error:
            raise ValidationError("PPro bank report: invalid reported line rate") from error
        endpoints = sorted(((local, cells[2]), (peer, cells[3])))
        rows.append({
            "bank_pair_id": _token("bank", repr(endpoints)),
            "local_fpga": local,
            "peer_fpga": peer,
            "direction": direction,
            "reported_channel_count": int(cells[7]),
            "maximum_tdm_ratio": int(cells[8]),
            "reported_line_rate": _number(line_rate, "reported line rate"),
        })
    # Reciprocal endpoint rows remain separate facts.  Their channel counts
    # must never be summed as two independent banks by a capacity consumer.
    return sorted(rows, key=lambda row: (row["bank_pair_id"], row["local_fpga"], row["direction"]))


def _section(line: str, current: str) -> str:
    match = re.match(r"^\s*##\s*(Setup|Hold)\s*##", line, re.I)
    return match.group(1).lower() if match else current


def _parse_paths(text: str, aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    summaries: dict[tuple[str, str], tuple[bool, int]] = {}
    section = "unspecified"
    for line in text.splitlines():
        section = _section(line, section)
        match = _SUMMARY.match(line.strip())
        if match is None:
            continue
        key = (section, match.group(1))
        value = ("unconstrained" not in match.group(2).lower(), int(match.group(3)))
        if key in summaries and summaries[key] != value:
            raise ValidationError("PPro SSTA report: ambiguous duplicate path identifier")
        summaries[key] = value

    paths: dict[tuple[str, str], dict[str, Any]] = {}
    section = "unspecified"
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        section = _section(line, section)
        start = _PATH.fullmatch(line.strip())
        if start is not None:
            key = (section, start.group(1))
            current = None
            if key not in summaries:
                continue
            if key in paths:
                raise ValidationError("PPro SSTA report: repeated path details")
            constrained, crossings = summaries[key]
            current = {
                "id": f"{section}-{start.group(1).lower()}",
                "constrained": constrained,
                "reported_cross_fpga_count": crossings,
                "arrival_ns": None,
                "startpoint_token": None,
                "endpoint_token": None,
                "points": [],
                "crossings": [],
            }
            paths[key] = current
            continue
        if current is None or current["arrival_ns"] is not None:
            # Ignore the later negative arrival operand in the slack summary.
            continue
        for marker, field in (("Startpoint:", "startpoint_token"), ("Endpoint:", "endpoint_token")):
            if line.strip().startswith(marker):
                descriptor, _ = _descriptor(line.strip()[len(marker):], aliases)
                current[field] = _token("pin", descriptor)
        arrival = _ARRIVAL.match(line)
        if arrival is not None:
            current["arrival_ns"] = _number(float(arrival.group(1)), "SSTA arrival")
            continue
        point = _POINT.match(line)
        if point is None:
            continue
        descriptor, fpga = _descriptor(point.group(3), aliases)
        ratio = _RATIO.search(descriptor)
        current["points"].append({
            "id": f"point{len(current['points'])}",
            "pin_token": _token("pin", descriptor),
            "descriptor": descriptor,
            "fpga": fpga,
            "increment_ns": _number(float(point.group(1)), "SSTA increment", nonnegative=False),
            "cumulative_ns": _number(float(point.group(2)), "SSTA cumulative", nonnegative=False),
            "reported_tdm_ratio": int(ratio.group(1)) if ratio else None,
            "kind": "pin" if "/" in descriptor else "other",
        })
    return sorted(paths.values(), key=lambda path: path["id"])


def parse_calibration_details(
    route_text: str, timing_text: str, aliases: Mapping[str, str], *,
    route_timing_text: str = "", net_tdm_text: str = "",
    report_sha256: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Extract evidence without claiming bank inventory or segment timing."""
    banks = _parse_banks(route_text, aliases)
    paths = _parse_paths(timing_text, aliases)
    nets, net_status = _parse_net_tdm(net_tdm_text, aliases)
    route_paths, path_limit = _parse_route_timing(route_timing_text, aliases, nets)
    bound_route_segments = any(path["crossings"] for path in route_paths)
    reasons = ["bank-inventory-not-reported", "tdm-ratio-semantics-unvalidated" if bound_route_segments else "crossing-boundary-and-ratio-not-bound"]
    if not banks:
        reasons.append("no-bank-allocation-rows")
    if not paths and not route_paths:
        reasons.append("no-path-specific-timing")
    if any(point["fpga"] is None and "unmapped-" in point["descriptor"]
           for path in paths for point in path["points"]):
        reasons.append("unmapped-timing-fpga-label")
    return validate_calibration_details({
        "schema": DETAILS_SCHEMA,
        "bank_rows": banks,
        "timing_paths": paths,
        "route_timing_paths": route_paths,
        "net_tdm_rows": nets,
        "net_tdm_capture_status": net_status,
        "route_timing_path_limit": path_limit,
        "report_sha256": dict(report_sha256 or {}),
        "identification": {
            "bank_inventory": "not_identifiable",
            "single_hop_delay": "not_identifiable",
            "reasons": sorted(reasons),
        },
    })


def validate_calibration_details(value: Any) -> dict[str, Any]:
    """Validate the observation-only contract; no inferred fit eligibility."""
    legacy = isinstance(value, Mapping) and value.get("schema") == _LEGACY_SCHEMA
    fields = {"schema", "bank_rows", "timing_paths", "identification"}
    if not legacy:
        fields |= {"route_timing_paths", "net_tdm_rows", "net_tdm_capture_status", "route_timing_path_limit", "report_sha256"}
    root = _keys(value, fields, "calibration details")
    if root["schema"] not in {DETAILS_SCHEMA, _LEGACY_SCHEMA}:
        raise ValidationError("calibration details: unsupported schema")
    banks = []
    bank_seen: set[tuple[str, str, str]] = set()
    for raw in _list(root["bank_rows"], "bank rows"):
        row = _keys(raw, {"bank_pair_id", "local_fpga", "peer_fpga", "direction", "reported_channel_count", "maximum_tdm_ratio", "reported_line_rate"}, "bank row")
        if not isinstance(row["bank_pair_id"], str) or not re.fullmatch(r"bank-[0-9a-f]{64}", row["bank_pair_id"]):
            raise ValidationError("bank row: invalid opaque bank identifier")
        if any(not isinstance(row[key], str) or not _FPGA.fullmatch(row[key]) for key in ("local_fpga", "peer_fpga")):
            raise ValidationError("bank row: invalid logical FPGA")
        key = (row["bank_pair_id"], row["local_fpga"], row["direction"])
        if row["local_fpga"] == row["peer_fpga"] or row["direction"] not in {"input", "output"} or key in bank_seen:
            raise ValidationError("bank row: duplicate or invalid directional endpoint")
        bank_seen.add(key)
        banks.append({**row,
            "reported_channel_count": _integer(row["reported_channel_count"], "bank channels"),
            "maximum_tdm_ratio": _integer(row["maximum_tdm_ratio"], "bank ratio"),
            "reported_line_rate": _number(row["reported_line_rate"], "bank line rate"),
        })
    paths = []
    path_seen: set[str] = set()
    for raw in _list(root["timing_paths"], "timing paths"):
        path = _keys(raw, {"id", "constrained", "reported_cross_fpga_count", "arrival_ns", "startpoint_token", "endpoint_token", "points", "crossings"}, "timing path")
        if not isinstance(path["id"], str) or not re.fullmatch(r"(?:setup|hold|unspecified)-path[0-9]+", path["id"]) or path["id"] in path_seen:
            raise ValidationError("timing path: invalid or duplicate identity")
        path_seen.add(path["id"])
        if type(path["constrained"]) is not bool:
            raise ValidationError("timing path: invalid constrained flag")
        for field in ("startpoint_token", "endpoint_token"):
            if path[field] is not None and (not isinstance(path[field], str) or not re.fullmatch(r"pin-[0-9a-f]{64}", path[field])):
                raise ValidationError("timing path: invalid endpoint token")
        if path["crossings"] != []:
            raise ValidationError("timing path: crossing extraction is not yet qualified")
        points = []
        for index, raw_point in enumerate(_list(path["points"], "timing points")):
            point = _keys(raw_point, {"id", "pin_token", "descriptor", "fpga", "increment_ns", "cumulative_ns", "reported_tdm_ratio", "kind"}, "timing point")
            if point["id"] != f"point{index}" or not isinstance(point["pin_token"], str) or not re.fullmatch(r"pin-[0-9a-f]{64}", point["pin_token"]):
                raise ValidationError("timing point: invalid identity")
            descriptor = point["descriptor"]
            if not isinstance(descriptor, str) or not descriptor or len(descriptor) > 256 or any(c in descriptor for c in "\n\r\x00") or descriptor.startswith(("/", "~")) or re.search(r"(?:^|\s)[A-Za-z]:[\\/]", descriptor) or re.search(r"\bFPGA_[0-9]+\b", descriptor):
                raise ValidationError("timing point: invalid compact descriptor")
            if point["pin_token"] != _token("pin", descriptor):
                raise ValidationError("timing point: descriptor token mismatch")
            if point["fpga"] is not None and (not isinstance(point["fpga"], str) or not _FPGA.fullmatch(point["fpga"])):
                raise ValidationError("timing point: invalid logical FPGA")
            if point["kind"] not in {"pin", "other"}:
                raise ValidationError("timing point: invalid kind")
            points.append({**point,
                "increment_ns": _number(point["increment_ns"], "point increment", nonnegative=False),
                "cumulative_ns": _number(point["cumulative_ns"], "point cumulative", nonnegative=False),
                "reported_tdm_ratio": None if point["reported_tdm_ratio"] is None else _integer(point["reported_tdm_ratio"], "point ratio"),
            })
        paths.append({**path,
            "reported_cross_fpga_count": _integer(path["reported_cross_fpga_count"], "cross FPGA count"),
            "arrival_ns": None if path["arrival_ns"] is None else _number(path["arrival_ns"], "path arrival"),
            "points": points,
        })
    identification = _keys(root["identification"], {"bank_inventory", "single_hop_delay", "reasons"}, "identification")
    if any(identification[key] != "not_identifiable" for key in ("bank_inventory", "single_hop_delay")):
        raise ValidationError("calibration details: report facts cannot assert identified models")
    reasons = _list(identification["reasons"], "identification reasons")
    allowed = {"bank-inventory-not-reported", "crossing-boundary-and-ratio-not-bound", "tdm-ratio-semantics-unvalidated", "no-bank-allocation-rows", "no-path-specific-timing", "unmapped-timing-fpga-label"}
    if not reasons or any(not isinstance(reason, str) or reason not in allowed for reason in reasons) or len(set(reasons)) != len(reasons):
        raise ValidationError("calibration details: invalid identification reason")
    if "bank-inventory-not-reported" not in reasons or not {"crossing-boundary-and-ratio-not-bound", "tdm-ratio-semantics-unvalidated"}.intersection(reasons):
        raise ValidationError("calibration details: missing unidentifiability reasons")
    result = {"schema": root["schema"], "bank_rows": banks, "timing_paths": paths, "identification": dict(identification)}
    if legacy:
        return result
    hashes = root["report_sha256"]
    if not isinstance(hashes, Mapping) or not set(hashes) <= _REPORT_ROLES or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values()):
        raise ValidationError("calibration details: invalid ordinary report hashes")
    status = root["net_tdm_capture_status"]
    if status not in {"unavailable", "complete", "partial"}:
        raise ValidationError("calibration details: invalid net TDM capture status")
    nets = []
    net_seen = set()
    for raw in _list(root["net_tdm_rows"], "net TDM rows"):
        net = _keys(raw, {"net_token", "net_name", "source_fpga", "sink_fpga", "transport_kind", "is_clock"}, "net TDM row")
        name = _safe_label(net["net_name"], "net name")
        if net["net_token"] != _token("net", name):
            raise ValidationError("net TDM row: token/name mismatch")
        if any(not isinstance(net[field], str) or not _FPGA.fullmatch(net[field]) for field in ("source_fpga", "sink_fpga")) or net["source_fpga"] == net["sink_fpga"]:
            raise ValidationError("net TDM row: invalid FPGA endpoints")
        if net["transport_kind"] not in {"untdm", "tdm"} or type(net["is_clock"]) is not bool:
            raise ValidationError("net TDM row: invalid transport kind or clock flag")
        key = (net["net_token"], net["source_fpga"], net["sink_fpga"])
        if key in net_seen:
            raise ValidationError("net TDM row: duplicate directed net")
        net_seen.add(key)
        nets.append(dict(net))
    if nets and status == "unavailable":
        raise ValidationError("net TDM rows contradict unavailable report")
    path_limit = root["route_timing_path_limit"]
    if path_limit is not None and _integer(path_limit, "route path limit") < 1:
        raise ValidationError("route timing: path limit must be positive")
    route_paths = []
    for index, raw in enumerate(_list(root["route_timing_paths"], "route timing paths")):
        path = _keys(raw, {"id", "startpoint_token", "endpoint_token", "source_clock", "sink_clock", "arrival_ns", "normalized_delay_ns", "points", "crossings", "unbound_crossing_count"}, "route timing path")
        if path["id"] != f"route-path{index}":
            raise ValidationError("route timing path: invalid ordered identity")
        # Apply the same point/endpoint/finite-value checks used for legacy
        # SSTA points without interpreting its aggregate path delay.
        checked = validate_calibration_details({
            "schema": _LEGACY_SCHEMA, "bank_rows": [], "identification": dict(identification),
            "timing_paths": [{"id": "unspecified-path0", "constrained": False,
                              "reported_cross_fpga_count": 0, "arrival_ns": path["arrival_ns"],
                              "startpoint_token": path["startpoint_token"], "endpoint_token": path["endpoint_token"],
                              "points": path["points"], "crossings": []}],
        })["timing_paths"][0]
        if any(point["fpga"] is None for point in checked["points"]):
            raise ValidationError("route timing path: missing logical Die identity")
        crossings, unbound = _route_crossings(checked["points"], nets)
        for crossing in _list(path["crossings"], "route crossings"):
            if not isinstance(crossing, Mapping):
                raise ValidationError("route crossing: expected an object")
            _number(crossing.get("delay_ns"), "crossing delay")
            _integer(crossing.get("reported_tdm_ratio"), "crossing reported ratio")
        if path["crossings"] != crossings or _integer(path["unbound_crossing_count"], "unbound crossing count") != unbound:
            raise ValidationError("route timing path: crossing identity/delay/net binding mismatch")
        for field in ("source_clock", "sink_clock"):
            if path[field] is not None:
                _safe_label(path[field], "route clock")
        normalized = None if path["normalized_delay_ns"] is None else _number(path["normalized_delay_ns"], "normalized route delay")
        route_paths.append({**path, "arrival_ns": checked["arrival_ns"], "points": checked["points"], "normalized_delay_ns": normalized})
    if route_paths and (path_limit is None or len(route_paths) > path_limit):
        raise ValidationError("route timing: reported path coverage limit violated")
    result.update({"report_sha256": dict(hashes), "net_tdm_rows": nets,
                   "net_tdm_capture_status": status, "route_timing_paths": route_paths,
                   "route_timing_path_limit": path_limit})
    return result
